#!/usr/bin/env python3
"""Tests for ilink.py's rotate-chatlog monthly archive.

Past-month chatlog.md lines move into per-month files
(chatlog-YYYY-MM.md); current-month and undated lines stay in place,
so the worker's "read the last 30 lines" still works after rotation.
A second run finds nothing old (ROTATE_NOTHING), and a missing
chatlog is ROTATE_NOTHING, not an error.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_chatlog_rotate.py
"""
import contextlib
import io
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-rotate-")
STATE_DIR = tempfile.mkdtemp(prefix="ilink-test-state-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = STATE_DIR
import ilink  # noqa: E402


def _pin_paths():
    ilink.BRIDGE = DATA_DIR
    os.environ["ILINK_HOOK_STATE_DIR"] = STATE_DIR


_pin_paths()


class RotateChatlogTests(unittest.TestCase):
    def setUp(self):
        _pin_paths()
        for name in os.listdir(DATA_DIR):
            path = os.path.join(DATA_DIR, name)
            if os.path.isfile(path):
                os.unlink(path)
        self.chatlog = os.path.join(DATA_DIR, "chatlog.md")

    def _run(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ilink.cmd_rotate_chatlog()
        return rc, buf.getvalue()

    def _read(self, path):
        with open(path) as f:
            return f.read()

    def test_archives_past_months_keeps_current_and_undated(self):
        today = time.strftime("%Y-%m-%d", time.localtime())
        with open(self.chatlog, "w") as f:
            f.write("[2000-01-05 10:00] 用户：old january\n")
            f.write("[2000-02-05 10:00] 我：old february\n")
            f.write(f"[{today} 10:00] 用户：new\n")
            f.write("no date prefix line\n")
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("ROTATE_OK", out)
        self.assertIn("old january",
                      self._read(os.path.join(DATA_DIR, "chatlog-2000-01.md")))
        self.assertIn("old february",
                      self._read(os.path.join(DATA_DIR, "chatlog-2000-02.md")))
        rest = self._read(self.chatlog)
        self.assertIn("new", rest)
        self.assertIn("no date prefix line", rest)
        self.assertNotIn("old january", rest)
        self.assertNotIn("old february", rest)

    def test_second_run_is_nothing(self):
        today = time.strftime("%Y-%m-%d", time.localtime())
        with open(self.chatlog, "w") as f:
            f.write("[2000-01-05 10:00] 用户：old\n")
            f.write(f"[{today} 10:00] 用户：new\n")
        self._run()
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("ROTATE_NOTHING", out)

    def test_missing_chatlog_is_nothing(self):
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("ROTATE_NOTHING", out)


if __name__ == "__main__":
    unittest.main()
