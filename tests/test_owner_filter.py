#!/usr/bin/env python3
"""Tests for the code-level owner allowlist in cmd_poll.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_owner_filter.py

Pinned behavior (external review, 2026-10-22): sender verification used
to live only in the worker's instructions — poll queued ANY sender and
woke the agent, and strangers landed in the contacts cache. Now poll
itself enforces owner.json: non-owner messages are dropped before
queueing, their ids still flow into the commit block (cursor advances,
no redelivery, no queue jam), contacts stay owner-only, and turned-away
senders are recorded in unknown_senders.json as an approval queue.
With no owner.json at all, everyone is denied (fail closed).
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-ownerfilter-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = tempfile.mkdtemp(prefix="ilink-test-state-")
import ilink  # noqa: E402

CURRENT = {"payload": {"msgs": [], "get_updates_buf": "BUF-2"}}


def fake_api(*args, **kwargs):
    return CURRENT["payload"]


def make_msg(mid, sender):
    return {
        "message_id": mid,
        "message_type": 1,
        "from_user_id": sender,
        "context_token": f"ctx-{sender}",
        "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
    }


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


def read_json(name):
    path = os.path.join(DATA_DIR, name)
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


class OwnerFilterTest(unittest.TestCase):
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
            json.dump({"bot_token": "tok", "cursor": ""}, f)
        ilink.api = fake_api
        CURRENT["payload"] = {"msgs": [], "get_updates_buf": "BUF-2"}

    def set_owners(self, data):
        with open(os.path.join(DATA_DIR, "owner.json"), "w") as f:
            json.dump(data, f)

    def poll_with(self, msgs):
        CURRENT["payload"] = {"msgs": msgs, "get_updates_buf": "BUF-2"}
        return run_poll()

    def test_owner_message_passes(self):
        self.set_owners({"owner_id": "owner-1"})
        out = self.poll_with([make_msg(1, "owner-1")])
        self.assertEqual(len(out["msgs"]), 1)
        self.assertEqual(out["msgs"][0]["from_user_id"], "owner-1")
        self.assertEqual(out["filtered"]["dropped"], 0)
        self.assertIn("owner-1", read_json("contacts.json"))

    def test_stranger_filtered_but_still_committed(self):
        self.set_owners({"owner_id": "owner-1"})
        out = self.poll_with([make_msg(99, "stranger-1")])
        self.assertEqual(out["msgs"], [])
        self.assertEqual(out["filtered"]["dropped"], 1)
        self.assertEqual(out["filtered"]["senders"], ["stranger-1"])
        # The id must stay in the commit block: cursor advances past the
        # stranger, so the server never redelivers and the queue can't jam.
        self.assertIn("99", out["commit"]["ids"])
        # Not in contacts; recorded in the approval queue instead.
        self.assertNotIn("stranger-1", read_json("contacts.json"))
        book = read_json("unknown_senders.json")
        self.assertEqual(book["stranger-1"]["count"], 1)

    def test_no_owner_config_denies_everyone(self):
        out = self.poll_with([make_msg(5, "anyone")])
        self.assertEqual(out["msgs"], [])
        self.assertEqual(out["filtered"]["dropped"], 1)

    def test_owner_ids_list_allows_multiple_accounts(self):
        self.set_owners({"owner_id": "a1", "owner_ids": ["a1", "a2"]})
        out = self.poll_with([make_msg(1, "a1"), make_msg(2, "a2")])
        self.assertEqual(len(out["msgs"]), 2)
        self.assertEqual(out["filtered"]["dropped"], 0)

    def test_repeat_stranger_count_increments(self):
        self.set_owners({"owner_id": "owner-1"})
        self.poll_with([make_msg(1, "stranger-1")])
        self.poll_with([make_msg(2, "stranger-1")])
        book = read_json("unknown_senders.json")
        self.assertEqual(book["stranger-1"]["count"], 2)

    def test_committed_stranger_is_not_reprocessed(self):
        self.set_owners({"owner_id": "owner-1"})
        out = self.poll_with([make_msg(7, "stranger-1")])
        run_commit(out["commit"])
        # Server redelivers the same id: dedupe drops it before the
        # allowlist even sees it, so the approval count stays at 1.
        out2 = self.poll_with([make_msg(7, "stranger-1")])
        self.assertEqual(out2["msgs"], [])
        self.assertEqual(out2["filtered"]["dropped"], 0)
        book = read_json("unknown_senders.json")
        self.assertEqual(book["stranger-1"]["count"], 1)

    def test_mixed_batch(self):
        self.set_owners({"owner_id": "owner-1"})
        out = self.poll_with([make_msg(1, "owner-1"), make_msg(2, "stranger-1")])
        self.assertEqual(len(out["msgs"]), 1)
        self.assertEqual(out["msgs"][0]["from_user_id"], "owner-1")
        self.assertEqual(out["filtered"]["dropped"], 1)
        self.assertEqual(sorted(out["commit"]["ids"]), ["1", "2"])


if __name__ == "__main__":
    unittest.main()
