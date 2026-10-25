#!/usr/bin/env python3
"""Tests for ilink.py's stats offline snapshot.

Stats reads local files only (no server calls): today's chatlog
exchanges and send-audit outcomes (local-midnight boundary),
send-ledger size, quote-cache size, media dir usage, session age,
and queue backlog (same strict queue rule as health). A missing file
is a zero, never a failure — exit code is always 0.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_stats.py
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-stats-")
STATE_DIR = tempfile.mkdtemp(prefix="ilink-test-state-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = STATE_DIR
import ilink  # noqa: E402


def _pin_paths():
    ilink.BRIDGE = DATA_DIR
    ilink.SESSION_FILE = os.path.join(DATA_DIR, "session.json")
    ilink.CONTACTS_FILE = os.path.join(DATA_DIR, "contacts.json")
    ilink.SEEN_FILE = os.path.join(DATA_DIR, "seen_ids.json")
    ilink.SEND_LEDGER_FILE = os.path.join(DATA_DIR, "send_ledger.json")
    ilink.SEND_AUDIT_FILE = os.path.join(DATA_DIR, "send_audit.jsonl")
    ilink.QUOTE_CACHE_FILE = os.path.join(DATA_DIR, "quote_cache.json")
    ilink.MEDIA_DIR = os.path.join(DATA_DIR, "media")
    os.environ["ILINK_HOOK_STATE_DIR"] = STATE_DIR


_pin_paths()


class StatsTests(unittest.TestCase):
    def setUp(self):
        _pin_paths()
        for base in (DATA_DIR, STATE_DIR):
            for name in os.listdir(base):
                path = os.path.join(base, name)
                if os.path.isfile(path):
                    os.unlink(path)
                else:
                    shutil.rmtree(path, ignore_errors=True)

    def _run(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ilink.cmd_stats()
        return rc, buf.getvalue()

    def _write(self, name, content):
        with open(os.path.join(DATA_DIR, name), "w") as f:
            f.write(content)

    def test_full_snapshot(self):
        today = time.strftime("%Y-%m-%d", time.localtime())
        now = int(time.time())
        self._write("chatlog.md",
                    f"[{today} 10:00] 用户：hi\n"
                    f"[{today} 10:01] 我：hello\n"
                    "[2000-01-01 10:00] 用户：ancient\n")
        audit = [
            {"ts": now, "result": "ok"},
            {"ts": now, "result": "ok"},
            {"ts": now, "result": "error"},
            {"ts": 1000000000, "result": "ok"},
        ]
        self._write("send_audit.jsonl",
                    "".join(json.dumps(e) + "\n" for e in audit))
        self._write("send_ledger.json", json.dumps({
            "k1": {"client_id": "c1", "at": now, "result": "ok"},
            "k2": {"client_id": "c2", "at": now, "result": "ok"}}))
        self._write("quote_cache.json", json.dumps({
            "by_id": {"m1": {}, "m2": {}}, "by_svr": {"s1": "m1"}}))
        os.makedirs(ilink.MEDIA_DIR, exist_ok=True)
        for name, size in (("a.jpg", 100), ("b.mp4", 200)):
            with open(os.path.join(ilink.MEDIA_DIR, name), "wb") as f:
                f.write(b"x" * size)
        self._write("session.json", json.dumps(
            {"bot_token": "TOK", "login_at": now - 3600}))
        with open(os.path.join(STATE_DIR, "ilink-pending.json"), "w") as f:
            json.dump({"msgs": [{}, {}]}, f)
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("STATS chatlog today: 1 user / 1 bot lines", out)
        self.assertIn("STATS sends today: 2 ok / 1 failed", out)
        self.assertIn("STATS ledger: 2 entries", out)
        self.assertIn("STATS quote cache: 2 entries, 1 svr mappings", out)
        self.assertIn("STATS media: 2 file(s)", out)
        self.assertIn("STATS session: logged in", out)
        self.assertIn("STATS queue: 2 pending", out)

    def test_corrupt_queue_reported_not_counted(self):
        with open(os.path.join(STATE_DIR, "ilink-pending.json"), "w") as f:
            f.write("{invalid")
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("STATS queue: pending CORRUPT", out)

    def test_missing_everything_is_zeros(self):
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("STATS chatlog today: 0 user / 0 bot lines", out)
        self.assertIn("STATS sends today: 0 ok / 0 failed", out)
        self.assertIn("STATS ledger: 0 entries", out)
        self.assertIn("STATS quote cache: 0 entries, 0 svr mappings", out)
        self.assertIn("STATS media: no dir yet", out)
        self.assertIn("STATS session: none", out)
        self.assertIn("STATS queue: 0 pending", out)


if __name__ == "__main__":
    unittest.main()
