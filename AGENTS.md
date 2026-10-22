# AGENTS.md — instructions for the agent deploying this bridge

You are an AI agent (typically Muse) whose user asked you to connect WeChat
to yourself using this repository. Follow these steps. Only one step needs
the human: scanning a QR code.

## 0. Prerequisites

- Python 3.10+ and `pip install -r requirements.txt` (the only hard
  dependency is `cryptography`, used for the encrypted media CDN).
- A WeChat account the user can scan a QR code with.
- Pick a data directory and export it for every command below:
  `ILINK_DATA_DIR=<dir>` (default `./data/`).

## 1. Log in (human scans once)

```bash
python3 ilink.py qrcode        # prints the path of a QR image file
python3 ilink.py login <qrcode-id>
```

Show the QR image to the user and wait for them to scan and confirm in
WeChat. On success the session is saved to `$ILINK_DATA_DIR/session.json`.
That file is a **login credential**: keep it local, never commit it, never
send it anywhere.

## 2. Choose a driver

**If you run inside Muse with its hook runtime available** (production
setup): wire `examples/muse-hook/ilink-watch.sh` as a hook that runs every
~5 seconds. Read `examples/muse-hook/README.md` first — the script persists
each batch to a pending queue before waking you, and you claim it with an
atomic `mv`. When woken, handle the batch under this contract:

- Reply only to the owner (first sender; stored in `owner.json`).
- After claiming with `mv`, immediately `touch` the processing file:
  crash recovery judges staleness by file timestamps, and `mv` preserves
  the message-arrival time — an un-refreshed claim can be mistaken for a
  stale one and bounced back to pending.
- One reply per burst: before sending, re-check the pending file; if newer
  messages arrived while you composed, merge them and re-compose against
  the newest context (max 3 rounds).
- The script already lit the "typing…" indicator on arrival; refresh it if
  composing takes >30s and stop it after sending.
- Never manually retry a failed send; the client already retries once with
  the same client_id.
- On `token_expired`, tell the user once that a re-scan is needed.

**Otherwise**: run `python3 daemon.py --echo` to verify the loop. For an
LLM-backed bot, `python3 daemon.py --llm` is a ready-made example handler
for any OpenAI-compatible API (config via `LLM_API_KEY` / `LLM_BASE_URL` /
`LLM_MODEL` env vars); replace it with your own reply logic when you need
more (see `daemon.py`).

## 3. Verify

Ask the user to send the bot a WeChat message. Confirm: it is received, a
single reply goes back, and the typing indicator appears promptly. Also
test a proactive push:

```bash
python3 ilink.py proactive <user_id> -   # text via stdin
```

## 4. Ongoing behavior

- Session lifetime is unpredictable: early community reports suggested
  roughly 24h, but in production one login has stayed valid for 3+ days.
  When commands report `TOKEN_EXPIRED` / `NO_TOKEN`, ask the user to
  re-scan — once, not repeatedly.
- The bot only receives messages sent **to the bot**. It cannot read the
  user's main-account chats, contacts, or Moments; the protocol has no
  such endpoints. Do not promise otherwise.
- Media you send (`send-image` / `send-file` / `proactive-image` /
  `proactive-file`) goes through the encrypted CDN upload implemented in
  `ilink.py`; the protocol traps are documented in its comments.
