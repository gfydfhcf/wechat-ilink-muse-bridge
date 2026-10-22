#!/usr/bin/env python3
"""Tests for daemon --llm history persistence.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_daemon_history.py

History used to live only in memory: restarting the daemon forgot the
conversation. It is now persisted to llm_history.json in the data dir
and reloaded lazily, per user, capped at LLM_HISTORY messages.
"""
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-daemon-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["LLM_API_KEY"] = "test-key"

import daemon  # noqa: E402

REQUESTS = []


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def fake_urlopen(req, timeout=0):
    REQUESTS.append(json.loads(req.data))
    body = {"choices": [{"message": {"content": "好的"}}]}
    return FakeResponse(json.dumps(body).encode())


class DaemonHistoryTest(unittest.TestCase):
    def setUp(self):
        # Re-pin paths per-test: other modules set ILINK_DATA_DIR too,
        # and the restart test reloads the module from the current env.
        daemon.DATA_DIR = DATA_DIR
        daemon.HISTORY_FILE = os.path.join(DATA_DIR, "llm_history.json")
        daemon._history = None
        if os.path.exists(daemon.HISTORY_FILE):
            os.remove(daemon.HISTORY_FILE)
        REQUESTS.clear()
        self._orig = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        self.addCleanup(self._restore)

    def _restore(self):
        urllib.request.urlopen = self._orig
        daemon._history = None

    def test_history_is_persisted(self):
        self.assertEqual(daemon.llm_reply("u1", "你好"), "好的")
        with open(daemon.HISTORY_FILE) as f:
            saved = json.load(f)
        self.assertEqual(saved["u1"], [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "好的"},
        ])

    def test_restart_continues_conversation(self):
        daemon.llm_reply("u1", "我叫小明")
        # simulate a daemon restart: fresh in-memory state, same data dir
        importlib.reload(daemon)
        daemon.DATA_DIR = DATA_DIR
        daemon.HISTORY_FILE = os.path.join(DATA_DIR, "llm_history.json")
        daemon.llm_reply("u1", "我叫什么")
        sent = REQUESTS[-1]["messages"]
        contents = [m["content"] for m in sent]
        self.assertIn("我叫小明", contents)
        self.assertIn("好的", contents)

    def test_histories_are_per_user(self):
        daemon.llm_reply("u1", "甲")
        daemon.llm_reply("u2", "乙")
        with open(daemon.HISTORY_FILE) as f:
            saved = json.load(f)
        self.assertEqual(saved["u1"][0]["content"], "甲")
        self.assertEqual(saved["u2"][0]["content"], "乙")

    def test_corrupt_history_file_starts_fresh(self):
        with open(daemon.HISTORY_FILE, "w") as f:
            f.write("{not json")
        daemon._history = None
        self.assertEqual(daemon.llm_reply("u1", "hi"), "好的")

    def test_cap_rolls_oldest(self):
        for i in range(daemon.LLM_HISTORY):  # each turn adds 2 messages
            daemon.llm_reply("u1", f"m{i}")
        hist = daemon._load_history()["u1"]
        self.assertEqual(len(hist), daemon.LLM_HISTORY)


if __name__ == "__main__":
    unittest.main()
