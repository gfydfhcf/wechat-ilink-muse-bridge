#!/usr/bin/env python3
"""Fault-injection tests for the two-phase poll protocol.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_poll_commit.py

Scenario pinned down here (from an external review, 2026-10-22): if the
process dies after getupdates returns but before the batch is durably
queued, the messages must come back on the next poll. The old design
recorded seen-ids and advanced the cursor inside poll itself, so that
crash window lost messages permanently. Now poll is pure (no cursor,
no seen writes) and cmd_commit does both, after queueing.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-pollcommit-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = tempfile.mkdtemp(prefix="ilink-test-state-")
import ilink  # noqa: E402

ilink.BRIDGE = DATA_DIR
ilink.SESSION_FILE = os.path.join(DATA_DIR, "session.json")
ilink.CONTACTS_FILE = os.path.join(DATA_DIR, "contacts.json")
ilink.SEEN_FILE = os.path.join(DATA_DIR, "seen_ids.json")

RAW_MSG = {
    "message_id": 4242,
    "message_type": 1,
    "from_user_id": "u1",
    "context_token": "ctx-1",
    "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
}
API_PAYLOAD = {"msgs": [RAW_MSG], "get_updates_buf": "BUF-2"}


def fake_api(*args, **kwargs):
    return API_PAYLOAD


def run_poll():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ilink.cmd_poll()
    assert code == 0
    return json.loads(buf.getvalue())


def run_commit(commit_block):
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(commit_block))
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ilink.cmd_commit()
    finally:
        sys.stdin = old_stdin
    assert code == 0, buf.getvalue()
    return buf.getvalue()


class PollCommitTest(unittest.TestCase):
    def setUp(self):
        # Pin paths per-test: other modules in the suite pin them too.
        ilink.BRIDGE = DATA_DIR
        ilink.SESSION_FILE = os.path.join(DATA_DIR, "session.json")
        ilink.CONTACTS_FILE = os.path.join(DATA_DIR, "contacts.json")
        ilink.SEEN_FILE = os.path.join(DATA_DIR, "seen_ids.json")
        for name in os.listdir(DATA_DIR):
            path = os.path.join(DATA_DIR, name)
            if os.path.isfile(path):
                os.remove(path)
        with open(ilink.SESSION_FILE, "w") as f:
            json.dump({"bot_token": "tok", "cursor": "BUF-1"}, f)
        # The scenario's sender is the owner; without an owner.json the
        # code-level allowlist (2026-10-23) would deny everyone.
        with open(os.path.join(DATA_DIR, "owner.json"), "w") as f:
            json.dump({"owner_id": "u1"}, f)
        self._orig_api = ilink.api
        ilink.api = fake_api
        self.addCleanup(self._restore)

    def _restore(self):
        ilink.api = self._orig_api

    def session_cursor(self):
        with open(ilink.SESSION_FILE) as f:
            return json.load(f).get("cursor")

    def test_poll_has_no_side_effects(self):
        out = run_poll()
        self.assertEqual(len(out["msgs"]), 1)
        self.assertEqual(out["msgs"][0]["text"], "hello")
        self.assertEqual(out["commit"], {"cursor": "BUF-2", "ids": ["4242"]})
        # neither the cursor nor the seen set moved
        self.assertEqual(self.session_cursor(), "BUF-1")
        self.assertEqual(ilink.load_seen(), [])

    def test_crash_before_commit_redelivers(self):
        first = run_poll()
        second = run_poll()  # process "restarted", commit never happened
        self.assertEqual(first, second)

    def test_commit_advances_cursor_and_seen(self):
        out = run_poll()
        run_commit(out["commit"])
        self.assertEqual(self.session_cursor(), "BUF-2")
        self.assertEqual(ilink.load_seen(), ["4242"])
        # and now the same server batch is filtered out as a duplicate
        out2 = run_poll()
        self.assertEqual(out2["msgs"], [])
        self.assertEqual(out2["commit"]["ids"], [])

    def test_commit_is_idempotent(self):
        out = run_poll()
        run_commit(out["commit"])
        run_commit(out["commit"])
        self.assertEqual(self.session_cursor(), "BUF-2")
        self.assertEqual(ilink.load_seen(), ["4242"])


if __name__ == "__main__":
    unittest.main()
