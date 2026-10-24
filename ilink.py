#!/usr/bin/env python3
"""iLink WeChat bot client (official Tencent protocol, no OpenClaw needed).

Usage:
  ilink.py qrcode              # get login QR, save image, print QR_CODE:<id>
  ilink.py status <qrcode>     # poll scan status once (JSON)
  ilink.py login <qrcode>      # poll until confirmed (up to 300s), save token
  ilink.py bind [seconds]      # one 10-min owner-binding window (first sender wins + welcome)
  ilink.py poll                # fetch new messages since cursor (JSON; two-phase: see commit)
  ilink.py commit              # commit a poll round (commit block via stdin): cursor + seen ids
  ilink.py send <to> <ctx> <text> [--key <id>]  # send text reply (use - for stdin)
  ilink.py proactive <to> <text> [--key <id>]   # proactive push via cached context_token
  ilink.py typing <to> <start|stop>  # show/cancel the "typing..." indicator
  ilink.py send-image <to> <ctx> <path> [--key <id>]   # reply with an image
  ilink.py send-file <to> <ctx> <path> [--key <id>]    # reply with a file
  ilink.py send-video <to> <ctx> <path> [--key <id>]   # reply with a video
  ilink.py send-voice <to> <ctx> <path> <playtime_ms>   # voice msg (SILK@24k; NOTE: channel silently drops outbound voice)
  ilink.py proactive-image <to> <path>    # proactive image via cached ctx
  ilink.py proactive-file <to> <path>     # proactive file via cached ctx
  ilink.py health [--offline]  # self-check: auth, queue, last poll (exit 0 ok / 1 warn / 2 bad)
  ilink.py proactive-video <to> <path>    # proactive video via cached ctx
  ilink.py proactive-voice <to> <path> <playtime_ms>   # proactive voice (dropped by channel, see send-voice)

  --key <id> is an idempotency key (e.g. the batch's first message_id):
  a second invocation with the same key prints SEND_ALREADY_SENT and
  does not call sendmessage again. A keyed send runs under an
  inter-process ledger lock and persists its intent (with a stable
  client_id) BEFORE calling sendmessage; only a confirmed success
  flips the entry to sent, and a retry reuses the same client_id, so
  the server's client_id dedupe covers the send-succeeded-but-
  unconfirmed window too. Legacy ledger entries without a status
  field (result 'ok') still count as sent. Every send attempt is also
  appended to send_audit.jsonl in the data dir (timestamp, direction,
  recipient, client_id, result, error type — never message text or
  tokens). A retry under the same key with different text delivers at
  most the first text — the deduped retry never re-delivers.
"""
import base64
import contextlib
import functools
import inspect
import json
import os
import random
import re
import sys
import threading
import time
import urllib.request

try:
    import fcntl
except ImportError:  # Windows has no fcntl: the ledger lock degrades
    fcntl = None  # to a no-op there (single-process use is unaffected).

BASE_URL = "https://ilinkai.weixin.qq.com"
# Data directory (session token, contacts cache, downloaded media).
# Override with the ILINK_DATA_DIR environment variable. Everything in here is
# PRIVATE (the session file is a login credential) — never commit it.
BRIDGE = os.environ.get(
    "ILINK_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
)
os.makedirs(BRIDGE, exist_ok=True)
os.makedirs(os.path.join(BRIDGE, "media"), exist_ok=True)
SESSION_FILE = os.path.join(BRIDGE, "session.json")
CONTACTS_FILE = os.path.join(BRIDGE, "contacts.json")  # from_user_id -> {context_token, updated_at}
SEEN_FILE = os.path.join(BRIDGE, "seen_ids.json")  # delivered inbound message_ids (dedupe)
SEND_AUDIT_FILE = os.path.join(BRIDGE, "send_audit.jsonl")  # one JSON line per send attempt
SEND_LEDGER_FILE = os.path.join(BRIDGE, "send_ledger.json")  # idempotency key -> intent / successful send
SEND_LEDGER_LOCK_FILE = os.path.join(BRIDGE, "send_ledger.lock")  # inter-process lock for keyed sends
SEND_AUDIT_CAP = 1000  # audit lines kept; older lines are trimmed on write
SEND_LEDGER_TTL = 86400  # ledger entries older than this are pruned on write
MEDIA_MAX_BYTES = 100 * 1024 * 1024  # media sends refuse above this size:
# the protocol needs the whole file AES-encrypted and uploaded in one
# POST, so a send reads the file fully into memory (plus ciphertext).
# Refusing up front beats letting a huge file exhaust process memory;
# chunked upload would need a protocol change and is out of scope.
MEDIA_DIR = os.path.join(BRIDGE, "media")
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

# MessageItemType: TEXT=1, IMAGE=2, VOICE=3, FILE=4, VIDEO=5
ITEM_TYPE_NAMES = {1: "text", 2: "image", 3: "voice", 4: "file", 5: "video"}
ITEM_KEY = {2: "image_item", 3: "voice_item", 4: "file_item", 5: "video_item"}


def _aes_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    dec = cipher.decryptor()
    padded = dec.update(ciphertext) + dec.finalize()
    unpadder = PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def _parse_aes_key(aes_key_b64: str) -> bytes:
    raw = base64.b64decode(aes_key_b64)
    if len(raw) == 16:
        return raw
    if len(raw) == 32:
        try:
            return bytes.fromhex(raw.decode("ascii"))
        except ValueError:
            pass
    raise ValueError(f"bad aes_key length: {len(raw)}")


def _http_get_range(url, start=0, timeout=60):
    """GET url, optionally resuming from byte offset `start`.
    Returns (status, data, total_len_or_None, complete_bool). IncompleteRead 被内部消化为 complete=False。"""
    from http.client import IncompleteRead
    req_headers = {"User-Agent": "Mozilla/5.0"}
    if start:
        req_headers["Range"] = f"bytes={start}-"
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        status = r.status
        total = None
        cr = r.headers.get("Content-Range")  # e.g. "bytes 100-99183/99184"
        if cr and "/" in cr:
            try:
                total = int(cr.rsplit("/", 1)[1])
            except ValueError:
                pass
        if total is None:
            cl = r.headers.get("Content-Length")
            if cl:
                try:
                    # 206 的 Content-Length 是剩余字节数，200 的是全长
                    total = int(cl) + (start if status == 206 else 0)
                except ValueError:
                    pass
        try:
            data = r.read()
            complete = True
        except IncompleteRead as e:
            data = e.partial or b""
            complete = False
        return status, data, total, complete


def download_media_item(item: dict, item_type: int):
    """Download + AES-ECB decrypt an IMAGE/VOICE/FILE/VIDEO item. Returns saved path or None.
    Diagnostics go to stderr only: poll's stdout is a machine-readable JSON
    channel, and any extra line printed there corrupts the whole batch for
    callers (hook jq / daemon json.loads) — regression-tested."""
    key = ITEM_KEY.get(item_type)
    if not key:
        return None
    sub = item.get(key) or {}
    media = sub.get("media") or {}
    aes_key_b64 = media.get("aes_key")
    if not aes_key_b64:
        aes_hex = sub.get("aeskey")
        if aes_hex:
            aes_key_b64 = base64.b64encode(bytes.fromhex(aes_hex)).decode("ascii")
    if not aes_key_b64:
        print(f"MEDIA_ERROR:no aes_key", file=sys.stderr, flush=True)
        return None
    aes_key = _parse_aes_key(aes_key_b64)
    full_url = media.get("full_url")
    eqp = media.get("encrypt_query_param")
    if full_url:
        url = full_url
    elif eqp:
        from urllib.parse import quote
        url = f"{CDN_BASE_URL}/download?encrypted_query_param={quote(eqp, safe='')}"
    else:
        print(f"MEDIA_ERROR:no download url", file=sys.stderr, flush=True)
        return None
    ciphertext = b""
    total = None
    last_err = "no attempt"
    for attempt in range(4):
        try:
            status, chunk, total, complete = _http_get_range(url, start=len(ciphertext), timeout=60)
        except Exception as e:
            last_err = f"{type(e).__name__}:{str(e)[:100]}"
            time.sleep(2 * (attempt + 1))
            continue
        if status == 206 and len(ciphertext):
            ciphertext += chunk
        else:
            # 200 或服务器忽略 Range：以本次响应为准（可能是不完整的，靠 complete/total 判断）
            ciphertext = chunk
        last_err = "ok" if complete else f"short read {len(ciphertext)}/{total}"
        if total is not None and len(ciphertext) >= total:
            break
        if complete and ciphertext:
            break  # 无长度信息但一次读完，视为完成
        time.sleep(2 * (attempt + 1))
    if total is not None and len(ciphertext) < total:
        print(f"MEDIA_ERROR:incomplete after retries {len(ciphertext)}/{total} last={last_err}", file=sys.stderr, flush=True)
        return None
    if not ciphertext:
        print(f"MEDIA_ERROR:empty download last={last_err}", file=sys.stderr, flush=True)
        return None
    plaintext = _aes_ecb_decrypt(ciphertext, aes_key)
    ext = {2: "jpg", 3: "silk", 4: "bin", 5: "mp4"}.get(item_type, "bin")
    if item_type == 4:
        fname = sub.get("file_name") or ""
        if "." in fname:
            ext = fname.rsplit(".", 1)[-1][:8]
    os.makedirs(MEDIA_DIR, exist_ok=True)
    path = os.path.join(MEDIA_DIR, f"{int(time.time())}_{random.randint(0, 0xFFFFFF):06x}.{ext}")
    with open(path, "wb") as f:
        f.write(plaintext)
    os.chmod(path, 0o600)
    return path


