# wechat-ilink-muse-bridge

[中文](README.md) | [English](README.en.md)

**Bring Muse (Meta's personal AI assistant) into WeChat.**

Built on Tencent's official iLink / ClawBot protocol (`ilinkai.weixin.qq.com`).
After a one-time QR-code authorization, your Muse can chat with you inside
WeChat in real time: you message, it replies in seconds — and it can also
push you proactive notifications (deliveries, server alerts, reminders...),
giving you a dual channel of app + WeChat.

The core client `ilink.py` is framework-free — lift it out on its own and you
have a minimal iLink client. The repo also ships two long-polling drivers:
a standalone `daemon.py`, and the Muse hook setup used in production.

<p align="center">
  <img src="docs/demo-chat.svg" alt="Demo: burst merging, typing indicator, image sending" width="420">
  <br><em>Demo: burst merging, the "typing…" indicator, and image/video sending</em>
</p>

## Quick deploy inside Muse

Send this sentence to your Muse:

```
Use wechat-ilink-muse-bridge to connect WeChat to you: https://github.com/gfydfhcf/wechat-ilink-muse-bridge
```

It will deploy everything itself by following [`AGENTS.md`](AGENTS.md) in the
repo root — you only need to pick up WeChat once, at the QR-scan step.

## Features

- **Official protocol, QR-code login** — no reverse engineering, no
  personal-account hacks; everything runs over Tencent's official iLink channel
- **Real-time long polling** — the server holds each `getupdates` request for
  up to 35s and returns the instant a message arrives, so latency is seconds
  (every project in this space does long polling; this repo's parameters and
  backoff strategy are copy-ready)
- **Rapid-fire message merging** — when a user fires off several messages,
  iLink splits them into multiple responses. This repo does a **pre-send
  re-check**: after composing a reply but before actually sending it, it
  glances at the queue once more; anything new gets merged in and answered
  in context — one reply per burst. **None of the SDKs we surveyed do this**;
  bots that answer message-by-message feel mechanical and reply to stale
  context
- **Send images, files & videos** — `getuploadurl` → AES-128-ECB encryption → CDN
  upload → `sendmessage` with a media item. Images, files and videos are
  supported, in reply mode and proactive mode. The protocol has traps
  (two enum/encoding mismatches); they are documented in code comments
- **"Typing…" indicator** — lights up while a reply is being composed
  (`getconfig` for a `typing_ticket` + `sendtyping` to toggle it, ticket
  cached for 9 minutes), so the user can see the bot is alive
- **Proactive push** — undocumented, but `sendmessage` with a cached
  `context_token` can message the user first (with a no-token fallback
  retry if the cached token is stale)
- **Resumable media downloads** — images/files arrive via an encrypted CDN;
  interrupted downloads resume with HTTP Range + exponential-backoff retries
  (on a weak network, restarting a whole image often dies at 90%)
- **No duplicate sends** — a failed send is never blindly retried (the server
  occasionally accepts a message but drops the receipt; a blind retry means
  the user gets two copies)
- **Inbound dedupe** — the server occasionally redelivers the same message;
  poll filters by `message_id` (last 500 persisted to disk), so one message
  never gets two replies
- **Token expiry nags once** — on expiry a marker file is written and the
  user is asked to re-scan exactly once

## How it differs from similar projects

There are already plenty of iLink projects on GitHub: SDKs
([corespeed-io/wechatbot](https://github.com/corespeed-io/wechatbot),
[epiral/weixin-bot](https://github.com/epiral/weixin-bot)) meant to be used
as libraries, and Claude Code bridges
([crazynomad/weixin-claude-bot](https://github.com/crazynomad/weixin-claude-bot))
that turn WeChat into a remote control for a coding tool.

This repo connects **Muse the personal assistant itself** — an assistant with
long-term memory, goals and reminders that reports back proactively — not a
coding CLI or a bare LLM API. So the focus is not library design, but **the
pitfalls a real assistant hits when it lives on WeChat**: burst merging,
duplicate-send prevention, nag-once token expiry, and a queue that never
loses or double-delivers a message. Steal away.

## Architecture

```
WeChat ──> iLink server ──getupdates long poll──> ilink.py poll
                                                       │ new messages to disk
                                                       ▼
                                               pending queue ──> assistant/worker
                                                       │ merge bursts, compose reply
                                                       ▼
                                               ilink.py send ──> WeChat
Proactive push: any program ──> ilink.py proactive (cached context_token) ──> WeChat
```

Two polling drivers, pick one:

| Driver | Best for | Entry point |
| --- | --- | --- |
| Standalone daemon | any server / VPS | `daemon.py` |
| Muse hook | the Muse runtime (used in production) | `examples/muse-hook/` |

## Quick start

```bash
pip install -r requirements.txt

# 1. QR login (prints a QR image path; scan it with WeChat)
python3 ilink.py qrcode
python3 ilink.py login <qrcode-id>     # session saved to data/session.json

# 2. Start long polling (--echo is a demo handler: parrots everything back)
python3 daemon.py --echo

# Now send the bot a couple of WeChat messages in a row —
# you'll get a single merged reply
```

To have an LLM write the replies (any OpenAI-compatible API: OpenAI,
DeepSeek, Qwen, Moonshot, a local Ollama, ...), use `--llm` mode instead —
configuration is all environment variables:

```bash
LLM_API_KEY=sk-... python3 daemon.py --llm
# optional: LLM_BASE_URL (default https://api.openai.com/v1),
#           LLM_MODEL (default gpt-4o-mini), LLM_SYSTEM_PROMPT
```

`--llm` is an example handler: per-user context of the last 10 messages,
stdlib only. For an agent setup with tools and long-term memory, see
`examples/muse-hook/`.

Or deploy with Docker (session and media persist in the `./data` volume):

```bash
docker compose run --rm bot python3 ilink.py qrcode        # QR login, once
docker compose run --rm bot python3 ilink.py login <qrcode-id>
docker compose up -d                                       # --echo by default
```

Command cheat sheet:

```bash
python3 ilink.py poll                                   # fetch one round of new messages (JSON)
python3 ilink.py send <user_id> <context_token> -       # reply (body via stdin)
python3 ilink.py proactive <user_id> -                  # proactive push (body via stdin)
python3 ilink.py typing <user_id> <start|stop>          # show/cancel "typing…"
python3 ilink.py send-image <user_id> <ctx> <image>     # reply with an image
python3 ilink.py send-file <user_id> <ctx> <file>       # reply with a file
python3 ilink.py send-video <user_id> <ctx> <video>     # reply with a video
python3 ilink.py proactive-image <user_id> <image>      # proactively push an image
python3 ilink.py proactive-file <user_id> <file>        # proactively push a file
python3 ilink.py proactive-video <user_id> <video>      # proactively push a video
```

The data directory defaults to `./data/` and can be overridden with the
`ILINK_DATA_DIR` environment variable.

## Known limitations (the fine print, up front)

- **Session lifetime is uncertain**: early community reports suggested
  roughly 24 hours, but in production a single login here has stayed valid
  for 3+ days (Oct 2026); it may vary by account. Expiry means a human has
  to re-scan the QR code (this repo only implements "notify once" —
  re-scanning is still manual).
- **Bots only see messages sent to the bot**: iLink is a separate bot
  identity. It cannot see your main account's messages, contacts, or
  Moments — the official protocol simply has no such endpoints. Anyone
  claiming otherwise is using a reverse-engineered protocol (with ban risk).
- **Your egress network needs to be stable**: cross-border links
  occasionally cut long-polling connections; the client must back off and
  reconnect (handled here — `POLL_ERROR` lines in the log are regulars and
  harmless).

## Security notes

`data/session.json` is a **login credential**, equivalent to a password: keep
it local, it is already in `.gitignore`, and **never commit or share it**.
The same goes for chat logs and the contacts cache.

## License

MIT
