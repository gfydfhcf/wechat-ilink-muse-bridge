#!/usr/bin/env python3
"""iLink WeChat bot client (official Tencent protocol, no OpenClaw needed).

Usage:
  ilink.py qrcode              # get login QR, save image, print QR_CODE:<id>
  ilink.py status <qrcode>     # poll scan status once (JSON)
  ilink.py login <qrcode>      # poll until confirmed (up to 300s), save token
  ilink.py poll                # fetch new messages since cursor (JSON; two-phase: see commit)
  ilink.py commit              # commit a poll round (commit block via stdin): cursor + seen ids
  ilink.py send <to> <ctx> <text>  # send text reply (use - for stdin)
  ilink.py proactive <to> <text>   # proactive push via cached context_token
  ilink.py typing <to> <start|stop>  # show/cancel the "typing..." indicator
  ilink.py send-image <to> <ctx> <path>   # reply with an image
  ilink.py send-file <to> <ctx> <path>    # reply with a file
  ilink.py send-video <to> <ctx> <path>   # reply with a video
  ilink.py send-voice <to> <ctx> <path> <playtime_ms>   # voice msg (SILK@24k; NOTE: channel silently drops outbound voice)
  ilink.py proactive-image <to> <path>    # proactive image via cached ctx
  ilink.py proactive-file <to> <path>     # proactive file via cached ctx
  ilink.py health [--offline]  # self-check: auth, queue, last poll (exit 0 ok / 1 warn / 2 bad)
  ilink.py proactive-video <to> <path>    # proactive video via cached ctx
  ilink.py proactive-voice <to> <path> <playtime_ms>   # proactive voice (dropped by channel, see send-voice)
"""
import base64
import json
import os
import random
import re
import sys
import time
import urllib.request

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
        with open(CONTACTS_FILE) as f:
            return json.load(f)
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
        with open(SESSION_FILE) as f:
            return json.load(f)
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
    """Commit one poll round: advance the cursor + record delivered ids.
    Reads the commit block from stdin: {"cursor": ..., "ids": [...]}.
    Call only after the batch is durably queued. Idempotent."""
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        print(f"COMMIT_ERROR:bad stdin: {e}")
        return 1
    cursor = payload.get("cursor")
    if cursor:
        s = load_session()
        s["cursor"] = cursor
        save_session(s)
    mark_seen(payload.get("ids") or [])
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


def cmd_proactive(to_user_id, text):
    """主动推送：先用缓存的 context_token，失败则去掉 token 重试一次。"""
    s = load_session()
    token = s.get("bot_token")
    if not token:
        print("NO_TOKEN")
        return 1
    if text == "-":
        text = sys.stdin.read()
    contacts = load_contacts()
    ctx = (contacts.get(to_user_id) or {}).get("context_token")
    if not ctx:
        print("NO_CONTEXT_TOKEN")
        return 1
    try:
        data = _send_raw(to_user_id, ctx, text, token)
    except Exception as e:
        print(f"SEND_ERROR:{e}")
        return 1
    code = data.get("code", 0)
    ret = data.get("ret", 0)
    errmsg = str(data.get("errmsg", ""))
    if code == -14:
        print("TOKEN_EXPIRED")
        return 2
    if code != 0 or ret == -2 or "prepare failed" in errmsg:
        # context_token 可能过期，去掉 token 重试一次
        print(f"RETRY_WITHOUT_TOKEN:{errmsg[:80]}", flush=True)
        try:
            data = _send_raw(to_user_id, None, text, token)
        except Exception as e:
            print(f"SEND_ERROR:{e}")
            return 1
        code = data.get("code", 0)
        if code == -14:
            print("TOKEN_EXPIRED")
            return 2
        if code != 0:
            print(f"PROACTIVE_FAILED:{str(data)[:200]}")
            return 1
    print("PROACTIVE_OK")
    return 0


def cmd_send(to_user_id, context_token, text):
    """发送回复。网络层失败时用相同 client_id 最多重试一次；应用层明确失败不重试。"""
    s = load_session()
    token = s.get("bot_token")
    if not token:
        print("NO_TOKEN")
        return 1
    if text == "-":
        text = sys.stdin.read()
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
            data = api("POST", "ilink/bot/sendmessage", body, token=token, timeout=40)
        except Exception as e:
            if attempt == 1:
                time.sleep(5)
                continue
            print(f"SEND_ERROR:{e}")
            return 1
        code = data.get("code", 0)
        if code == -14:
            print("TOKEN_EXPIRED")
            return 2
        if code != 0:
            print(f"SEND_FAILED:{str(data)[:200]}")
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


