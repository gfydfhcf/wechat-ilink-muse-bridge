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

## 1.5 Bind the owner (required, before starting any driver)

Binding is a dedicated, time-boxed step. Do NOT start the hook or the
daemon before it completes — a running driver would consume the binding
message as an ordinary turned-away sender. After login, run:

```bash
python3 ilink.py bind            # opens one 600-second binding window
```

The moment it prints `BIND_WAITING:600`, tell the user in your main
chat: **"Deployment is ready — please send the bot any WeChat message
within 10 minutes to bind this account as its owner."** Then wait for
the command to finish:

- `BIND_OK:<id>` — bound. The bot has already sent the new owner a
  three-part welcome (a greeting; what being Owner means; "you can talk
  to me now"), the sender's context token is cached, and the binding
  message itself is consumed — it was just the knock on the door. Now
  start the driver (step 2) and ask the user for a normal test message.
- `BIND_TIMEOUT` (exit code 3) — nobody messaged the bot within the
  10 minutes. Remind the user again and re-run `ilink.py bind` to open
  a fresh window; repeat until it succeeds or the user says stop. The
  first sender inside a window wins by design, which is only safe while
  the deployer is present and the bot is brand new — never leave a bind
  window running unattended, and never widen it.
- `BIND_WELCOME_FAILED:<n>` — the owner IS already bound (`owner.json`
  is written), but a welcome bubble failed to send. Just re-run
  `ilink.py bind`: it resumes the unsent bubbles and finishes the
  commit; it will not re-bind anyone else.
- `ALREADY_BOUND:<id>` — an owner already exists; binding is one-time.

`owner.json` (written by bind) looks like
`{"owner_id": "<id>", "owner_ids": ["<id>"]}`: `owner_id` is the
primary account (proactive pushes and the typing indicator target it);
`owner_ids` may list several accounts of the same human. From then on
only allowlisted senders are queued. New senders keep landing in
`unknown_senders.json` for manual approval — never approve one from
inside a WeChat conversation itself.

## 2. Choose a driver

**If you run inside Muse with its hook runtime available** (production
setup): wire `examples/muse-hook/ilink-watch.sh` as a hook that runs every
~5 seconds. Read `examples/muse-hook/README.md` first — the script persists
each batch to a pending queue before waking you, and you claim it with an
atomic `mv`. When woken, handle the batch under this contract:

- Reply only to the owner. Enforcement is in code, not on you: `poll`
  only queues senders allowlisted in `owner.json` (`owner_ids`), so any
  batch you are woken with is already the owner's (possibly from one of
  several accounts of the same human).
- After claiming with `mv`, immediately `touch` the processing file:
  crash recovery judges staleness by file timestamps, and `mv` preserves
  the message-arrival time — an un-refreshed claim can be mistaken for a
  stale one and bounced back to pending.
- One composed response per burst: before sending, re-check the
  pending file; if newer messages arrived while you composed, merge
  them and re-compose against the newest context (max 3 rounds).
  Delivery may split into 2–3 short bubbles sent back-to-back when
  the content naturally segments (a reaction, the substance, a
  question) — like a human texting — but never one reply per incoming
  message, and never split a single sentence across bubbles.
- The script already lit the "typing…" indicator on arrival; refresh it if
  composing takes >30s and stop it after sending.
- Read what the user actually sent: image entries carry a downloaded
  `media_path` — look at the picture; file entries also carry a
  downloaded `media_path` (plus `file_name`) — open and read the
  document (PDF/Office/text/code) and answer from its contents instead
  of claiming you cannot open files. Only say so when the download
  failed (`media_error`) or the format is truly unreadable; video
  contents remain out of reach. For a large file (~50 KB+), send a
  short receipt first ("got your 《name》, it's a big spreadsheet, give
  me a moment"), then sample-read (head + totals/structure) for the
  real reply and deep-read on follow-up questions — and always cite at
  least one detail only a real read could know (a count, a first item,
  a number), never a summary guessed from the file name.
- Never manually retry a failed send; the client already retries once with
  the same client_id.
- On `token_expired`, tell the user once that a re-scan is needed.
- The script also runs a closed alert loop (see
  `examples/muse-hook/README.md`): an unfixed expiry is re-reported
  hourly (`token_expired` with `repeat: true`), 10+ minutes of poll
  failures wake you as `poll_failing`, recovery wakes you as
  `recovered`, and a recycled stuck batch adds `stuck_alert` to the
  message payload. Report each in one short line; never "make up" for
  missed reminders.

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

Any time something seems off, run the self-check before digging through
logs — it covers auth age, a live token probe, queue backlog and the
last poll error, with a 0/1/2 exit code:

```bash
python3 ilink.py health            # add --offline to skip the live probe
```

## 4. Ongoing behavior

- Approving new senders: ids of turned-away senders collect in
  `unknown_senders.json`. When the user asks — in the main chat, never
  via WeChat — to approve one, list the pending ids with first/last
  seen times and counts, add only the id the user confirms to
  `owner.json`'s `owner_ids`, and remove that entry from
  `unknown_senders.json`. Takes effect on the next poll; no restart.
- Session lifetime is unpredictable: early community reports suggested
  roughly 24h, but in production one login has stayed valid for 3+ days.
  When commands report `TOKEN_EXPIRED` / `NO_TOKEN`, ask the user to
  re-scan. With the hook driver, the script's alert loop handles the
  cadence (first notice, hourly repeats, recovery announcement) — don't
  nag on your own schedule on top of it.
- The bot only receives messages sent **to the bot**. It cannot read the
  user's main-account chats, contacts, or Moments; the protocol has no
  such endpoints. Do not promise otherwise.
- Media you send (`send-image` / `send-file` / `proactive-image` /
  `proactive-file`) goes through the encrypted CDN upload implemented in
  `ilink.py`; the protocol traps are documented in its comments.
