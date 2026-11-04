import json, os, re, smtplib, ssl, subprocess, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.header import Header

API = "https://discord.com/api/v10/invites/{}?with_counts=true"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleCrawl/1.0 invite-monitor"
FAIL_ALERT_AFTER = 8   # 连续这么多轮查询失败，就发一封“监控失灵”提醒


def log(*a):
    t = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
    print(t, *a, flush=True)


def code_of(invite):
    return re.split(r"[/?#]", invite.strip().rstrip("/"))[-1] if "/" in invite else invite.strip()


def check(code):
    """返回 open / closed / invalid；查询失败返回 None。遇到限流或服务器错误会自动等待重试。"""
    for attempt in range(3):
        req = urllib.request.Request(API.format(code), headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
            feats = (data.get("guild") or {}).get("features") or []
            return "closed" if "INVITES_DISABLED" in feats else "open"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "invalid"
            if e.code == 429:
                try:
                    wait = float(e.headers.get("Retry-After", "5"))
                except Exception:
                    wait = 5
                log("限流 429，等待", wait, "秒")
                time.sleep(min(max(wait, 1), 20))
                continue
            log("HTTP", e.code, code)
            if e.code >= 500:
                time.sleep(2)
                continue
            return None
        except Exception as e:
            log("网络错误", type(e).__name__, code)
            time.sleep(2)
    return None


def confirmed(code, first):
    """状态变化时，隔几秒再确认两次，两次都一致才算数，防止偶发错误读数造成误报/漏报。"""
    for _ in range(2):
        time.sleep(3)
        again = check(code)
        if again != first:
            log("复查结果不一致，暂不判定", code, first, "→", again)
            return False
    return True


def send_mail(subject, body):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT") or "465")
    user, pwd = os.environ["SMTP_USER"], os.environ["SMTP_PASS"]
    to_list = [x.strip() for x in re.split(r"[,;，；\s]+", os.environ["MAIL_TO"]) if x.strip()]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"], msg["To"] = user, ", ".join(to_list)
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
            s.login(user, pwd)
            s.sendmail(user, to_list, msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(user, pwd)
            s.sendmail(user, to_list, msg.as_string())


def git(*args):
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True)


def save_state(state, push):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    if not push or not os.environ.get("GITHUB_ACTIONS"):
        return
    git("add", "state.json")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("-c", "user.name=monitor-bot", "-c", "user.email=monitor-bot@users.noreply.github.com",
        "commit", "-m", "update state")
    for _ in range(3):
        if git("push").returncode == 0:
            return
        git("pull", "--rebase", "--autostash", "-q")
    log("状态推送失败（不影响监控继续）")


def notify(code, name, old, now):
    """需要通知则发邮件；发送失败抛异常。返回 True 表示这次变化已处理。"""
    link = "https://discord.gg/" + code
    if now == "open":
        send_mail("【开门了】" + name, name + " 现在可以加入了。\n" + link)
    elif now == "closed" and old == "open":
        send_mail("【关门了】" + name, name + " 已暂停邀请。\n" + link)
    elif now == "invalid":
        send_mail("【链接失效】" + name, "这个邀请链接查不到了，可能已被删除或更换。\n" + link)


def run_once(state, fails):
    with open(os.path.join(HERE, "communities.json"), encoding="utf-8") as f:
        items = json.load(f)
    dirty = False
    for it in items:
        code = code_of(it["invite"])
        name = it.get("name") or code
        now = check(code)
        old = state.get(code)

        if now is None:
            fails[code] = fails.get(code, 0) + 1
            log(name, code, "查询失败 第", fails[code], "次")
            if fails[code] == FAIL_ALERT_AFTER:
                try:
                    send_mail("【监控异常】" + name, "连续多次查不到这个社区的状态（可能被 Discord 限流或网络问题），"
                              "这期间的开关门可能漏报。恢复后会自动继续。\nhttps://discord.gg/" + code)
                except Exception as e:
                    log("异常提醒发送失败:", e)
            continue
        if fails.get(code, 0) >= FAIL_ALERT_AFTER:
            log(name, "查询已恢复")
        fails[code] = 0

        if old is None:                       # 第一次见到：只记录，不通知
            state[code] = now
            dirty = True
            log(name, code, now, "(首次记录)")
            continue
        if now == old:
            log(name, code, now)
            continue

        if not confirmed(code, now):
            continue
        log(name, code, old, "→", now, "（已复查确认）")
        try:
            notify(code, name, old, now)
        except Exception as e:
            log("邮件发送失败，下一轮会重试:", e)
            continue                          # 状态不更新，下一轮再发，不会因为发信失败而漏掉
        state[code] = now
        save_state(state, push=True)          # 每次变化立刻存档，任务被中断也不会丢
    save_state(state, push=dirty)


def main():
    if "--test" in sys.argv:
        send_mail("测试邮件：开门提醒已连通", "收到这封邮件说明邮箱设置正确。")
        print("test mail sent")
        return

    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}

    loop = int(os.environ.get("LOOP_SECONDS") or "0")
    every = int(os.environ.get("INTERVAL_SECONDS") or "20")
    end = time.time() + loop
    fails = {}
    last_pull = time.time()
    while True:
        started = time.time()
        try:
            run_once(state, fails)
        except Exception as e:
            log("本轮出错:", type(e).__name__, e)
        # 每分钟同步一次仓库，这样在网页上新增/删除社区，不用等下一个任务就能生效
        if os.environ.get("GITHUB_ACTIONS") and time.time() - last_pull > 60:
            git("pull", "--rebase", "--autostash", "-q")
            last_pull = time.time()
        if started + every >= end:
            break
        time.sleep(max(1, every - (time.time() - started)))


if __name__ == "__main__":
    main()
