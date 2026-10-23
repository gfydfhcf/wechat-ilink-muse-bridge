#!/usr/bin/env python3
"""Tests for the first-owner binding window (`ilink.py bind`).

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_bind.py

Pinned behavior (2026-10-23): binding is a dedicated, bounded setup
window — NOT a permanent first-come mode in poll. Inside one window
(default 600s) the first sender becomes the owner, is written to
owner.json BEFORE the welcome goes out, and receives the welcome as
three separate bubbles. The binding batch is only committed after the
welcome completes; progress is persisted per bubble so a crash resumes
instead of re-binding. A lapsed window exits 3 (BIND_TIMEOUT) so the
deploying agent can remind the human and open a fresh window. Normal
polling stays deny-by-default throughout.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-bind-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = tempfile.mkdtemp(prefix="ilink-test-state-")
import ilink  # noqa: E402


def make_msg(mid, sender, ctx=None):
    return {
        "message_id": mid,
        "message_type": 1,
        "from_user_id": sender,
        "context_token": ctx or f"ctx-{sender}",
        "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
    }


class FakeAPI:
    """Routes getupdates / sendmessage; records every welcome bubble."""

    def __init__(self):
        self.payloads = []
        self.sent = []          # list of (to_user_id, context_token, text)
        self.send_fail_at = None  # 0-based bubble index that returns code 9
        self.calls = 0

    def __call__(self, method, path, body=None, token=None, timeout=None):
        self.calls += 1
        if "getupdates" in path:
            if self.payloads:
                return self.payloads.pop(0)
            return {"msgs": [], "get_updates_buf": "BUF-1"}
        if "sendmessage" in path:
            idx = len(self.sent)
            msg = body["msg"]
            text = msg["item_list"][0]["text_item"]["text"]
            self.sent.append((msg["to_user_id"], msg.get("context_token"), text))
            if self.send_fail_at is not None and idx == self.send_fail_at:
                return {"code": 9, "errmsg": "boom"}
            return {"code": 0, "message_id": 1000 + idx}
        raise AssertionError(f"unexpected api path: {path}")


def run_bind(wait_secs=5):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ilink.cmd_bind(wait_secs)
    return code, buf.getvalue()


def read_json(name):
    path = os.path.join(DATA_DIR, name)
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


class BindTest(unittest.TestCase):
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
        self.api = FakeAPI()
        self._orig_api = ilink.api
        ilink.api = self.api
        self.addCleanup(self._restore)

    def _restore(self):
        ilink.api = self._orig_api

    def test_first_sender_binds_and_gets_three_welcomes(self):
        self.api.payloads = [{
            "msgs": [make_msg(11, "owner-1", "ctx-A"), make_msg(12, "owner-1", "ctx-B")],
            "get_updates_buf": "BUF-2",
        }]
        code, out = run_bind()
        self.assertEqual(code, 0)
        self.assertIn("BIND_WAITING:5", out)
        self.assertIn("BIND_OK:owner-1", out)
        owner = read_json("owner.json")
        self.assertEqual(owner["owner_id"], "owner-1")
        self.assertEqual(owner["owner_ids"], ["owner-1"])
        # Exactly three bubbles, in order, to the winner, latest ctx.
        self.assertEqual([t for _, _, t in self.api.sent],
                         list(ilink.WELCOME_MESSAGES))
        self.assertTrue(all(to == "owner-1" for to, _, _ in self.api.sent))
        self.assertTrue(all(ctx == "ctx-B" for _, ctx, _ in self.api.sent))
        # Contacts cached for later proactive pushes.
        self.assertEqual(read_json("contacts.json")["owner-1"]["context_token"],
                         "ctx-B")
        # Binding batch committed only after the welcome: cursor + seen.
        with open(ilink.SESSION_FILE) as f:
            self.assertEqual(json.load(f)["cursor"], "BUF-2")
        self.assertEqual(sorted(ilink.load_seen()), ["11", "12"])
        # No leftover claim state.
        self.assertEqual(read_json("bind_state.json"), {})

    def test_first_sender_wins_other_sender_queued_for_approval(self):
        self.api.payloads = [{
            "msgs": [make_msg(1, "first"), make_msg(2, "second")],
            "get_updates_buf": "BUF-2",
        }]
        code, _ = run_bind()
        self.assertEqual(code, 0)
        self.assertEqual(read_json("owner.json")["owner_id"], "first")
        self.assertTrue(all(to == "first" for to, _, _ in self.api.sent))
        book = read_json("unknown_senders.json")
        self.assertEqual(book["second"]["count"], 1)
        self.assertEqual(sorted(ilink.load_seen()), ["1", "2"])

    def test_timeout_returns_3_and_binds_nobody(self):
        code, out = run_bind(wait_secs=0)
        self.assertEqual(code, 3)
        self.assertIn("BIND_TIMEOUT", out)
        self.assertEqual(read_json("owner.json"), {})
        self.assertEqual(self.api.sent, [])

    def test_already_bound_is_a_noop(self):
        with open(os.path.join(DATA_DIR, "owner.json"), "w") as f:
            json.dump({"owner_id": "owner-1", "owner_ids": ["owner-1"]}, f)
        code, out = run_bind()
        self.assertEqual(code, 0)
        self.assertIn("ALREADY_BOUND:owner-1", out)
        self.assertEqual(self.api.calls, 0)
        self.assertEqual(self.api.sent, [])

    def test_welcome_failure_keeps_claim_and_resumes(self):
        self.api.payloads = [{
            "msgs": [make_msg(7, "owner-1")],
            "get_updates_buf": "BUF-2",
        }]
        self.api.send_fail_at = 1  # second bubble fails at the app layer
        code, out = run_bind()
        self.assertEqual(code, 1)
        self.assertIn("BIND_WELCOME_FAILED:2", out)
        # The claim stands (window closed to everyone else) ...
        self.assertEqual(read_json("owner.json")["owner_id"], "owner-1")
        state = read_json("bind_state.json")
        self.assertEqual(state["welcome_sent"], 1)
        # ... but the batch is NOT committed yet.
        with open(ilink.SESSION_FILE) as f:
            self.assertEqual(json.load(f)["cursor"], "BUF-1")
        self.assertEqual(ilink.load_seen(), [])
        # Re-running bind resumes the welcome where it stopped: only
        # bubbles 2 and 3 go out, then the batch commits.
        self.api.send_fail_at = None
        sent_before = len(self.api.sent)
        code, out = run_bind()
        self.assertEqual(code, 0)
        self.assertIn("BIND_OK:owner-1", out)
        self.assertEqual([t for _, _, t in self.api.sent[sent_before:]],
                         list(ilink.WELCOME_MESSAGES[1:]))
        with open(ilink.SESSION_FILE) as f:
            self.assertEqual(json.load(f)["cursor"], "BUF-2")
        self.assertEqual(ilink.load_seen(), ["7"])
        self.assertEqual(read_json("bind_state.json"), {})


if __name__ == "__main__":
    unittest.main()
