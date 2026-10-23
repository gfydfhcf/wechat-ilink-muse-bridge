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
  python3 ilink.py bind          # once: 10-min window, first sender = owner
  python3 daemon.py              # log incoming bursts, reply nothing
  python3 daemon.py --echo       # reply once per burst (demo handler)
  python3 daemon.py --llm        # reply with an LLM (example handler)

Environment:
  MERGE_WINDOW   seconds to keep collecting a burst (default 25)
  ILINK_DATA_DIR data dir forwarded to ilink.py (default ./data)
  LLM_API_KEY    API key for --llm mode (required there)
  LLM_BASE_URL   OpenAI-compatible base URL (default https://api.openai.com/v1)
  LLM_MODEL      model name (default gpt-4o-mini)
  LLM_SYSTEM_PROMPT  system prompt for --llm mode (default: a brief
                 Chinese assistant persona)
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

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
        data = json.loads(out)
    except ValueError:
        return "error", []
    # Two-phase poll: submit the commit block now that the batch is in
    # hand. If commit fails, the batch is simply re-fetched next round —
    # duplicates are possible, loss is not.
    commit = data.get("commit")
    if commit:
        try:
            subprocess.run(
                [sys.executable, os.path.join(HERE, "ilink.py"), "commit"],
                input=json.dumps(commit), capture_output=True, text=True,
                timeout=30,
            )
        except Exception:
            pass
    return "ok", data.get("msgs") or []


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


def handle_burst(msgs, echo=False, llm=False):
    """One user turn. Replace this with your own agent/LLM call."""
    by_user = {}
    for m in msgs:
        by_user.setdefault(m["from_user_id"], []).append(m)
    for user, items in by_user.items():
        texts = [i.get("text") or f"[{i.get('kind')}]" for i in items]
        print(f"[{time.strftime('%H:%M:%S')}] {user}: " + " / ".join(texts), flush=True)
        if not (echo or llm):
            continue
        ctx = items[-1]["context_token"]  # always reply with the latest ctx
        if llm:
            reply = llm_reply(user, " ".join(texts))
        else:
            reply = "收到：" + " ".join(texts)
        _typing(user, "start")  # show "typing..." while the reply goes out
        try:
            subprocess.run(
                [sys.executable, os.path.join(HERE, "ilink.py"), "send", user, ctx, "-"],
                input=reply, text=True, timeout=120,
            )
        finally:
            _typing(user, "stop")


# ---------------------------------------------------------------------------
# Example LLM handler (--llm): any OpenAI-compatible chat-completions API.
# Works with OpenAI itself, DeepSeek, Qwen (DashScope compatible mode),
# Moonshot, a local Ollama (LLM_BASE_URL=http://localhost:11434/v1,
# LLM_API_KEY can be any string), etc. Stdlib only — no extra dependency.
# Per-user history (last LLM_HISTORY messages) is persisted to
# llm_history.json in the data dir, so a daemon restart continues the
# conversation instead of starting fresh; delete that file to reset.
# Swap this function out for your own agent call if you need real
# tools/memory — that is what the Muse hook setup in
# examples/muse-hook/ does.
# ---------------------------------------------------------------------------

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
LLM_SYSTEM_PROMPT = os.environ.get(
    "LLM_SYSTEM_PROMPT", "你是一个微信聊天助手，回复简短、口语化，用中文。"
)
LLM_HISTORY = 10  # messages per user kept as context
DATA_DIR = os.environ.get("ILINK_DATA_DIR", "./data")
HISTORY_FILE = os.path.join(DATA_DIR, "llm_history.json")
_history = None  # loaded lazily from HISTORY_FILE on first use


def _load_history():
    global _history
    if _history is None:
        try:
            with open(HISTORY_FILE) as f:
                data = json.load(f)
            _history = data if isinstance(data, dict) else {}
        except Exception:
            _history = {}  # missing or corrupt: start fresh, never crash
    return _history


def _save_history():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_history, f, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, HISTORY_FILE)
    except Exception as e:
        print(f"[llm] could not persist history: {e}", flush=True)


def llm_reply(user, text):
    """Send one user turn to the LLM and return its reply text."""
    history = _load_history()
    hist = history.setdefault(user, [])
    messages = (
        [{"role": "system", "content": LLM_SYSTEM_PROMPT}]
        + hist
        + [{"role": "user", "content": text}]
    )
    req = urllib.request.Request(
        LLM_BASE_URL + "/chat/completions",
        data=json.dumps({"model": LLM_MODEL, "messages": messages}).encode(),
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        reply = data["choices"][0]["message"]["content"].strip()
    except Exception as e:  # network error, bad key, unexpected payload...
        print(f"[llm] request failed: {e}", flush=True)
        return "（LLM 调用失败，看一下 daemon 的日志）"
    hist += [
        {"role": "user", "content": text},
        {"role": "assistant", "content": reply},
    ]
    history[user] = hist[-LLM_HISTORY:]
    _save_history()
    return reply


def main():
    llm = "--llm" in sys.argv
    echo = "--echo" in sys.argv and not llm
    if llm and not LLM_API_KEY:
        print(
            "LLM_API_KEY is not set. Example:\n"
            "  LLM_API_KEY=sk-... python3 daemon.py --llm\n"
            "Optional: LLM_BASE_URL, LLM_MODEL, LLM_SYSTEM_PROMPT (see docstring).",
            flush=True,
        )
        sys.exit(1)
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
            handle_burst(collect_burst(msgs), echo=echo, llm=llm)


if __name__ == "__main__":
    main()