def load_contacts():
    if os.path.exists(CONTACTS_FILE):
        try:
            with open(CONTACTS_FILE) as f:
                data = json.load(f)
        except Exception as e:
            # Corrupt cache must not crash the whole command: warn on
            # stderr (poll's stdout is machine-readable JSON) and fall
            # back to an empty cache; the next poll rebuilds it.
            print(f"WARNING: corrupt contacts file {CONTACTS_FILE}: {e}; "
                  "treating as empty", file=sys.stderr, flush=True)
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def save_contacts(c):
    os.makedirs(BRIDGE, exist_ok=True)
    tmp = CONTACTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONTACTS_FILE)


# --- Inbound dedupe ---------------------------------------------------------
# getupdates occasionally redelivers the same message (e.g. a response lost
# after the cursor advanced). Processing one message_id twice means the
# user gets two replies. poll is the only inbound path, so filter here and
# both drivers (hook and daemon) are protected. An id is recorded as seen
# when poll returns it — the same semantics as the cursor advancing
# (delivered = returned by poll). Messages without a message_id cannot be
# deduped and pass through.
SEEN_CAP = 500  # keep the most recent 500 ids; older ones roll off

def load_seen():
    try:
        with open(SEEN_FILE) as f:
            ids = json.load(f)
        return ids if isinstance(ids, list) else []
    except Exception:
        return []


def save_seen(ids):
    os.makedirs(BRIDGE, exist_ok=True)
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ids[-SEEN_CAP:], f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SEEN_FILE)


def filter_seen(raw_msgs):
    """Split raw msgs into (kept, new_ids) by message_id — READ ONLY.

    Ids are persisted by mark_seen() at commit time, after the caller
    has durably queued the batch. Recording them here would lose
    messages if the process died before queueing: the cursor would
    already be advanced and the id marked, so the server would never
    redeliver and a redelivery would be filtered out.
    """
    seen_set = set(load_seen())
    kept, new_ids = [], []
    for m in raw_msgs:
        mid = m.get("message_id")
        if mid is not None:
            mid = str(mid)
            if mid in seen_set:
                continue
            seen_set.add(mid)
            new_ids.append(mid)
        kept.append(m)
    return kept, new_ids


def mark_seen(ids):
    """Persist message ids as delivered (the commit step of polling)."""
    if not ids:
        return
    seen = load_seen()
    have = set(seen)
    merged = seen + [i for i in (str(x) for x in ids) if i not in have]
    if len(merged) != len(seen):
        save_seen(merged)


def load_owner_ids():
    """The owner allowlist: owner.json's `owner_ids` (several accounts of
    the same human are fine), falling back to the legacy single
    `owner_id`. A missing/empty owner.json yields an EMPTY set and poll
    then denies everyone — deny-by-default is deliberate: the bot holds
    the owner's memories and tools, so an unconfigured bridge must fail
    closed, not open."""
    try:
        with open(os.path.join(BRIDGE, "owner.json")) as f:
            data = json.load(f)
    except Exception:
        return set()
    ids = list(data.get("owner_ids") or [])
    if data.get("owner_id"):
        ids.append(data["owner_id"])
    return {str(i) for i in ids if i}


