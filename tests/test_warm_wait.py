#!/usr/bin/env python3
"""Tests for examples/muse-hook/ilink-warm-wait.sh.

The warm waiter keeps a worker alive briefly after a reply so a
follow-up message skips the cold start. It owns the warm marker the
watch script checks. Three exits are pinned:

  * IDLE_TIMEOUT  quiet for ILINK_WARM_IDLE seconds, marker removed
  * NEW_MESSAGES  a batch appears in pending, marker kept (its stale
                  heartbeat is what re-arms the hook if the worker
                  dies before claiming)
  * TOTAL_CAP     the session's total cap is already in the past
                  (marker left by a dead session) — exits at once

    python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
HELPER = os.path.join(ROOT, "examples", "muse-hook", "ilink-warm-wait.sh")


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash"),
                     "needs bash + jq")
class WarmWaitTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="ilink-warm-")
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        self.env = dict(os.environ)
        self.env.update({"ILINK_HOOK_STATE_DIR": self.state,
                         "ILINK_WARM_IDLE": "2", "ILINK_WARM_POLL": "1",
                         "ILINK_WARM_TOTAL": "1800"})

    def run_helper(self, timeout=15):
        return subprocess.run(["bash", HELPER], env=self.env,
                              capture_output=True, text=True,
                              timeout=timeout)

    def marker(self):
        p = os.path.join(self.state, "ilink-warm.json")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return json.load(f)

    def test_idle_timeout_removes_marker(self):
        r = self.run_helper()
        self.assertEqual(r.stdout.strip(), "IDLE_TIMEOUT")
        self.assertIsNone(self.marker())

    def test_new_messages_exit_keeps_marker(self):
        proc = subprocess.Popen(["bash", HELPER], env=self.env,
                                stdout=subprocess.PIPE, text=True)
        time.sleep(1.2)
        with open(os.path.join(self.state, "ilink-pending.json"), "w") as f:
            json.dump({"msgs": [{"message_id": 1}]}, f)
        out, _ = proc.communicate(timeout=10)
        self.assertEqual(out.strip(), "NEW_MESSAGES")
        self.assertIsNotNone(self.marker())

    def test_dead_session_marker_hits_total_cap(self):
        now = int(time.time())
        with open(os.path.join(self.state, "ilink-warm.json"), "w") as f:
            json.dump({"since": now - 3600, "activity": now - 3600,
                       "heartbeat": now - 3600, "until": now - 1800}, f)
        r = self.run_helper()
        self.assertEqual(r.stdout.strip(), "TOTAL_CAP")
        self.assertIsNone(self.marker())


if __name__ == "__main__":
    unittest.main()
