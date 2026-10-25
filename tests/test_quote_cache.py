#!/usr/bin/env python3
"""Tests for quote-message restoration (BACKLOG item 6).

The server's ref_msg carries only the quoted message's id (full quotes)
or id plus a partial_text fragment (selective quotes) — never the
original text. So poll keeps a local id -> original cache, and send /
proactive record their server message_id on confirmed success, so a
reply is quotable even before the next poll echoes it back.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_quote_cache.py

Pinned behavior:
- poll caches inbound owner messages and bot messages echoed back in
  getupdates (type 2), but never queues the bot's own messages;
- a quoted entry carries quoted_text / quoted_kind (cache hit),
  quoted_fragment (selective quote, even on a cache miss),
  ref_msg_id / ref_svr_id, and quoted_title when the server sent one;
- svr_id -> msg_id mappings are learned from refs carrying both;
- same-batch quotes resolve (entries cached earlier in the batch);
- stranger messages are never cached (owner allowlist applies);
- a corrupt cache degrades to empty instead of failing poll/send;
- retention: entries older than QUOTE_CACHE_TTL are pruned on write,
  and the cap keeps only the newest QUOTE_CACHE_CAP entries.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-quote-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = tempfile.mkdtemp(prefix="ilink-test-state-")
import ilink  # noqa: E402

CURRENT = {"payload": {"msgs": [], "get_updates_buf": "BUF-2"}}
SEND_ID = {"message_id": 999}


def fake_api(*args, **kwargs):
    path = args[1] if len(args) > 1 else ""
    if "sendmessage" in path:
        return {"code": 0, "message_id": SEND_ID["message_id"]}
    return CURRENT["payload"]


def make_msg(mid, sender, text="hi", ref_msg=None, mtype=1):
    item = {"type": 1, "text_item": {"text": text}}
    if ref_msg is not None:
        item["ref_msg"] = ref_msg
    return {
        "message_id": mid,
        "message_type": mtype,
        "from_user_id": sender,
        "context_token": f"ctx-{sender}",
        "item_list": [item],
    }


def run_poll():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ilink.cmd_poll()
    assert code == 0, buf.getvalue()
    return json.loads(buf.getvalue())


def run_cmd(func, *args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = func(*args)
    return code, buf.getvalue()


def read_json(name):
    path = os.path.join(DATA_DIR, name)
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def write_json(name, data):
    with open(os.path.join(DATA_DIR, name), "w") as f:
        json.dump(data, f)


class QuoteCacheTest(unittest.TestCase):
    def setUp(self):
        ilink.BRIDGE = DATA_DIR
        ilink.SESSION_FILE = os.path.join(DATA_DIR, "session.json")
        ilink.CONTACTS_FILE = os.path.join(DATA_DIR, "contacts.json")
        ilink.SEEN_FILE = os.path.join(DATA_DIR, "seen_ids.json")
        ilink.QUOTE_CACHE_FILE = os.path.join(DATA_DIR, "quote_cache.json")
        ilink.QUOTE_CACHE_LOCK_FILE = os.path.join(DATA_DIR, "quote_cache.lock")
        for name in os.listdir(DATA_DIR):
            path = os.path.join(DATA_DIR, name)
            if os.path.isfile(path):
                os.remove(path)
        with open(ilink.SESSION_FILE, "w") as f:
            json.dump({"bot_token": "tok", "cursor": ""}, f)
        ilink.api = fake_api
        CURRENT["payload"] = {"msgs": [], "get_updates_buf": "BUF-2"}
        self.set_owners({"owner_id": "owner-1"})

    def set_owners(self, data):
        write_json("owner.json", data)

    def poll_with(self, msgs):
        CURRENT["payload"] = {"msgs": msgs, "get_updates_buf": "BUF-2"}
        return run_poll()

    def test_poll_caches_inbound_user_text(self):
        out = self.poll_with([make_msg(1, "owner-1", "hello")])
        self.assertEqual(len(out["msgs"]), 1)
        cache = read_json("quote_cache.json")
        self.assertEqual(cache["by_id"]["1"]["text"], "hello")
        self.assertEqual(cache["by_id"]["1"]["kind"], "text")
        self.assertEqual(cache["by_id"]["1"]["from"], "user")

    def test_poll_caches_bot_message_without_queueing(self):
        out = self.poll_with([make_msg(7, "bot", "bot said this", mtype=2)])
        self.assertEqual(out["msgs"], [])
        cache = read_json("quote_cache.json")
        self.assertEqual(cache["by_id"]["7"]["text"], "bot said this")
        self.assertEqual(cache["by_id"]["7"]["from"], "bot")

    def test_full_quote_resolves_from_cache(self):
        write_json("quote_cache.json", {
            "by_id": {"100": {"text": "bot said this", "kind": "text",
                              "from": "bot", "at": int(time.time())}},
            "by_svr": {}})
        ref = {"message_item": {"msg_id": 100, "create_time_ms": 1}}
        out = self.poll_with([make_msg(2, "owner-1", "what is this",
                                       ref_msg=ref)])
        entry = out["msgs"][0]
        self.assertEqual(entry["quoted_text"], "bot said this")
        self.assertEqual(entry["quoted_kind"], "text")
        self.assertEqual(entry["ref_msg_id"], "100")

    def test_selective_quote_fragment_without_cache_hit(self):
        ref = {"message_item": {"msg_id": 4242},
               "partial_text": {"start": "好", "end": "睡",
                                "startindex": 0, "endindex": 0,
                                "quotemd5": "6366c8642"}}
        out = self.poll_with([make_msg(3, "owner-1", "好的 我马上睡觉",
                                       ref_msg=ref)])
        entry = out["msgs"][0]
        self.assertNotIn("quoted_text", entry)
        self.assertEqual(entry["quoted_fragment"], {"start": "好", "end": "睡"})

    def test_svr_id_resolution(self):
        write_json("quote_cache.json", {
            "by_id": {"55": {"text": "old msg", "kind": "text",
                             "from": "user", "at": int(time.time())}},
            "by_svr": {"svr-9": "55"}})
        ref = {"svr_id": "svr-9"}
        out = self.poll_with([make_msg(4, "owner-1", "this one",
                                       ref_msg=ref)])
        self.assertEqual(out["msgs"][0]["quoted_text"], "old msg")
        self.assertEqual(out["msgs"][0]["ref_svr_id"], "svr-9")

    def test_svr_mapping_learned_from_ref(self):
        # The quoted message itself must already be cached: a mapping to
        # an unknown id is pruned as dangling on write, by design.
        write_json("quote_cache.json", {
            "by_id": {"77": {"text": "old bot msg", "kind": "text",
                             "from": "bot", "at": int(time.time())}},
            "by_svr": {}})
        ref = {"message_item": {"msg_id": 77}, "svr_id": "svr-7"}
        out = self.poll_with([make_msg(5, "owner-1", "noted", ref_msg=ref)])
        self.assertEqual(out["msgs"][0]["quoted_text"], "old bot msg")
        cache = read_json("quote_cache.json")
        self.assertEqual(cache["by_svr"]["svr-7"], "77")
        # A later quote carrying only svr_id resolves via the mapping.
        out2 = self.poll_with([make_msg(6, "owner-1", "again",
                                        ref_msg={"svr_id": "svr-7"})])
        self.assertEqual(out2["msgs"][0]["quoted_text"], "old bot msg")

    def test_same_batch_quote_resolves(self):
        batch = [make_msg(1, "owner-1", "first"),
                 make_msg(2, "owner-1", "second",
                          ref_msg={"message_item": {"msg_id": 1}})]
        out = self.poll_with(batch)
        self.assertEqual(out["msgs"][1]["quoted_text"], "first")

    def test_stranger_message_not_cached(self):
        self.poll_with([make_msg(9, "stranger", "hello stranger")])
        cache = read_json("quote_cache.json")
        self.assertNotIn("9", cache.get("by_id", {}))

    def test_corrupt_cache_degrades(self):
        with open(ilink.QUOTE_CACHE_FILE, "w") as f:
            f.write("{not json")
        out = self.poll_with([make_msg(6, "owner-1", "still works")])
        self.assertEqual(len(out["msgs"]), 1)
        self.assertEqual(out["msgs"][0]["text"], "still works")

    def test_send_records_outbound(self):
        code, out = run_cmd(ilink.cmd_send, "owner-1", "ctx", "reply text")
        self.assertEqual(code, 0)
        self.assertIn("SEND_OK", out)
        cache = read_json("quote_cache.json")
        self.assertEqual(cache["by_id"]["999"]["text"], "reply text")
        self.assertEqual(cache["by_id"]["999"]["from"], "bot")

    def test_proactive_records_outbound(self):
        write_json("contacts.json",
                   {"owner-1": {"context_token": "ctx",
                                "updated_at": int(time.time())}})
        code, out = run_cmd(ilink.cmd_proactive, "owner-1", "push text")
        self.assertEqual(code, 0)
        self.assertIn("PROACTIVE_OK", out)
        cache = read_json("quote_cache.json")
        self.assertEqual(cache["by_id"]["999"]["text"], "push text")

    def test_retention_trims_old_and_over_cap(self):
        now = int(time.time())
        old = now - ilink.QUOTE_CACHE_TTL - 100
        write_json("quote_cache.json", {
            "by_id": {
                "1": {"text": "old", "kind": "text", "from": "user", "at": old},
                "2": {"text": "a", "kind": "text", "from": "user", "at": now - 3},
                "3": {"text": "b", "kind": "text", "from": "user", "at": now - 2},
                "4": {"text": "c", "kind": "text", "from": "user", "at": now - 1},
            },
            "by_svr": {"svr-old": "1"}})
        saved_cap = ilink.QUOTE_CACHE_CAP
        try:
            ilink.QUOTE_CACHE_CAP = 2
            ilink._quote_cache_upsert(
                {"5": {"text": "d", "kind": "text", "from": "user",
                       "at": now}})
        finally:
            ilink.QUOTE_CACHE_CAP = saved_cap
        cache = read_json("quote_cache.json")
        # Old entry pruned by TTL, then cap keeps only the 2 newest.
        self.assertEqual(sorted(cache["by_id"]), ["4", "5"])
        self.assertNotIn("svr-old", cache["by_svr"])

    def test_media_descriptors(self):
        self.assertEqual(ilink._quote_media_descriptor(2, {}), "[图片]")
        self.assertEqual(ilink._quote_media_descriptor(4, {"file_item": {"file_name": "a.pdf"}}),
                         "[文件：a.pdf]")
        self.assertEqual(ilink._quote_media_descriptor(5, {}), "[视频]")


if __name__ == "__main__":
    unittest.main()