def note_unknown_sender(from_id):
    """Record a sender the allowlist turned away, in unknown_senders.json.

    This is the approval queue: the deployer reads the ids here and adds
    their own extra accounts to owner.json by hand. Only ids and counts
    are kept — never message content. Capped at the 100 most recent
    senders so a flood cannot grow the file without bound."""
    path = os.path.join(BRIDGE, "unknown_senders.json")
    try:
        with open(path) as f:
            book = json.load(f)
    except Exception:
        book = {}
    now = int(time.time())
    ent = book.get(from_id) or {"first_seen": now, "count": 0}
    ent["last_seen"] = now
    ent["count"] = ent.get("count", 0) + 1
    book[from_id] = ent
    if len(book) > 100:
        book = dict(sorted(book.items(),
                           key=lambda kv: kv[1].get("last_seen", 0))[-100:])
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(book, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# --- First-owner binding ----------------------------------------------------
# Binding is a dedicated setup command, NOT part of normal polling: regular
# poll stays deny-by-default forever. After QR login the deployer runs
# `ilink.py bind`, which opens ONE bounded window (default 600s). The first
# account to message the bot inside that window becomes the owner and gets
# the three-part welcome below. If the window lapses, bind exits with
# BIND_TIMEOUT (exit 3) so the deploying agent can remind the human and run
# bind again — an unattended, indefinitely open "first come wins" mode
# would let a stranger seize a bridge whose owner file was lost.
BIND_DEFAULT_SECONDS = 600
WELCOME_MESSAGES = [
    "欢迎使用 WeChat iLink Muse Bridge！",
    "绑定成功，当前微信账号已设为 Owner。今后只有 Owner 以及你手动批准的账号可以与我聊天。",
    "现在可以直接给我发送消息了。",
]


def _owner_path():
    return os.path.join(BRIDGE, "owner.json")


def _bind_state_path():
    return os.path.join(BRIDGE, "bind_state.json")


def _bind_lock_path():
    return os.path.join(BRIDGE, "bind.lock")


def _save_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def save_owner(owner_id):
    _save_json_atomic(_owner_path(),
                      {"owner_id": owner_id, "owner_ids": [owner_id]})


def load_bind_state():
    try:
        with open(_bind_state_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_bind_state(state):
    _save_json_atomic(_bind_state_path(), state)


def clear_bind_state():
    try:
        os.remove(_bind_state_path())
    except FileNotFoundError:
        pass


def _acquire_bind_lock():
    """Single-window lock: two bind commands must never race for the first
    sender. A lock older than 15 minutes is treated as stale (the holder
    died) and taken over once."""
    path = _bind_lock_path()
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > 900:
                    os.remove(path)
                    continue
            except FileNotFoundError:
                continue
            return None
        os.write(fd, json.dumps(
            {"pid": os.getpid(), "started_at": int(time.time())}).encode())
        return fd
    return None


def _release_bind_lock(fd):
    try:
        os.close(fd)
    except Exception:
        pass
    try:
        os.remove(_bind_lock_path())
    except FileNotFoundError:
        pass


def _send_welcome_segment(to_user_id, context_token, text, token, client_id):
    """Send one welcome bubble. Network failures retry once with the SAME
    client_id (same rule as cmd_send); application failures do not retry."""
    for attempt in (1, 2):
        try:
            data = _send_raw(to_user_id, context_token, text, token,
                             client_id=client_id)
        except Exception as e:
            if attempt == 1:
                time.sleep(1)
                continue
            print(f"BIND_SEND_ERROR:{e}", file=sys.stderr, flush=True)
            return False, None
        code = data.get("code", 0)
        if code == 0:
            return True, code
        return False, code
    return False, None


def _deliver_pending_welcome(token):
    """Send any unsent welcome bubbles for a claimed owner, then commit the
    binding batch. Progress is persisted after every bubble, so re-running
    bind after a crash resumes where it stopped instead of re-binding or
    re-sending from scratch."""
    state = load_bind_state()
    owner_id = state.get("owner_id")
    if not owner_id:
        print("BIND_STATE_INVALID")
        return 1
    ctx = state.get("context_token")
    if not ctx:
        print("BIND_NO_CONTEXT_TOKEN")
        return 1
    claim = str(state.get("claim_id") or state.get("claimed_at") or "bind")
    sent = int(state.get("welcome_sent") or 0)
    while sent < len(WELCOME_MESSAGES):
        client_id = f"muse-wechat-bind-{claim}-{sent}"
        ok, code = _send_welcome_segment(owner_id, ctx,
                                         WELCOME_MESSAGES[sent], token,
                                         client_id)
        if code == -14:
            print("TOKEN_EXPIRED")
            return 2
        if not ok:
            print(f"BIND_WELCOME_FAILED:{sent + 1}")
            return 1
        sent += 1
        state["welcome_sent"] = sent
        save_bind_state(state)
    commit = state.get("commit") or {}
    if commit.get("cursor"):
        s = load_session()
        s["cursor"] = commit["cursor"]
        save_session(s)
    mark_seen(commit.get("ids") or [])
    clear_bind_state()
    print(f"BIND_OK:{owner_id}")
    return 0


def cmd_bind(wait_secs=BIND_DEFAULT_SECONDS):
    """Open one owner-binding window and wait for the first message.

    Exit codes: 0 bound (or already bound), 1 error / welcome pending,
    2 token expired, 3 window timed out (remind the human, run again),
    4 another bind window is already open."""
    owners = load_owner_ids()
    if owners and not load_bind_state():
        primary = sorted(owners)[0]
        print(f"ALREADY_BOUND:{primary}")
        return 0
    lock_fd = _acquire_bind_lock()
    if lock_fd is None:
        print("BIND_IN_PROGRESS")
        return 4
    try:
        # Re-check under the lock: another window may have just finished.
        owners = load_owner_ids()
        state = load_bind_state()
        s = load_session()
        token = s.get("bot_token")
        if not token:
            print("NO_TOKEN")
            return 1
        if state and state.get("owner_id") in owners:
            return _deliver_pending_welcome(token)
        if owners:
            clear_bind_state()
            print(f"ALREADY_BOUND:{sorted(owners)[0]}")
            return 0
        if state:
            clear_bind_state()  # stale claim whose owner file is gone

        wait_secs = max(0, int(wait_secs))
        print(f"BIND_WAITING:{wait_secs}", flush=True)
        deadline = time.time() + wait_secs
        buf = s.get("cursor", "")
        while time.time() < deadline:
            try:
                data = api("POST", "ilink/bot/getupdates",
                           {"get_updates_buf": buf,
                            "base_info": {"channel_version": "1.0.2"}},
                           token=token, timeout=40)
            except Exception as e:
                print(f"BIND_ERROR:{e}", file=sys.stderr, flush=True)
                time.sleep(min(2, max(0, deadline - time.time())))
                continue
            if data.get("code") == -14:
                print("TOKEN_EXPIRED")
                return 2
            new_buf = data.get("get_updates_buf") or buf
            fresh, new_ids = filter_seen(data.get("msgs") or [])
            winner = None
            winner_ctx = None
            for m in fresh:
                if m.get("message_type") != 1:
                    continue
                sender = m.get("from_user_id")
                if not sender:
                    continue
                if winner is None:
                    winner = sender
                    winner_ctx = m.get("context_token")
                elif sender == winner:
                    winner_ctx = m.get("context_token") or winner_ctx
                else:
                    # Same batch, different sender: too late — the first
                    # sender wins; this one joins the approval queue.
                    note_unknown_sender(sender)
            if winner is None:
                # Nothing claimable in this batch: commit it and keep
                # waiting, so the normal driver never replays it later.
                if new_buf != buf or new_ids:
                    if new_buf:
                        s["cursor"] = new_buf
                        save_session(s)
                    mark_seen(new_ids)
                    buf = new_buf
                continue
            # Claim first, welcome second: once owner.json exists the
            # window is closed to everyone else, whatever happens next.
            save_owner(winner)
            if winner_ctx:
                contacts = load_contacts()
                contacts[winner] = {"context_token": winner_ctx,
                                    "updated_at": int(time.time())}
                save_contacts(contacts)
            source_id = next(
                (str(m.get("message_id")) for m in fresh
                 if m.get("from_user_id") == winner
                 and m.get("message_id") is not None),
                str(int(time.time())))
            save_bind_state({
                "owner_id": winner,
                "context_token": winner_ctx,
                "claim_id": source_id,
                "claimed_at": int(time.time()),
                "welcome_sent": 0,
                "commit": {"cursor": new_buf, "ids": new_ids},
            })
            return _deliver_pending_welcome(token)
        print("BIND_TIMEOUT")
        return 3
    finally:
        _release_bind_lock(lock_fd)


def headers(token=None):
    uin = str(random.randint(0, 0xFFFFFFFF))
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": base64.b64encode(uin.encode()).decode(),
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def api(method, path, body=None, token=None, timeout=40):
    url = f"{BASE_URL}/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers(token), method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def load_session():
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE) as f:
                data = json.load(f)
        except Exception as e:
            # Same rule as load_contacts: a corrupt session file degrades
            # to "not logged in" (the caller's NO_TOKEN path) instead of
            # crashing the command with a traceback.
            print(f"WARNING: corrupt session file {SESSION_FILE}: {e}; "
                  "treating as empty", file=sys.stderr, flush=True)
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def save_session(s):
    os.makedirs(BRIDGE, exist_ok=True)
    tmp = SESSION_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SESSION_FILE)


def cmd_qrcode():
    data = api("GET", "ilink/bot/get_bot_qrcode?bot_type=3")
    qrcode = data["qrcode"]
    img = data.get("qrcode_img_content", "")
    if img.startswith("data:image/"):
        header, b64 = img.split(",", 1)
        m = re.search(r"data:image/(\w+)", header)
        ext = m.group(1) if m else "png"
        path = os.path.join(BRIDGE, f"qrcode.{ext}")
        os.makedirs(BRIDGE, exist_ok=True)
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        print(f"QR_IMAGE:{path}")
    elif img.startswith("http"):
        print(f"QR_URL:{img}")
    print(f"QR_CODE:{qrcode}")


def cmd_login(qrcode, wait_secs=300):
    deadline = time.time() + wait_secs
    while time.time() < deadline:
        try:
            st = api("GET", f"ilink/bot/get_qrcode_status?qrcode={qrcode}", timeout=35)
        except Exception as e:
            print(f"STATUS_ERROR:{e}", flush=True)
            time.sleep(3)
            continue
        status = st.get("status")
        print(f"SCAN_STATUS:{status}", flush=True)
        if status == "confirmed":
            s = load_session()
            s["bot_token"] = st["bot_token"]
            s["baseurl"] = st.get("baseurl", "")
            s["login_at"] = int(time.time())
            save_session(s)
            print("LOGIN_OK")
            return 0
        if status in ("expired", "canceled"):
            print(f"LOGIN_FAILED:{status}")
            return 1
        time.sleep(2)
    print("LOGIN_TIMEOUT")
    return 1


