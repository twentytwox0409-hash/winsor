import json, os, re, smtplib, ssl, sys, time, urllib.request, urllib.error
from email.mime.text import MIMEText
from email.header import Header

API = "https://discord.com/api/v10/invites/{}?with_counts=true"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")


def code_of(invite):
    return re.split(r"[/?#]", invite.strip().rstrip("/"))[-1] if "/" in invite else invite.strip()


def check(code):
    req = urllib.request.Request(API.format(code), headers={"User-Agent": "invite-monitor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        return "invalid" if e.code == 404 else None
    except Exception:
        return None
    feats = (data.get("guild") or {}).get("features") or []
    return "closed" if "INVITES_DISABLED" in feats else "open"


def send_mail(subject, body):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
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


def run_once(state):
    with open(os.path.join(HERE, "communities.json"), encoding="utf-8") as f:
        items = json.load(f)
    for it in items:
        code = code_of(it["invite"])
        name = it.get("name") or code
        now = check(code)
        print(name, code, now, flush=True)
        if now is None:
            continue
        old = state.get(code)
        state[code] = now
        if old is None or old == now:
            continue
        link = "https://discord.gg/" + code
        try:
            if now == "open":
                send_mail("【开门了】" + name, name + " 现在可以加入了。\n" + link)
            elif now == "closed" and old == "open":
                send_mail("【关门了】" + name, name + " 已暂停邀请。\n" + link)
            elif now == "invalid":
                send_mail("【链接失效】" + name, "这个邀请链接查不到了，可能已被删除或更换。\n" + link)
        except Exception as e:
            print("mail failed:", e, flush=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


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

    loop = int(os.environ.get("LOOP_SECONDS", "0"))
    every = int(os.environ.get("INTERVAL_SECONDS", "60"))
    end = time.time() + loop
    while True:
        started = time.time()
        run_once(state)
        if started + every >= end:
            break
        time.sleep(max(1, every - (time.time() - started)))


if __name__ == "__main__":
    main()
