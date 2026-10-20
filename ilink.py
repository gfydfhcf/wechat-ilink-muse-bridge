#!/usr/bin/env python3
"""iLink WeChat bot client (official Tencent protocol, no OpenClaw needed).

Usage:
  ilink.py qrcode              # get login QR, save image, print QR_CODE:<id>
  ilink.py status <qrcode>     # poll scan status once (JSON)
  ilink.py login <qrcode>      # poll until confirmed (up to 300s), save token
  ilink.py poll                # fetch new messages since cursor (JSON)
  ilink.py send <to> <ctx> <text>  # send text reply (use - for stdin)
  ilink.py proactive <to> <text>   # proactive push via cached context_token
  ilink.py typing <to> <start|stop>  # show/cancel the "typing..." indicator
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
    """Download + AES-ECB decrypt an IMAGE/VOICE/FILE/VIDEO item. Returns saved path or None."""
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
        print(f"MEDIA_ERROR:no aes_key", flush=True)
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
        print(f"MEDIA_ERROR:no download url", flush=True)
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
        print(f"MEDIA_ERROR:incomplete after retries {len(ciphertext)}/{total} last={last_err}", flush=True)
        return None
    if not ciphertext:
        print(f"MEDIA_ERROR:empty download last={last_err}", flush=True)
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
    msgs = []
    contacts = load_contacts()
    for m in data.get("msgs") or []:
        if m.get("message_type") != 1:  # 只要用户发来的消息
            continue
        from_id = m.get("from_user_id")
        ctx = m.get("context_token")
        if from_id and ctx:
            contacts[from_id] = {"context_token": ctx, "updated_at": int(time.time())}
        for item in m.get("item_list") or []:
            t = item.get("type") or 0
            kind = ITEM_TYPE_NAMES.get(t, f"unknown_{t}")
            entry = {"from_user_id": from_id, "context_token": ctx, "kind": kind}
            if t == 1:  # TEXT
                entry["text"] = (item.get("text_item") or {}).get("text", "")
            elif t == 3:  # VOICE: 可能带语音转写文本
                entry["text"] = (item.get("voice_item") or {}).get("text", "")
                entry["voice_duration_ms"] = (item.get("voice_item") or {}).get("voice_duration_ms", 0)
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
    s["cursor"] = new_buf
    save_session(s)
    print(json.dumps({"msgs": msgs}, ensure_ascii=False))
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


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "qrcode":
        cmd_qrcode()
    elif cmd == "login":
        sys.exit(cmd_login(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 300))
    elif cmd == "poll":
        sys.exit(cmd_poll())
    elif cmd == "send":
        sys.exit(cmd_send(sys.argv[2], sys.argv[3], sys.argv[4]))
    elif cmd == "proactive":
        sys.exit(cmd_proactive(sys.argv[2], sys.argv[3]))
    elif cmd == "typing":
        sys.exit(cmd_typing(sys.argv[2], sys.argv[3]))
    else:
        print(__doc__)
        sys.exit(2)