def cmd_poll():
    s = load_session()
    token = s.get("bot_token")
    if not token:
        print("NO_TOKEN")
        return 1
    buf = s.get("cursor", "")
    try:
        data = api("POST", "ilink/bot/getupdates",
                   {"get_updates_buf": buf, "base_info": {"channel_version": "1.0.2"}},
                   token=token, timeout=40)
    except Exception as e:
        print(f"POLL_ERROR:{e}")
        return 1
    if data.get("code") == -14:
        print("TOKEN_EXPIRED")
        return 2
    new_buf = data.get("get_updates_buf") or buf
    fresh, new_ids = filter_seen(data.get("msgs") or [])
    owners = load_owner_ids()
    filtered_senders = []
    msgs = []
    contacts = load_contacts()
    for m in fresh:
        if m.get("message_type") != 1:  # 只要用户发来的消息
            continue
        from_id = m.get("from_user_id")
        if from_id not in owners:
            # Owner allowlist (code-enforced): a stranger's message is
            # never queued, never wakes the worker and never lands in
            # the contacts cache. Its id stays in new_ids, so the commit
            # step still advances past it — the server will not redeliver
            # it and the queue cannot jam on it. The sender is noted in
            # unknown_senders.json for manual approval (e.g. the owner
            # messaging from a second account).
            if from_id:
                note_unknown_sender(from_id)
                filtered_senders.append(from_id)
            continue
        ctx = m.get("context_token")
        if from_id and ctx:
            contacts[from_id] = {"context_token": ctx, "updated_at": int(time.time())}
        for item in m.get("item_list") or []:
            t = item.get("type") or 0
            kind = ITEM_TYPE_NAMES.get(t, f"unknown_{t}")
            entry = {"from_user_id": from_id, "context_token": ctx, "kind": kind,
                     "message_id": m.get("message_id")}
            if t == 1:  # TEXT
                entry["text"] = (item.get("text_item") or {}).get("text", "")
            elif t == 3:  # VOICE: 可能带语音转写文本
                entry["text"] = (item.get("voice_item") or {}).get("text", "")
                entry["voice_duration_ms"] = (item.get("voice_item") or {}).get("playtime", 0)
                try:
                    p = download_media_item(item, t)
                    if p:
                        entry["media_path"] = p
                except Exception as e:
                    entry["media_error"] = str(e)[:100]
            elif t in (2, 4, 5):  # IMAGE / FILE / VIDEO
                if t == 4:
                    entry["file_name"] = (item.get("file_item") or {}).get("file_name", "")
                try:
                    p = download_media_item(item, t)
                    if p:
                        entry["media_path"] = p
                    else:
                        entry["media_error"] = "download failed"
                except Exception as e:
                    entry["media_error"] = str(e)[:100]
            else:
                continue
            msgs.append(entry)
    if contacts:
        save_contacts(contacts)
    # Two-phase commit: do NOT advance the cursor or record seen ids
    # here. The caller (hook script / daemon) queues the batch durably,
    # then submits the commit block below via `ilink.py commit`. A crash
    # before that simply re-fetches the same batch next poll — messages
    # may duplicate, but they are never lost.
    print(json.dumps({"msgs": msgs,
                      "filtered": {"dropped": len(filtered_senders),
                                   "senders": sorted(set(filtered_senders))},
                      "commit": {"cursor": new_buf, "ids": new_ids}},
                     ensure_ascii=False))
    return 0


def cmd_commit():
    """Commit one poll round: record delivered ids + advance the cursor.
    Reads the commit block from stdin: {"cursor": ..., "ids": [...]}.
    Call only after the batch is durably queued. Idempotent.

    Order matters: seen ids are persisted BEFORE the cursor. If the
    process dies between the two writes, the un-advanced cursor makes
    the server redeliver the batch, and the recorded seen ids filter
    the redelivery out — a duplicate poll, not a duplicate reply. The
    old order (cursor first) could lose the seen write and let a
    redelivery through as a fresh message."""
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        print(f"COMMIT_ERROR:bad stdin: {e}")
        return 1
    mark_seen(payload.get("ids") or [])
    cursor = payload.get("cursor")
    if cursor:
        s = load_session()
        s["cursor"] = cursor
        save_session(s)
    print("COMMIT_OK")
    return 0


def _send_raw(to_user_id, context_token, text, token, client_id=None):
    if client_id is None:
        client_id = f"muse-wechat-{random.randint(0, 0xFFFFFFFF):08x}"
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": 2,
            "message_state": 2,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        },
        "base_info": {"channel_version": "1.0.2"},
    }
    if context_token:
        body["msg"]["context_token"] = context_token
    return api("POST", "ilink/bot/sendmessage", body, token=token, timeout=40)


# --- Send audit log + idempotency ledger ------------------------------------
# The 2026-10-23 duplicate-send diagnosis showed the failure mode is a
# second, independent send invocation (fresh client_id), which server-side
# client_id dedupe cannot stop. The audit log makes the next incident
# provable in minutes; the ledger makes a repeated invocation with the
# same key a no-op. Neither file ever carries message text or tokens —
# only ids, outcomes and error type names.

