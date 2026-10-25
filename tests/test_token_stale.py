#!/usr/bin/env python3
"""Tests for the -14 stale backoff (BACKLOG item 7).

A -14 inside the backoff window reports TOKEN_STALE (exit 3) instead of
TOKEN_EXPIRED: the hook stays silent and keeps polling, since the token
may self-heal. Only a -14 streak longer than the window reports
TOKEN_EXPIRED. Any successful call clears the marker.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_token_stale.py
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
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-stale-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = tempfile.mkdtemp(prefix="ilink-test-state-")
import ilink  # noqa: E402

CURRENT = {"payload": {"msgs": [], "get_updates_buf": "BUF-2"}}


def fake_api(*args, **kwargs):
    path = args[1] if len(args) > 1 else ""
    if "sendmessage" in path:
        return dict(CURRENT["send_response"])
    return CURRENT["payload"]


def run_poll():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ilink.cmd_poll()
    return code, buf.getvalue()


def run_send():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ilink.cmd_send("owner-1", "ctx", "hi")
    return code, buf.getvalue()


def seed_stale(first_seen):
    with open(ilink.TOKEN_STALE_FILE, "w") as f:
        json.dump({"first_seen": first_seen}, f)


class TokenStaleTest(unittest.TestCase):
    def setUp(self):
        ilink.BRIDGE = DATA_DIR
        ilink.SESSION_FILE = os.path.join(DATA_DIR, "session.json")
        ilink.CONTACTS_FILE = os.path.join(DATA_DIR, "contacts.json")
        ilink.SEEN_FILE = os.path.join(DATA_DIR, "seen_ids.json")
        ilink.QUOTE_CACHE_FILE = os.path.join(DATA_DIR, "quote_cache.json")
        ilink.QUOTE_CACHE_LOCK_FILE = os.path.join(DATA_DIR, "quote_cache.lock")
        ilink.SEND_AUDIT_FILE = os.path.join(DATA_DIR, "send_audit.jsonl")
        ilink.SEND_LEDGER_FILE = os.path.join(DATA_DIR, "send_ledger.json")
        ilink.TOKEN_STALE_FILE = os.path.join(DATA_DIR, "token_stale.json")
        for name in os.listdir(DATA_DIR):
            path = os.path.join(DATA_DIR, name)
            if os.path.isfile(path):
                os.remove(path)
        with open(ilink.SESSION_FILE, "w") as f:
            json.dump({"bot_token": "tok", "cursor": ""}, f)
        with open(os.path.join(DATA_DIR, "owner.json"), "w") as f:
            json.dump({"owner_ids": ["owner-1"]}, f)
        ilink.api = fake_api
        CURRENT["payload"] = {"msgs": [], "get_updates_buf": "BUF-2"}
        CURRENT["send_response"] = {"code": 0, "message_id": 999}

    def test_first_minus14_is_stale(self):
        CURRENT["payload"] = {"code": -14}
        code, out = run_poll()
        self.assertEqual(code, 3)
        self.assertTrue(out.startswith("TOKEN_STALE"))
        self.assertTrue(os.path.exists(ilink.TOKEN_STALE_FILE))

    def test_minus14_inside_window_stays_stale(self):
        seed_stale(int(time.time()) - 100)
        CURRENT["payload"] = {"code": -14}
        code, out = run_poll()
        self.assertEqual(code, 3)
        self.assertTrue(out.startswith("TOKEN_STALE"))

    def test_minus14_after_window_is_expired(self):
        seed_stale(int(time.time()) - ilink.TOKEN_STALE_WINDOW - 10)
        CURRENT["payload"] = {"code": -14}
        code, out = run_poll()
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("TOKEN_EXPIRED"))
        self.assertFalse(os.path.exists(ilink.TOKEN_STALE_FILE))

    def test_success_clears_stale(self):
        seed_stale(int(time.time()) - 100)
        code, out = run_poll()
        self.assertEqual(code, 0)
        self.assertIn('"msgs"', out)
        self.assertFalse(os.path.exists(ilink.TOKEN_STALE_FILE))

    def test_send_minus14_is_stale(self):
        CURRENT["send_response"] = {"code": -14}
        code, out = run_send()
        self.assertEqual(code, 3)
        self.assertTrue(out.startswith("TOKEN_STALE"))

    def test_send_success_clears_stale(self):
        seed_stale(int(time.time()) - 100)
        code, out = run_send()
        self.assertEqual(code, 0)
        self.assertIn("SEND_OK", out)
        self.assertFalse(os.path.exists(ilink.TOKEN_STALE_FILE))


if __name__ == "__main__":
    unittest.main()
