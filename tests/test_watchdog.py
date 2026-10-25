#!/usr/bin/env python3
"""Tests for ilink.py's watchdog dead-man's switch.

The hook touches ilink-heartbeat on every run (quiet runs included);
watchdog reads that heartbeat's age and reports WATCHDOG_OK (fresh),
WATCHDOG_ALERT (first stale sighting, exit 1), WATCHDOG_STALE
(repeat stale sightings, exit 1), WATCHDOG_RECOVERED (fresh again
after an alert, exit 0), WATCHDOG_DISABLED (maintenance flag file
present, exit 0), or WATCHDOG_ALERT when no heartbeat exists yet.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_watchdog.py

Pinned behavior:
- alert transitions live in ilink-watchdog-state.json in the hook
  state dir, so a cron driver stays silent on healthy runs and
  reports the first stale sighting and the recovery exactly once;
- the threshold is a parameter (tests pass 30 minutes explicitly),
  not the module default, so a default change cannot silently move
  these tests;
- the maintenance flag silences everything even when stale.
"""
import contextlib
import io
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-watchdog-")
STATE_DIR = tempfile.mkdtemp(prefix="ilink-test-state-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = STATE_DIR
import ilink  # noqa: E402


def _pin_paths():
    ilink.BRIDGE = DATA_DIR
    os.environ["ILINK_HOOK_STATE_DIR"] = STATE_DIR


_pin_paths()


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        _pin_paths()
        for name in os.listdir(STATE_DIR):
            path = os.path.join(STATE_DIR, name)
            if os.path.isfile(path):
                os.unlink(path)
        self.hb = os.path.join(STATE_DIR, "ilink-heartbeat")

    def _run(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ilink.cmd_watchdog(max_age_mins=30)
        return rc, buf.getvalue()

    def _touch(self, age_secs=0):
        with open(self.hb, "w") as f:
            f.write("beat")
        if age_secs:
            old = int(time.time()) - age_secs
            os.utime(self.hb, (old, old))

    def test_fresh_heartbeat_ok(self):
        self._touch()
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("WATCHDOG_OK", out)

    def test_stale_alert_once_then_stale(self):
        self._touch(age_secs=3600)
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("WATCHDOG_ALERT", out)
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("WATCHDOG_STALE", out)
        self.assertNotIn("WATCHDOG_ALERT", out)

    def test_recovery_reported_once(self):
        self._touch(age_secs=3600)
        self._run()
        self._touch()
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("WATCHDOG_RECOVERED", out)
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("WATCHDOG_OK", out)

    def test_no_heartbeat_alerts(self):
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("WATCHDOG_ALERT", out)
        self.assertIn("no heartbeat yet", out)

    def test_disabled_flag_silences_stale(self):
        self._touch(age_secs=3600)
        with open(os.path.join(STATE_DIR, "ilink-watchdog-off"), "w") as f:
            f.write("maintenance")
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("WATCHDOG_DISABLED", out)


if __name__ == "__main__":
    unittest.main()
