#!/usr/bin/env python3
"""Tests for release-reminder subscriptions (update-push solution).

subscribe/unsubscribe manage the per-owner subscribed list in
version_notify.json (idempotent), and clear any pending subscribe
question either way. The welcome flow opens a 24h subscribe-question
window via subscribe_ask.json.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_subscribe.py
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
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-subscribe-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = tempfile.mkdtemp(prefix="ilink-test-state-")
import ilink  # noqa: E402


def read_state():
    try:
        with open(ilink.VERSION_NOTIFY_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def run_cmd(fn, *args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*args)
    return code, buf.getvalue()


class SubscribeTest(unittest.TestCase):
    def setUp(self):
        ilink.BRIDGE = DATA_DIR
        ilink.VERSION_NOTIFY_FILE = os.path.join(DATA_DIR, "version_notify.json")
        ilink.SUBSCRIBE_ASK_FILE = os.path.join(DATA_DIR, "subscribe_ask.json")
        for name in os.listdir(DATA_DIR):
            path = os.path.join(DATA_DIR, name)
            if os.path.isfile(path):
                os.remove(path)

    def test_subscribe_adds_and_is_idempotent(self):
        code, out = run_cmd(ilink.cmd_subscribe, "o1")
        self.assertEqual(code, 0)
        self.assertIn("SUBSCRIBED:o1", out)
        self.assertEqual(read_state()["subscribed"], ["o1"])
        run_cmd(ilink.cmd_subscribe, "o1")
        self.assertEqual(read_state()["subscribed"], ["o1"])

    def test_unsubscribe_removes_only_that_owner(self):
        run_cmd(ilink.cmd_subscribe, "o1")
        run_cmd(ilink.cmd_subscribe, "o2")
        code, out = run_cmd(ilink.cmd_unsubscribe, "o1")
        self.assertEqual(code, 0)
        self.assertIn("UNSUBSCRIBED:o1", out)
        self.assertEqual(read_state()["subscribed"], ["o2"])

    def test_commands_clear_pending_question(self):
        with open(ilink.SUBSCRIBE_ASK_FILE, "w") as f:
            json.dump({"owner_id": "o1", "asked_at": int(time.time())}, f)
        run_cmd(ilink.cmd_subscribe, "o1")
        self.assertFalse(os.path.exists(ilink.SUBSCRIBE_ASK_FILE))
        with open(ilink.SUBSCRIBE_ASK_FILE, "w") as f:
            json.dump({"owner_id": "o1", "asked_at": int(time.time())}, f)
        run_cmd(ilink.cmd_unsubscribe, "o1")
        self.assertFalse(os.path.exists(ilink.SUBSCRIBE_ASK_FILE))

    def test_write_subscribe_ask(self):
        self.assertEqual(ilink.SUBSCRIBE_ASK_TTL, 86400)
        ilink._write_subscribe_ask("o1")
        with open(ilink.SUBSCRIBE_ASK_FILE) as f:
            data = json.load(f)
        self.assertEqual(data["owner_id"], "o1")
        self.assertAlmostEqual(data["asked_at"], int(time.time()), delta=5)

    def test_welcome_includes_subscribe_question(self):
        self.assertEqual(len(ilink.WELCOME_MESSAGES), 4)
        self.assertIn("订阅", ilink.WELCOME_MESSAGES[3])
        self.assertIn("取消订阅", ilink.WELCOME_MESSAGES[3])

    def test_cli_dispatch(self):
        code, out = run_cmd(ilink.main, ["ilink.py", "subscribe", "o9"])
        self.assertEqual(code, 0)
        self.assertIn("SUBSCRIBED:o9", out)
        code, out = run_cmd(ilink.main, ["ilink.py", "unsubscribe", "o9"])
        self.assertEqual(code, 0)
        self.assertIn("UNSUBSCRIBED:o9", out)


if __name__ == "__main__":
    unittest.main()
