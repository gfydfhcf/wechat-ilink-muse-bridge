#!/usr/bin/env python3
"""Tests for ilink.py's health self-check (cmd_health).

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_health.py

Everything runs against temp dirs with --offline semantics (no network):
no session -> exit 2, fresh session -> exit 0, stale processing claim
(simulated by jumping the clock forward) -> exit 1.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-health-")
STATE_DIR = tempfile.mkdtemp(prefix="ilink-test-state-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = STATE_DIR
import ilink  # noqa: E402

# When the whole suite runs in one process, another test module may have
# imported ilink first with its own data dir — pin the paths to ours.
ilink.BRIDGE = DATA_DIR
ilink.SESSION_FILE = os.path.join(DATA_DIR, "session.json")
ilink.CONTACTS_FILE = os.path.join(DATA_DIR, "contacts.json")
ilink.SEEN_FILE = os.path.join(DATA_DIR, "seen_ids.json")


def run_health():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ilink.cmd_health(offline=True)
    return code, buf.getvalue()


def write_session(**kw):
    with open(os.path.join(DATA_DIR, "session.json"), "w") as f:
        json.dump(kw, f)


class HealthTest(unittest.TestCase):
    def setUp(self):
        # Pin paths per-test: other modules in the suite pin them too.
        ilink.BRIDGE = DATA_DIR
        ilink.SESSION_FILE = os.path.join(DATA_DIR, "session.json")
        ilink.CONTACTS_FILE = os.path.join(DATA_DIR, "contacts.json")
        ilink.SEEN_FILE = os.path.join(DATA_DIR, "seen_ids.json")
        os.environ["ILINK_HOOK_STATE_DIR"] = STATE_DIR
        for name in os.listdir(DATA_DIR):
            path = os.path.join(DATA_DIR, name)
            if os.path.isfile(path):
                os.remove(path)
        for name in os.listdir(STATE_DIR):
            os.remove(os.path.join(STATE_DIR, name))

    def test_no_session_is_bad(self):
        code, out = run_health()
        self.assertEqual(code, 2)
        self.assertIn("not logged in", out)
        self.assertIn("verdict: BAD", out)

    def test_fresh_session_is_ok(self):
        write_session(bot_token="tok", login_at=int(time.time()) - 3600)
        code, out = run_health()
        self.assertEqual(code, 0)
        self.assertIn("probe: skipped (--offline)", out)
        self.assertIn("verdict: OK", out)

    def test_pending_messages_are_reported_not_fatal(self):
        write_session(bot_token="tok", login_at=int(time.time()))
        with open(os.path.join(STATE_DIR, "ilink-pending.json"), "w") as f:
            json.dump({"msgs": [{"x": 1}, {"x": 2}]}, f)
        code, out = run_health()
        self.assertEqual(code, 0)
        self.assertIn("2 message(s) pending", out)

    def test_stale_processing_claim_warns(self):
        write_session(bot_token="tok", login_at=int(time.time()))
        proc = os.path.join(STATE_DIR, "ilink-processing.json")
        with open(proc, "w") as f:
            json.dump({"msgs": [{"x": 1}]}, f)
        # ctime cannot be backdated; jump the clock forward 11 minutes
        future = time.time() + 660
        with mock.patch.object(ilink.time, "time", return_value=future):
            code, out = run_health()
        self.assertEqual(code, 1)
        self.assertIn("worker may be stuck", out)
        self.assertIn("verdict: WARNINGS", out)

    def test_fresh_processing_claim_is_fine(self):
        write_session(bot_token="tok", login_at=int(time.time()))
        proc = os.path.join(STATE_DIR, "ilink-processing.json")
        with open(proc, "w") as f:
            json.dump({"msgs": [{"x": 1}]}, f)
        code, out = run_health()
        self.assertEqual(code, 0)
        self.assertIn("processing in flight", out)


if __name__ == "__main__":
    unittest.main()
