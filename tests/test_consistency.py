#!/usr/bin/env python3
"""Tests for the consistency bundle (BACKLOG item 5):

  a) corrupt session/contacts JSON degrades to empty instead of crashing;
  b) send/proactive share one outcome classification rule;
  c) CLI commands with missing args print usage and exit 2, never a bare
     IndexError traceback;
  d) the daemon main loop survives poll_once() itself raising.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_consistency.py
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
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-consistency-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = tempfile.mkdtemp(prefix="ilink-test-state-")
import daemon  # noqa: E402
import ilink  # noqa: E402


def _pin_paths():
    ilink.BRIDGE = DATA_DIR
    ilink.SESSION_FILE = os.path.join(DATA_DIR, "session.json")
    ilink.CONTACTS_FILE = os.path.join(DATA_DIR, "contacts.json")
    ilink.SEEN_FILE = os.path.join(DATA_DIR, "seen_ids.json")
    ilink.SEND_AUDIT_FILE = os.path.join(DATA_DIR, "send_audit.jsonl")
    ilink.SEND_LEDGER_FILE = os.path.join(DATA_DIR, "send_ledger.json")


class CorruptJsonTest(unittest.TestCase):
    def setUp(self):
        _pin_paths()

    def test_corrupt_session_degrades_to_empty(self):
        with open(ilink.SESSION_FILE, "w") as f:
            f.write("{not json")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(ilink.load_session(), {})
        self.assertIn("corrupt session file", err.getvalue())

    def test_corrupt_contacts_degrades_to_empty(self):
        with open(ilink.CONTACTS_FILE, "w") as f:
            f.write("[1, 2")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(ilink.load_contacts(), {})
        self.assertIn("corrupt contacts file", err.getvalue())

    def test_valid_files_still_load(self):
        with open(ilink.SESSION_FILE, "w") as f:
            json.dump({"bot_token": "tok"}, f)
        with open(ilink.CONTACTS_FILE, "w") as f:
            json.dump({"u1": {"context_token": "ctx"}}, f)
        self.assertEqual(ilink.load_session(), {"bot_token": "tok"})
        self.assertEqual(ilink.load_contacts(),
                         {"u1": {"context_token": "ctx"}})


class ClassifyTest(unittest.TestCase):
    def test_outcome_table(self):
        cases = [
            ({"code": 0}, False, "ok"),
            ({"code": 0, "message_id": "m"}, True, "ok"),
            ({"code": 0}, True, "app_error"),  # media without message_id
            ({"code": -14}, False, "token_expired"),
            ({"code": -14}, True, "token_expired"),
            ({"code": 1}, False, "app_error"),
            ({"code": 1, "message_id": "m"}, True, "app_error"),
        ]
        for data, require_mid, expected in cases:
            with self.subTest(data=data, require_mid=require_mid):
                self.assertEqual(
                    ilink._classify_send_response(data, require_mid), expected)

    def test_proactive_retry_predicate(self):
        # Stale context_token signature: code 0 but ret -2 still retries.
        self.assertTrue(ilink._proactive_needs_tokenless_retry(
            {"code": 0, "ret": -2}))
        self.assertTrue(ilink._proactive_needs_tokenless_retry(
            {"code": 0, "errmsg": "prepare failed: ctx"}))
        self.assertFalse(ilink._proactive_needs_tokenless_retry({"code": 0}))
        self.assertTrue(ilink._proactive_needs_tokenless_retry(
            {"code": 0}, require_message_id=True))


class CliArgsTest(unittest.TestCase):
    def setUp(self):
        _pin_paths()

    def _run_main(self, argv):
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = ilink.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_missing_args_exit_2_with_usage(self):
        for argv in (
            ["ilink.py", "send"],
            ["ilink.py", "send", "u1"],
            ["ilink.py", "send", "u1", "ctx"],
            ["ilink.py", "proactive", "u1"],
            ["ilink.py", "typing", "u1"],
            ["ilink.py", "send-image", "u1", "ctx"],
            ["ilink.py", "login"],
            ["ilink.py"],
        ):
            with self.subTest(argv=argv):
                code, _, err = self._run_main(argv)
                self.assertEqual(code, 2)
                if argv[1:] and argv[1] != "bogus":
                    self.assertIn("usage:", err)

    def test_unknown_command_still_exit_2(self):
        code, _, _ = self._run_main(["ilink.py", "bogus"])
        self.assertEqual(code, 2)


class DaemonPollExceptionTest(unittest.TestCase):
    def test_poll_once_raising_does_not_kill_loop(self):
        calls = []
        orig_argv = sys.argv
        sys.argv = ["daemon.py"]
        self.addCleanup(setattr, sys, "argv", orig_argv)

        def fake_poll():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("subprocess timed out")
            raise KeyboardInterrupt  # second round: escape the loop

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(daemon, "poll_once", side_effect=fake_poll), \
                mock.patch.object(time, "sleep"), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(KeyboardInterrupt):
                daemon.main()
        self.assertEqual(len(calls), 2)
        self.assertIn("POLL_EXCEPTION", out.getvalue())


if __name__ == "__main__":
    unittest.main()
