#!/usr/bin/env python3
"""Tests for ilink.py's preflight pre-login environment check.

Preflight verifies Python >= 3.10, cryptography importable, network
reachability to the iLink API host, and data/media dir writability —
before any login, without touching state. Any HTTP response at all,
even an error status, counts as reachable (it proves network + TLS);
only a transport failure fails the network leg.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_preflight.py
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-preflight-")
STATE_DIR = tempfile.mkdtemp(prefix="ilink-test-state-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = STATE_DIR
import ilink  # noqa: E402


def _pin_paths():
    ilink.BRIDGE = DATA_DIR
    ilink.MEDIA_DIR = os.path.join(DATA_DIR, "media")
    os.makedirs(ilink.MEDIA_DIR, exist_ok=True)
    os.environ["ILINK_HOOK_STATE_DIR"] = STATE_DIR


_pin_paths()


class PreflightTests(unittest.TestCase):
    def setUp(self):
        _pin_paths()

    def _run(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ilink.cmd_preflight()
        return rc, buf.getvalue()

    def test_all_pass(self):
        with mock.patch("urllib.request.urlopen",
                         return_value=object()):
            rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("PREFLIGHT_OK", out)

    def test_http_error_still_counts_as_reachable(self):
        err = urllib.error.HTTPError("https://ilinkai.weixin.qq.com", 404,
                                     "Not Found", {}, io.BytesIO())
        with mock.patch("urllib.request.urlopen", side_effect=err):
            rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("PREFLIGHT network: PASS", out)

    def test_network_failure_fails(self):
        with mock.patch("urllib.request.urlopen",
                         side_effect=urllib.error.URLError("dns")):
            rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("PREFLIGHT_FAILED: network", out)

    def test_missing_cryptography_fails(self):
        with mock.patch("urllib.request.urlopen",
                         return_value=object()), \
                mock.patch.dict(sys.modules, {"cryptography": None}):
            rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("PREFLIGHT_FAILED: cryptography", out)

    def test_old_python_fails(self):
        with mock.patch("urllib.request.urlopen",
                         return_value=object()), \
                mock.patch.object(sys, "version_info",
                                  (3, 9, 0, "final", 0)):
            rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("PREFLIGHT_FAILED: python", out)

    def test_unwritable_dirs_fail(self):
        ilink.BRIDGE = "/nonexistent-xyz-dir"
        ilink.MEDIA_DIR = "/nonexistent-xyz-media"
        with mock.patch("urllib.request.urlopen",
                         return_value=object()):
            rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("data dir", out)
        self.assertIn("media dir", out)


if __name__ == "__main__":
    unittest.main()
