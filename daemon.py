#!/usr/bin/env python3
"""Standalone long-poll daemon for the iLink WeChat bot.

Reference loop used by wechat-ilink-muse-bridge: keep calling
`ilink.py poll` back-to-back. Each poll is a *long* poll — the server
holds the connection for up to ~35s and returns the instant a message
arrives — so this loop is effectively push, with second-level latency.

It also implements burst merging: when a user sends several messages
seconds apart ("吃完了" / "吃干净了"), the iLink server often returns
them in separate poll responses. Instead of replying to each one, the
daemon keeps collecting for MERGE_WINDOW seconds and dispatches the
whole burst as ONE user turn. (None of the popular iLink SDKs do this;
bots built on raw per-message dispatch feel robotic.)

Usage:
  python3 ilink.py qrcode && python3 ilink.py login <qrcode>   # once
  python3 daemon.py              # log incoming bursts, reply nothing
  python3 daemon.py --echo       # reply once per burst (demo handler)

Environment:
  MERGE_WINDOW   seconds to keep collecting a burst (default 25)
  ILINK_DATA_DIR data dir forwarded to ilink.py (default ./data)
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MERGE_WINDOW = float(os.environ.get("MERGE_WINDOW", "25"))


def poll_once():
    """One long-poll. Returns (status, msgs)."""
    r = subprocess.run(
        [sys.executable, os.path.join(HERE, "ilink.py"), "poll"],
        capture_output=True, text=True, timeout=180,
    )
    out = (r.stdout or "").strip()
    if out.startswith("NO_TOKEN") or out.startswith("TOKEN_EXPIRED"):
        return "expired", []
    if out.startswith("POLL_ERROR"):
        return "error", []
    try:
        return "ok", json.loads(out).get("msgs") or []
    except ValueError:
        return "error", []


def collect_burst(first_msgs):
    """Keep polling until MERGE_WINDOW elapses with no new messages."""
    msgs = list(first_msgs)
    deadline = time.time() + MERGE_WINDOW
    while time.time() < deadline:
        status, more = poll_once()
        if status == "ok" and more:
            msgs.extend(more)
            deadline = time.time() + MERGE_WINDOW  # burst still going
        elif status == "ok":
            break  # a full long-poll came back empty: burst is over
        else:
            time.sleep(2)
    return msgs


def _typing(user, word):
    """Best-effort 'typing...' indicator; failures are ignored by design."""
    try:
        subprocess.run(
            [sys.executable, os.path.join(HERE, "ilink.py"), "typing", user, word],
            capture_output=True, timeout=30,
        )
    except Exception:
        pass


def handle_burst(msgs, echo=False):
    """One user turn. Replace this with your own agent/LLM call."""
    by_user = {}
    for m in msgs:
        by_user.setdefault(m["from_user_id"], []).append(m)
    for user, items in by_user.items():
        texts = [i.get("text") or f"[{i.get('kind')}]" for i in items]
        print(f"[{time.strftime('%H:%M:%S')}] {user}: " + " / ".join(texts), flush=True)
        if echo:
            ctx = items[-1]["context_token"]  # always reply with the latest ctx
            reply = "收到：" + " ".join(texts)
            _typing(user, "start")  # show "typing..." while the reply goes out
            try:
                subprocess.run(
                    [sys.executable, os.path.join(HERE, "ilink.py"), "send", user, ctx, "-"],
                    input=reply, text=True, timeout=120,
                )
            finally:
                _typing(user, "stop")


def main():
    echo = "--echo" in sys.argv
    backoff = 1
    noticed_expired = False
    print("long-poll daemon started (Ctrl+C to stop)", flush=True)
    while True:
        status, msgs = poll_once()
        if status == "expired":
            if not noticed_expired:
                print("TOKEN EXPIRED: run `ilink.py qrcode` + `login` again.", flush=True)
                noticed_expired = True
            time.sleep(60)
            continue
        if status == "error":
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        backoff = 1
        if msgs:
            handle_burst(collect_burst(msgs), echo=echo)


if __name__ == "__main__":
    main()