def _audit_send(direction, to_user_id, client_id, result, error=None,
                attempt=1, elapsed_ms=None):
    """Append one JSON line to the send audit log. Never raises: a logging
    failure must not change the outcome of the send itself."""
    try:
        entry = {"ts": int(time.time()), "direction": direction,
                 "to": to_user_id, "client_id": client_id,
                 "result": result, "attempt": attempt}
        if error:
            entry["error"] = error
        if elapsed_ms is not None:
            entry["elapsed_ms"] = elapsed_ms
        with open(SEND_AUDIT_FILE, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        try:
            with open(SEND_AUDIT_FILE) as f:
                lines = f.readlines()
        except Exception:
            return
        if len(lines) > SEND_AUDIT_CAP:
            tmp = SEND_AUDIT_FILE + ".tmp"
            with open(tmp, "w") as f:
                f.writelines(lines[-SEND_AUDIT_CAP:])
            os.replace(tmp, SEND_AUDIT_FILE)
    except Exception:
        pass


class LedgerCorrupt(Exception):
    """Send ledger file exists but is unusable: unparseable JSON, a
    non-object top level, or unreadable for a reason other than
    'not there yet'. Callers must fail closed on this — never fall
    back to an empty ledger, because an empty ledger forgets every
    recorded success and the next retry of an old key would send with
    a fresh client_id the server cannot dedupe against."""


def _load_send_ledger():
    """key -> {client_id, at, result}. A missing file reads as empty
    (fresh deploy). Anything else unusable raises LedgerCorrupt.

    The corrupt file is deliberately left in place for operator
    inspection — do NOT auto-quarantine-and-continue: quarantining and
    then proceeding would make the very next send see an empty ledger
    again, which is exactly the silent fallback this refuses. Manual
    recovery: inspect the file, then move it away; the ledger rebuilds
    empty, and the old keys' dedupe history is lost with it — the
    accepted cost of a human intervening on a damaged file."""
    try:
        with open(SEND_LEDGER_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise LedgerCorrupt(f"send ledger unreadable: {e}")
    if not isinstance(data, dict):
        raise LedgerCorrupt("send ledger is not an object")
    return data


def _record_send_ledger(key, client_id):
    """Backwards-compatible alias: record a successful send under key.
    New code uses _mark_send_ledger_sent; both write the same entry,
    and both propagate a write failure instead of swallowing it."""
    _mark_send_ledger_sent(key, client_id)


def _ledger_entry_is_sent(ent):
    """A fresh 'sent' entry counts as delivered. Entries written before
    the intent upgrade carry no status field but result 'ok': those are
    legacy successes and count the same, so old ledger files stay valid."""
    if not ent:
        return False
    if ent.get("status") == "sent":
        return True
    return ent.get("status") is None and ent.get("result") == "ok"


def _ledger_entry_is_fresh(ent):
    try:
        return int(time.time()) - int(ent.get("at", 0)) < SEND_LEDGER_TTL
    except (TypeError, ValueError):
        return False


def _check_send_ledger(key):
    """Return the recorded entry for key if it is a fresh success, else None.
    Propagates LedgerCorrupt: no current caller uses this (sends go
    through _resolve_send_intent), but a corrupt ledger must never be
    reported as 'not sent' from any entry point."""
    if not key:
        return None
    ent = _load_send_ledger().get(key)
    if not _ledger_entry_is_sent(ent) or not _ledger_entry_is_fresh(ent):
        return None
    return ent


def _write_send_ledger(book):
    """Atomic ledger write (tmp + replace). Raises on failure, on
    purpose: a ledger write is a correctness gate, not a best-effort
    log — callers must not treat a failed write as success (a lost
    intent means a later retry gets a fresh client_id and the server
    dedupe can no longer cover it). tmp+replace already gives
    atomicity; no fsync by design (a torn write is impossible, a lost
    write is reported loudly instead)."""
    tmp = SEND_LEDGER_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(book, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SEND_LEDGER_FILE)


def _prune_send_ledger(book, now):
    # now is kept for call-site clarity; freshness itself is judged
    # against the current clock (a few seconds of skew is immaterial
    # next to a 24h TTL). Non-dict entries are dropped as malformed.
    return {k: v for k, v in book.items()
            if isinstance(v, dict) and _ledger_entry_is_fresh(v)}


_ledger_lock_state = threading.local()


@contextlib.contextmanager
def _send_ledger_lock():
    """Inter-process lock for a keyed send, held across check, intent
    write, API call and result marking — so two processes racing on the
    same key cannot both decide 'not sent yet'. Re-entrant within one
    thread (the decorator below and the helpers share it); across
    processes/threads fcntl serializes.

    Fail-closed, with one platform exception: where fcntl is
    unavailable (non-POSIX platforms) the lock is a no-op and only the
    intent/client_id reuse still applies — documented, not silent, and
    the only case that degrades. Where fcntl IS available but opening
    the lock file or flock fails, this raises instead of proceeding
    unlocked: an unlocked keyed send could race another process into
    the same 'not sent yet' decision the lock exists to prevent."""
    state = _ledger_lock_state
    if getattr(state, "depth", 0) > 0:
        state.depth += 1
        try:
            yield
        finally:
            state.depth -= 1
        return
    fd = None
    if fcntl is not None:
        try:
            fd = os.open(SEND_LEDGER_LOCK_FILE,
                         os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except Exception:
            if fd is not None:
                os.close(fd)
            raise
    state.depth = 1
    try:
        yield
    finally:
        state.depth = 0
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _keyed_send_locked(func):
    """Decorator: a call carrying a --key runs entirely inside the
    ledger lock; keyless sends are untouched (no lock, no behaviour
    change). The key is the function's `key` argument, positional or
    keyword. A lock-acquire failure aborts the send before any ledger
    read or API call: LOCK_ERROR, exit 1, audit row — never an
    unlocked send. (Only the acquire step is converted; exceptions
    from the send itself are the function's own to report, and
    release-step failures are best-effort — the lock was held for
    the whole critical section either way.)"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            bound = inspect.signature(func).bind_partial(*args, **kwargs)
            key = bound.arguments.get("key")
        except Exception:
            key = kwargs.get("key")
        if not key:
            return func(*args, **kwargs)
        lock = _send_ledger_lock()
        try:
            lock.__enter__()
        except Exception as e:
            direction = {"cmd_send": "send", "cmd_proactive": "proactive",
                         "cmd_send_media": "media",
                         "cmd_proactive_media": "media"}.get(
                             func.__name__, "send")
            to_user_id = args[0] if args else None
            _audit_send(direction, to_user_id, None, "lock_error",
                        error=type(e).__name__)
            print(f"LOCK_ERROR:{e}")
            return 1
        try:
            return func(*args, **kwargs)
        finally:
            try:
                lock.__exit__(None, None, None)
            except Exception:
                pass
    return wrapper


def _resolve_send_intent(key):
    """Under the ledger lock: return (client_id, already_sent).

    First call for a key persists an intent {client_id, at, status}
    BEFORE any API call; a later call with the same key reuses that
    client_id. Only a confirmed success flips the entry to 'sent'.
    Application failures and exceptions leave the intent in place, so a
    retry reuses the same client_id — and the server dedupes by
    client_id (verified live 2026-10-23), so a 'sent but unconfirmed'
    retry does not deliver twice. One accepted semantic: if a retry
    carries different text under the same key, the user keeps at most
    the first text, because the deduped retry never re-delivers.
    Raises if the intent itself cannot be persisted: the caller must
    abort BEFORE any API call, or the send would go out with a
    client_id no retry could ever reuse."""
    now = int(time.time())
    book = _load_send_ledger()
    ent = book.get(key)
    if ent and _ledger_entry_is_fresh(ent):
        if _ledger_entry_is_sent(ent):
            return ent.get("client_id"), True
        if ent.get("status") == "intent" and ent.get("client_id"):
            return ent["client_id"], False
    client_id = f"muse-wechat-{random.randint(0, 0xFFFFFFFF):08x}"
    book = _prune_send_ledger(book, now)
    book[key] = {"client_id": client_id, "at": now, "status": "intent"}
    _write_send_ledger(book)
    return client_id, False


def _mark_send_ledger_sent(key, client_id):
    """Flip a key's intent to 'sent' after a confirmed success.
    Raises on write failure, and also on a corrupt ledger load (the
    load happens here too, so a damaged file surfaces through the
    same caller path): either way the caller reports LEDGER_WRITE_FAILED,
    not success, per the unconfirmed-send semantics below.
    Raises on write failure: the caller must NOT report success then,
    because the ledger still says 'intent'. The caller reports the
    send as unconfirmed (nonzero exit, no SEND_OK), so its driver does
    not commit the batch and a later retry reuses the same client_id —
    and the server's client_id dedupe then covers the resend, so the
    user still receives exactly one message. A silent success here
    would be worse than a loud retry: the next invocation would send
    again believing nothing was recorded, and only the dedupe would
    (usually) save it."""
    now = int(time.time())
    book = _prune_send_ledger(_load_send_ledger(), now)
    book[key] = {"client_id": client_id, "at": now,
                 "status": "sent", "result": "ok"}
    _write_send_ledger(book)


def _classify_send_response(data, require_message_id=False):
    """Shared send/proactive outcome rule: 'ok' | 'token_expired' | 'app_error'.
    code -14 is always token expiry; code != 0 is an application failure.
    When require_message_id is set (media sends), a code-0 response with
    no message_id is also an application failure, not a success."""
    code = data.get("code", 0)
    if code == -14:
        return "token_expired"
    if code != 0:
        return "app_error"
    if require_message_id and not data.get("message_id"):
        return "app_error"
    return "ok"


def _proactive_needs_tokenless_retry(data, require_message_id=False):
    """proactive's retry-without-token trigger, on top of the shared rule:
    any non-ok outcome, or a ret == -2 / 'prepare failed' response even
    when code == 0 (the stale-context_token signature)."""
    if _classify_send_response(data, require_message_id) != "ok":
        return True
    if data.get("ret") == -2:
        return True
    return "prepare failed" in str(data.get("errmsg", ""))


@_keyed_send_locked
def cmd_proactive(to_user_id, text, key=None):
    """主动推送：先用缓存的 context_token，失败则去掉 token 重试一次。"""
    s = load_session()
    token = s.get("bot_token")
    if not token:
        print("NO_TOKEN")
        return 1
    if text == "-":
        text = sys.stdin.read()
    if key:
        try:
            client_id, already_sent = _resolve_send_intent(key)
        except LedgerCorrupt as e:
            # Ledger unusable: fail closed BEFORE any API call.
            # Falling back to an empty ledger here would forget
            # recorded successes and let a retry resend under a
            # fresh client_id the server cannot dedupe against.
            _audit_send("proactive", to_user_id, None, "ledger_error",
                        error=type(e).__name__)
            print(f"LEDGER_CORRUPT:{e}")
            return 1
        except Exception as e:
            # Intent not persisted: abort BEFORE any API call. Sending
            # now would use a client_id no retry could reuse.
            _audit_send("proactive", to_user_id, None, "ledger_error",
                        error=type(e).__name__)
            print(f"LEDGER_ERROR:{e}")
            return 1
        if already_sent:
            print("SEND_ALREADY_SENT")
            return 0
    else:
        client_id = f"muse-wechat-{random.randint(0, 0xFFFFFFFF):08x}"
    contacts = load_contacts()
    ctx = (contacts.get(to_user_id) or {}).get("context_token")
    if not ctx:
        print("NO_CONTEXT_TOKEN")
        return 1
    try:
        t0 = time.time()
        data = _send_raw(to_user_id, ctx, text, token, client_id=client_id)
    except Exception as e:
        _audit_send("proactive", to_user_id, client_id, "exception",
                    error=type(e).__name__, attempt=1)
        print(f"SEND_ERROR:{e}")
        return 1
    retry = _proactive_needs_tokenless_retry(data)
    _audit_send("proactive", to_user_id, client_id,
                "app_error" if retry else "ok",
                elapsed_ms=int((time.time() - t0) * 1000), attempt=1)
    if _classify_send_response(data) == "token_expired":
        print("TOKEN_EXPIRED")
        return 2
    if retry:
        # context_token 可能过期，去掉 token 重试一次
        print(f"RETRY_WITHOUT_TOKEN:{str(data.get('errmsg', ''))[:80]}", flush=True)
        try:
            t0 = time.time()
            data = _send_raw(to_user_id, None, text, token, client_id=client_id)
        except Exception as e:
            _audit_send("proactive", to_user_id, client_id, "exception",
                        error=type(e).__name__, attempt=2)
            print(f"SEND_ERROR:{e}")
            return 1
        _audit_send("proactive", to_user_id, client_id,
                    "ok" if data.get("code", 0) == 0 else "app_error",
                    elapsed_ms=int((time.time() - t0) * 1000), attempt=2)
        code = data.get("code", 0)
        if code == -14:
            print("TOKEN_EXPIRED")
            return 2
        if code != 0:
            print(f"PROACTIVE_FAILED:{str(data)[:200]}")
            return 1
    if key:
        try:
            _mark_send_ledger_sent(key, client_id)
        except Exception as e:
            # Message may already be delivered, but the ledger cannot
            # confirm it: report unconfirmed, not success. The driver
            # will not commit the batch, and a retry reuses the same
            # client_id, which the server dedupes — one delivery.
            _audit_send("proactive", to_user_id, client_id, "ledger_error",
                        error=type(e).__name__)
            print(f"LEDGER_WRITE_FAILED:{e}")
            return 1
    print("PROACTIVE_OK")
    return 0


@_keyed_send_locked
def cmd_send(to_user_id, context_token, text, key=None):
    """发送回复。网络层失败时用相同 client_id 最多重试一次；应用层明确失败不重试。
    key 不为空时：账本已有该 key 的成功记录直接返回已发送，不再调用 API；
    否则先落意图（含固定 client_id）再发送，成功后标记已发送——重试复用
    同一 client_id，服务端去重兜底，崩溃窗口也不重复投递。"""
    s = load_session()
    token = s.get("bot_token")
    if not token:
        print("NO_TOKEN")
        return 1
    if text == "-":
        text = sys.stdin.read()
    if key:
        try:
            client_id, already_sent = _resolve_send_intent(key)
        except LedgerCorrupt as e:
            # Ledger unusable: fail closed BEFORE any API call.
            # Falling back to an empty ledger here would forget
            # recorded successes and let a retry resend under a
            # fresh client_id the server cannot dedupe against.
            _audit_send("send", to_user_id, None, "ledger_error",
                        error=type(e).__name__)
            print(f"LEDGER_CORRUPT:{e}")
            return 1
        except Exception as e:
            # Intent not persisted: abort BEFORE any API call. Sending
            # now would use a client_id no retry could reuse.
            _audit_send("send", to_user_id, None, "ledger_error",
                        error=type(e).__name__)
            print(f"LEDGER_ERROR:{e}")
            return 1
        if already_sent:
            print("SEND_ALREADY_SENT")
            return 0
    else:
        client_id = f"muse-wechat-{random.randint(0, 0xFFFFFFFF):08x}"
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": 2,
            "message_state": 2,
            "context_token": context_token,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        },
        "base_info": {"channel_version": "1.0.2"},
    }
    for attempt in (1, 2):
        try:
            t0 = time.time()
            data = api("POST", "ilink/bot/sendmessage", body, token=token, timeout=40)
        except Exception as e:
            _audit_send("send", to_user_id, client_id, "exception",
                        error=type(e).__name__, attempt=attempt)
            if attempt == 1:
                time.sleep(5)
                continue
            print(f"SEND_ERROR:{e}")
            return 1
        outcome = _classify_send_response(data)
        _audit_send("send", to_user_id, client_id,
                    "ok" if outcome == "ok" else "app_error",
                    elapsed_ms=int((time.time() - t0) * 1000), attempt=attempt)
        if outcome == "token_expired":
            print("TOKEN_EXPIRED")
            return 2
        if outcome != "ok":
            print(f"SEND_FAILED:{str(data)[:200]}")
            return 1
        if key:
            try:
                _mark_send_ledger_sent(key, client_id)
            except Exception as e:
                # Message may already be delivered, but the ledger
                # cannot confirm it: report unconfirmed, not success.
                # The driver will not commit the batch, and a retry
                # reuses the same client_id, which the server dedupes.
                _audit_send("send", to_user_id, client_id, "ledger_error",
                            error=type(e).__name__, attempt=attempt)
                print(f"LEDGER_WRITE_FAILED:{e}")
                return 1
        print("SEND_OK")
        return 0


def _typing_ticket(to_user_id, token, force=False):
    """取（或刷新）某用户的 typing_ticket，缓存在 contacts 里（TTL 9 分钟）。"""
    contacts = load_contacts()
    entry = contacts.get(to_user_id) or {}
    now = int(time.time())
    if (not force and entry.get("typing_ticket")
            and now - entry.get("typing_ticket_at", 0) < 540):
        return entry["typing_ticket"]
    ctx = entry.get("context_token")
    if not ctx:
        return None
    data = api("POST", "ilink/bot/getconfig",
               {"ilink_user_id": to_user_id, "context_token": ctx,
                "base_info": {"channel_version": "1.0.2"}},
               token=token, timeout=20)
    ticket = data.get("typing_ticket")
    if ticket:
        entry["typing_ticket"] = ticket
        entry["typing_ticket_at"] = now
        contacts[to_user_id] = entry
        save_contacts(contacts)
    return ticket


def cmd_typing(to_user_id, status_word):
    """显示/取消"正在输入"状态：status_word = start | stop。这是装饰性功能，
    失败绝不影响正常回复流程（调用方应忽略失败）。"""
    status = {"start": 1, "stop": 2}.get(status_word)
    if status is None:
        print("TYPING_BAD_STATUS")
        return 2
    s = load_session()
    token = s.get("bot_token")
    if not token:
        print("NO_TOKEN")
        return 1
    try:
        ticket = _typing_ticket(to_user_id, token)
        if not ticket:
            print("NO_TYPING_TICKET")
            return 1
        data = api("POST", "ilink/bot/sendtyping",
                   {"ilink_user_id": to_user_id, "typing_ticket": ticket,
                    "status": status,
                    "base_info": {"channel_version": "1.0.2"}},
                   token=token, timeout=20)
        if data.get("ret", 0) != 0:
            # ticket 可能已失效，强制刷新后重试一次
            ticket = _typing_ticket(to_user_id, token, force=True)
            data = api("POST", "ilink/bot/sendtyping",
                       {"ilink_user_id": to_user_id, "typing_ticket": ticket,
                        "status": status,
                        "base_info": {"channel_version": "1.0.2"}},
                       token=token, timeout=20)
            if data.get("ret", 0) != 0:
                print(f"TYPING_FAILED:{str(data)[:200]}")
                return 1
    except Exception as e:
        print(f"TYPING_ERROR:{e}")
        return 1
    print("TYPING_OK")
    return 0


def _aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7
    padder = PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(padded) + enc.finalize()


def _send_item_raw(to_user_id, context_token, item, token, client_id=None):
    """sendmessage 的通用封装：item 是任意 MessageItem（文本/图片/文件）。"""
    if client_id is None:
        client_id = f"muse-wechat-{random.randint(0, 0xFFFFFFFF):08x}"
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": 2,
            "message_state": 2,
            "item_list": [item],
        },
        "base_info": {"channel_version": "1.0.2"},
    }
    if context_token:
        body["msg"]["context_token"] = context_token
    return api("POST", "ilink/bot/sendmessage", body, token=token, timeout=40)


def _upload_media(to_user_id, data, media_type, token):
    """把媒体字节加密上传到微信 CDN。返回构造消息 item 所需的字段 dict。
    协议要点（实测）：getuploadurl 的 aeskey 传 hex 字符串；消息里
    CDNMedia.aes_key 用 base64(hex 字符串)；CDN 上传响应头 x-encrypted-param
    即下载参数。"""
    import hashlib
    from urllib.parse import quote
    key = os.urandom(16)
    aeskey_hex = key.hex()
    filekey = "%032x" % random.getrandbits(128)
    ciphertext = _aes_ecb_encrypt(data, key)
    # 注意：getuploadurl 的 media_type 是另一套枚举（1=IMG, 2=VID, 3=FILE, 4=VOICE），
    # 与消息 item 类型（2=IMAGE, 4=FILE）不同，必须映射。
    upload_type = {2: 1, 3: 4, 4: 3, 5: 2}[media_type]
    body = {
        "filekey": filekey,
        "media_type": upload_type,
        "to_user_id": to_user_id,
        "rawsize": len(data),
        "rawfilemd5": hashlib.md5(data).hexdigest(),
        "filesize": len(ciphertext),
        "aeskey": aeskey_hex,
        "no_need_thumb": True,
        "base_info": {"channel_version": "1.0.2"},
    }
    r = api("POST", "ilink/bot/getuploadurl", body, token=token, timeout=20)
    upload_param = r.get("upload_param")
    if not upload_param:
        raise RuntimeError(f"getuploadurl no upload_param: {str(r)[:200]}")
    url = (f"{CDN_BASE_URL}/upload?encrypted_query_param="
           f"{quote(upload_param, safe='')}&filekey={filekey}")
    req = urllib.request.Request(url, data=ciphertext, method="POST",
                                 headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        download_param = resp.headers.get("x-encrypted-param")
    if not download_param:
        raise RuntimeError("CDN upload: no x-encrypted-param header")
    return {
        "media": {"encrypt_query_param": download_param,
                  "aes_key": base64.b64encode(aeskey_hex.encode("ascii")).decode("ascii"),
                  "encrypt_type": 1},
        "md5": body["rawfilemd5"],
        "rawsize": len(data),
        "cipher_size": len(ciphertext),
    }


def _media_item(path, up, media_type, playtime_ms=0):
    if media_type == 2:  # IMAGE
        return {"type": 2, "image_item": {"media": up["media"],
                                          "mid_size": up["cipher_size"]}}
    if media_type == 3:  # VOICE (experimental): audio must be SILK@24kHz
        # NOTE: the iLink channel silently drops outbound voice items —
        # the API returns success but the client never shows them
        # (verified live 2026-10-22; other SDKs document the same).
        # Kept for completeness; send audio as FILE (wav) instead.
        return {"type": 3, "voice_item": {"media": up["media"],
                                          "encode_type": 6,
                                          "bits_per_sample": 16,
                                          "sample_rate": 24000,
                                          "playtime": int(playtime_ms)}}
    if media_type == 5:  # VIDEO (thumb optional: getuploadurl says no_need_thumb)
        return {"type": 5, "video_item": {"media": up["media"],
                                          "video_size": up["cipher_size"]}}
    # FILE
    return {"type": 4, "file_item": {"media": up["media"],
                                     "file_name": os.path.basename(path),
                                     "md5": up["md5"],
                                     "len": str(up["rawsize"])}}


@_keyed_send_locked
def cmd_send_media(to_user_id, context_token, path, media_type, playtime_ms=0, key=None):
    """发送图片/视频/文件（回复模式，需 context_token）。"""
    s = load_session()
    token = s.get("bot_token")
    if not token:
        print("NO_TOKEN")
        return 1
    if key:
        try:
            client_id, already_sent = _resolve_send_intent(key)
        except LedgerCorrupt as e:
            # Ledger unusable: fail closed BEFORE any API call.
            # Falling back to an empty ledger here would forget
            # recorded successes and let a retry resend under a
            # fresh client_id the server cannot dedupe against.
            _audit_send("media", to_user_id, None, "ledger_error",
                        error=type(e).__name__)
            print(f"LEDGER_CORRUPT:{e}")
            return 1
        except Exception as e:
            # Intent not persisted: abort BEFORE any read or API call.
            _audit_send("media", to_user_id, None, "ledger_error",
                        error=type(e).__name__)
            print(f"LEDGER_ERROR:{e}")
            return 1
        if already_sent:
            print("SEND_ALREADY_SENT")
            return 0
    try:
        if os.path.getsize(path) > MEDIA_MAX_BYTES:
            print(f"MEDIA_TOO_LARGE:{os.path.basename(path)} exceeds "
                  f"{MEDIA_MAX_BYTES} bytes, refusing to read it fully "
                  f"into memory")
            return 1
    except OSError as e:
        print(f"MEDIA_READ_ERROR:{e}")
        return 1
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception as e:
        print(f"MEDIA_READ_ERROR:{e}")
        return 1
    if not key:
        client_id = f"muse-wechat-{random.randint(0, 0xFFFFFFFF):08x}"
    try:
        up = _upload_media(to_user_id, data, media_type, token)
        item = _media_item(path, up, media_type, playtime_ms)
        t0 = time.time()
        data_resp = _send_item_raw(to_user_id, context_token, item, token,
                                   client_id=client_id)
    except Exception as e:
        _audit_send("media", to_user_id, client_id, "exception",
                    error=type(e).__name__)
        print(f"SEND_ERROR:{e}")
        return 1
    outcome = _classify_send_response(data_resp, require_message_id=True)
    _audit_send("media", to_user_id, client_id,
                "ok" if outcome == "ok" else "app_error",
                elapsed_ms=int((time.time() - t0) * 1000))
    if outcome == "token_expired":
        print("TOKEN_EXPIRED")
        return 2
    if outcome != "ok":
        print(f"SEND_FAILED:{str(data_resp)[:200]}")
        return 1
    if key:
        try:
            _mark_send_ledger_sent(key, client_id)
        except Exception as e:
            # Message may already be delivered, but the ledger cannot
            # confirm it: report unconfirmed, not success. The driver
            # will not commit the batch, and a retry reuses the same
            # client_id, which the server dedupes.
            _audit_send("media", to_user_id, client_id, "ledger_error",
                        error=type(e).__name__)
            print(f"LEDGER_WRITE_FAILED:{e}")
            return 1
    print("SEND_OK")
    return 0


@_keyed_send_locked
def cmd_proactive_media(to_user_id, path, media_type, playtime_ms=0, key=None):
    """主动推送图片/视频/文件：用缓存的 context_token，失败则去掉 token 重试一次。"""
    s = load_session()
    token = s.get("bot_token")
    if not token:
        print("NO_TOKEN")
        return 1
    if key:
        try:
            client_id, already_sent = _resolve_send_intent(key)
        except LedgerCorrupt as e:
            # Ledger unusable: fail closed BEFORE any API call.
            # Falling back to an empty ledger here would forget
            # recorded successes and let a retry resend under a
            # fresh client_id the server cannot dedupe against.
            _audit_send("media", to_user_id, None, "ledger_error",
                        error=type(e).__name__)
            print(f"LEDGER_CORRUPT:{e}")
            return 1
        except Exception as e:
            # Intent not persisted: abort BEFORE any read or API call.
            _audit_send("media", to_user_id, None, "ledger_error",
                        error=type(e).__name__)
            print(f"LEDGER_ERROR:{e}")
            return 1
        if already_sent:
            print("SEND_ALREADY_SENT")
            return 0
    contacts = load_contacts()
    ctx = (contacts.get(to_user_id) or {}).get("context_token")
    if not ctx:
        print("NO_CONTEXT_TOKEN")
        return 1
    if not key:
        client_id = f"muse-wechat-{random.randint(0, 0xFFFFFFFF):08x}"
    try:
        if os.path.getsize(path) > MEDIA_MAX_BYTES:
            print(f"MEDIA_TOO_LARGE:{os.path.basename(path)} exceeds "
                  f"{MEDIA_MAX_BYTES} bytes, refusing to read it fully "
                  f"into memory")
            return 1
    except OSError as e:
        print(f"MEDIA_READ_ERROR:{e}")
        return 1
    try:
        with open(path, "rb") as f:
            data = f.read()
        up = _upload_media(to_user_id, data, media_type, token)
        item = _media_item(path, up, media_type, playtime_ms)
        t0 = time.time()
        data_resp = _send_item_raw(to_user_id, ctx, item, token, client_id=client_id)
    except Exception as e:
        _audit_send("media", to_user_id, client_id, "exception",
                    error=type(e).__name__, attempt=1)
        print(f"SEND_ERROR:{e}")
        return 1
    outcome = _classify_send_response(data_resp, require_message_id=True)
    _audit_send("media", to_user_id, client_id,
                "ok" if outcome == "ok" else "app_error",
                elapsed_ms=int((time.time() - t0) * 1000), attempt=1)
    if outcome == "token_expired":
        print("TOKEN_EXPIRED")
        return 2
    if outcome != "ok":
        try:
            t0 = time.time()
            data_resp = _send_item_raw(to_user_id, None, item, token,
                                       client_id=client_id)
        except Exception as e:
            _audit_send("media", to_user_id, client_id, "exception",
                        error=type(e).__name__, attempt=2)
            print(f"SEND_ERROR:{e}")
            return 1
        outcome = _classify_send_response(data_resp, require_message_id=True)
        _audit_send("media", to_user_id, client_id,
                    "ok" if outcome == "ok" else "app_error",
                    elapsed_ms=int((time.time() - t0) * 1000), attempt=2)
        if outcome == "token_expired":
            print("TOKEN_EXPIRED")
            return 2
        if outcome != "ok":
            print(f"PROACTIVE_FAILED:{str(data_resp)[:200]}")
            return 1
    if key:
        try:
            _mark_send_ledger_sent(key, client_id)
        except Exception as e:
            # Message may already be delivered, but the ledger cannot
            # confirm it: report unconfirmed, not success. The driver
            # will not commit the batch, and a retry reuses the same
            # client_id, which the server dedupes.
            _audit_send("media", to_user_id, client_id, "ledger_error",
                        error=type(e).__name__)
            print(f"LEDGER_WRITE_FAILED:{e}")
            return 1
    print("PROACTIVE_OK")
    return 0


def _fmt_age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def cmd_health(offline=False):
    """Self-check: auth, live token probe, message queue, last poll error.
    Exit code 0 = healthy, 1 = warnings, 2 = broken."""
    now = int(time.time())
    lines, warnings, problems = [], [], []
    s = load_session()
    token = s.get("bot_token")
    if not token:
        problems.append("not logged in: no bot_token in session, QR login needed")
    elif s.get("login_at"):
        lines.append(f"auth: logged in (QR scanned {_fmt_age(now - s['login_at'])} ago)")
    else:
        lines.append("auth: logged in (login time unknown)")

    owner = None
    try:
        with open(os.path.join(BRIDGE, "owner.json")) as f:
            owner = json.load(f).get("owner_id")
    except Exception:
        pass

    # Live probe: getconfig for a typing ticket — no side effects, does not
    # touch the message cursor.
    if token and not offline:
        ctx = (load_contacts().get(owner) or {}).get("context_token") if owner else None
        if not ctx:
            lines.append("probe: skipped (no cached owner context_token)")
        else:
            try:
                data = api("POST", "ilink/bot/getconfig",
                           {"ilink_user_id": owner, "context_token": ctx,
                            "base_info": {"channel_version": "1.0.2"}},
                           token=token, timeout=12)
                if data.get("typing_ticket"):
                    lines.append("probe: token valid (getconfig OK)")
                elif data.get("code") == -14:
                    problems.append("token expired (getconfig code -14), re-scan needed")
                else:
                    warnings.append(f"probe result unclear: {str(data)[:80]}")
            except Exception as e:
                warnings.append(f"probe failed (network?): {type(e).__name__}")
    elif token:
        lines.append("probe: skipped (--offline)")

    # Message queue (hook state dir — same canonical variable and default
    # as the example hook script; ILINK_HOOK_STATE is a legacy alias)
    state_dir = (os.environ.get("ILINK_HOOK_STATE_DIR")
                 or os.environ.get("ILINK_HOOK_STATE")
                 or os.path.expanduser("~/hooks/state"))
    pend = os.path.join(state_dir, "ilink-pending.json")
    proc = os.path.join(state_dir, "ilink-processing.json")

    def _queue_count(path):
        """(count, corrupt): a queue file that exists but does not
        parse is corrupt — never report it as empty, or a stuck batch
        looks like a drained queue. Same strict shape rule as the hook
        script's queue_count: top level must be an object and .msgs
        must be an array. A parseable-but-misshapen file (e.g.
        {"msgs": "invalid"}) is corrupt too — counting a string's
        length as a message count used to report phantom backlog."""
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return 0, False
        except Exception:
            return 0, True
        if not isinstance(data, dict) or not isinstance(data.get("msgs"), list):
            return 0, True
        return len(data["msgs"]), False

    pend_n, pend_corrupt = _queue_count(pend)
    if pend_corrupt:
        warnings.append(
            f"pending queue is corrupt (unparseable JSON): {pend} — "
            "the hook quarantines it on its next run; inspect it there")
    elif pend_n > 0:
        lines.append(f"queue: {pend_n} message(s) pending")
    else:
        lines.append("queue: nothing pending")
    if os.path.exists(proc):
        claimed_age = now - int(os.stat(proc).st_ctime)  # ctime = claim time
        if claimed_age > 600:
            warnings.append(
                f"processing claimed {_fmt_age(claimed_age)} ago, still not done "
                "(worker may be stuck)")
        else:
            lines.append(f"queue: processing in flight ({_fmt_age(claimed_age)})")
    else:
        lines.append("queue: nothing processing")

    try:
        lines.append(f"dedupe: {len(load_seen())} message ids recorded")
    except Exception:
        pass

    lines.append(f"owners: {len(load_owner_ids())} allowlisted")
    try:
        with open(os.path.join(BRIDGE, "unknown_senders.json")) as f:
            unknown = json.load(f)
        if unknown:
            lines.append(f"unknown senders: {len(unknown)} awaiting approval "
                         "(unknown_senders.json)")
    except Exception:
        pass
    try:
        _load_send_ledger()
    except LedgerCorrupt:
        # Keyed sends fail closed while the ledger is damaged, so this
        # is a warning, not a snapshot detail: the file is left in
        # place for inspection, and sends resume only once an operator
        # has inspected or moved it away.
        warnings.append(
            "send ledger is corrupt (unparseable or not an object): "
            f"{SEND_LEDGER_FILE} — keyed sends abort until it is "
            "inspected or moved away")

    poll_log = os.path.join(BRIDGE, "poll.log")

    poll_log = os.path.join(BRIDGE, "poll.log")
    if os.path.exists(poll_log):
        try:
            with open(poll_log) as f:
                tail = [ln.strip() for ln in f if ln.strip()]
            if tail:
                lines.append(f"poll: last error {tail[-1][:90]}")
        except Exception:
            pass

    for w in warnings:
        lines.append(f"WARN: {w}")
    for p in problems:
        lines.append(f"BAD: {p}")
    if problems:
        lines.append("verdict: BAD")
        print("\n".join(lines))
        return 2
    if warnings:
        lines.append("verdict: WARNINGS")
        print("\n".join(lines))
        return 1
    lines.append("verdict: OK")
    print("\n".join(lines))
    return 0


def _pop_key(args):
    """Extract a trailing '--key <id>' pair from an argv tail.
    Returns (key_or_None, remaining_args). A '--key' with no following
    value is ignored rather than crashing."""
    rest = list(args)
    key = None
    if "--key" in rest:
        i = rest.index("--key")
        if i + 1 < len(rest):
            key = rest[i + 1]
            del rest[i:i + 2]
    return key, rest


def _need(argv, n, usage):
    """Arg-count guard: missing args print usage to stderr and exit 2
    instead of raising a bare IndexError traceback."""
    if len(argv) < n:
        print(f"usage: {usage}", file=sys.stderr)
        return False
    return True


def main(argv):
    """CLI dispatch. Returns an exit code (does not call sys.exit itself,
    so tests can call it directly)."""
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "qrcode":
        cmd_qrcode()
        return 0
    if cmd == "login":
        if not _need(argv, 3, "ilink.py login <qrcode> [wait_secs]"):
            return 2
        return cmd_login(argv[2], int(argv[3]) if len(argv) > 3 else 300)
    if cmd == "bind":
        return cmd_bind(int(argv[2]) if len(argv) > 2 else BIND_DEFAULT_SECONDS)
    if cmd == "poll":
        return cmd_poll()
    if cmd == "commit":
        return cmd_commit()
    if cmd == "health":
        return cmd_health(offline="--offline" in argv)
    if cmd == "send":
        if not _need(argv, 5, "ilink.py send <to> <ctx> <text> [--key <id>]"):
            return 2
        key, _ = _pop_key(argv[5:])
        return cmd_send(argv[2], argv[3], argv[4], key=key)
    if cmd == "proactive":
        if not _need(argv, 4, "ilink.py proactive <to> <text> [--key <id>]"):
            return 2
        key, _ = _pop_key(argv[4:])
        return cmd_proactive(argv[2], argv[3], key=key)
    if cmd == "typing":
        if not _need(argv, 4, "ilink.py typing <to> <start|stop>"):
            return 2
        return cmd_typing(argv[2], argv[3])
    if cmd in ("send-image", "send-file", "send-video"):
        if not _need(argv, 5, f"ilink.py {cmd} <to> <ctx> <path> [--key <id>]"):
            return 2
        key, _ = _pop_key(argv[5:])
        return cmd_send_media(argv[2], argv[3], argv[4],
                              {"send-image": 2, "send-file": 4,
                               "send-video": 5}[cmd], key=key)
    if cmd == "send-voice":
        if not _need(argv, 5, "ilink.py send-voice <to> <ctx> <path> [playtime_ms] [--key <id>]"):
            return 2
        key, rest = _pop_key(argv[5:])
        playtime = int(rest[0]) if rest else 0
        return cmd_send_media(argv[2], argv[3], argv[4], 3, playtime, key=key)
    if cmd in ("proactive-image", "proactive-file", "proactive-video"):
        if not _need(argv, 4, f"ilink.py {cmd} <to> <path> [--key <id>]"):
            return 2
        key, _ = _pop_key(argv[4:])
        return cmd_proactive_media(argv[2], argv[3],
                                   {"proactive-image": 2, "proactive-file": 4,
                                    "proactive-video": 5}[cmd], key=key)
    if cmd == "proactive-voice":
        if not _need(argv, 4, "ilink.py proactive-voice <to> <path> [playtime_ms] [--key <id>]"):
            return 2
        key, rest = _pop_key(argv[4:])
        playtime = int(rest[0]) if rest else 0
        return cmd_proactive_media(argv[2], argv[3], 3, playtime, key=key)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