def cmd_send_media(to_user_id, context_token, path, media_type, playtime_ms=0):
    """发送图片/视频/文件（回复模式，需 context_token）。"""
    s = load_session()
    token = s.get("bot_token")
    if not token:
        print("NO_TOKEN")
        return 1
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception as e:
        print(f"MEDIA_READ_ERROR:{e}")
        return 1
    try:
        up = _upload_media(to_user_id, data, media_type, token)
        item = _media_item(path, up, media_type, playtime_ms)
        data_resp = _send_item_raw(to_user_id, context_token, item, token)
    except Exception as e:
        print(f"SEND_ERROR:{e}")
        return 1
    code = data_resp.get("code", 0)
    if code == -14:
        print("TOKEN_EXPIRED")
        return 2
    if code != 0 or not data_resp.get("message_id"):
        print(f"SEND_FAILED:{str(data_resp)[:200]}")
        return 1
    print("SEND_OK")
    return 0


def cmd_proactive_media(to_user_id, path, media_type, playtime_ms=0):
    """主动推送图片/视频/文件：用缓存的 context_token，失败则去掉 token 重试一次。"""
    s = load_session()
    token = s.get("bot_token")
    if not token:
        print("NO_TOKEN")
        return 1
    contacts = load_contacts()
    ctx = (contacts.get(to_user_id) or {}).get("context_token")
    if not ctx:
        print("NO_CONTEXT_TOKEN")
        return 1
    try:
        with open(path, "rb") as f:
            data = f.read()
        up = _upload_media(to_user_id, data, media_type, token)
        item = _media_item(path, up, media_type, playtime_ms)
        data_resp = _send_item_raw(to_user_id, ctx, item, token)
    except Exception as e:
        print(f"SEND_ERROR:{e}")
        return 1
    ok = data_resp.get("code", 0) == 0 and data_resp.get("message_id")
    if data_resp.get("code", 0) == -14:
        print("TOKEN_EXPIRED")
        return 2
    if not ok:
        try:
            data_resp = _send_item_raw(to_user_id, None, item, token)
        except Exception as e:
            print(f"SEND_ERROR:{e}")
            return 1
        if data_resp.get("code", 0) == -14:
            print("TOKEN_EXPIRED")
            return 2
        if data_resp.get("code", 0) != 0 or not data_resp.get("message_id"):
            print(f"PROACTIVE_FAILED:{str(data_resp)[:200]}")
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

    # Message queue (hook state dir; ILINK_HOOK_STATE_DIR overrides, for tests)
    state_dir = os.environ.get("ILINK_HOOK_STATE_DIR",
                               os.path.expanduser("~/hooks/state"))
    pend = os.path.join(state_dir, "ilink-pending.json")
    proc = os.path.join(state_dir, "ilink-processing.json")

    def _queue_count(path):
        try:
            with open(path) as f:
                return len(json.load(f).get("msgs") or [])
        except Exception:
            return 0

    if os.path.exists(pend) and _queue_count(pend) > 0:
        lines.append(f"queue: {_queue_count(pend)} message(s) pending")
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


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "qrcode":
        cmd_qrcode()
    elif cmd == "login":
        sys.exit(cmd_login(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 300))
    elif cmd == "poll":
        sys.exit(cmd_poll())
    elif cmd == "commit":
        sys.exit(cmd_commit())
    elif cmd == "health":
        sys.exit(cmd_health(offline="--offline" in sys.argv))
    elif cmd == "send":
        sys.exit(cmd_send(sys.argv[2], sys.argv[3], sys.argv[4]))
    elif cmd == "proactive":
        sys.exit(cmd_proactive(sys.argv[2], sys.argv[3]))
    elif cmd == "typing":
        sys.exit(cmd_typing(sys.argv[2], sys.argv[3]))
    elif cmd == "send-image":
        sys.exit(cmd_send_media(sys.argv[2], sys.argv[3], sys.argv[4], 2))
    elif cmd == "send-file":
        sys.exit(cmd_send_media(sys.argv[2], sys.argv[3], sys.argv[4], 4))
    elif cmd == "send-video":
        sys.exit(cmd_send_media(sys.argv[2], sys.argv[3], sys.argv[4], 5))
    elif cmd == "send-voice":
        sys.exit(cmd_send_media(sys.argv[2], sys.argv[3], sys.argv[4], 3,
                                int(sys.argv[5]) if len(sys.argv) > 5 else 0))
    elif cmd == "proactive-image":
        sys.exit(cmd_proactive_media(sys.argv[2], sys.argv[3], 2))
    elif cmd == "proactive-file":
        sys.exit(cmd_proactive_media(sys.argv[2], sys.argv[3], 4))
    elif cmd == "proactive-video":
        sys.exit(cmd_proactive_media(sys.argv[2], sys.argv[3], 5))
    elif cmd == "proactive-voice":
        sys.exit(cmd_proactive_media(sys.argv[2], sys.argv[3], 3,
                                     int(sys.argv[4]) if len(sys.argv) > 4 else 0))
    else:
        print(__doc__)
        sys.exit(2)
