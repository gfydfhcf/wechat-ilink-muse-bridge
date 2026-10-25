#!/usr/bin/env python3
"""Tests for the new-release reminder (BACKLOG item 6).

version-check compares the local VERSION constant with the upstream
latest release tag. With --notify it sends one proactive notification
per release tag (recorded in version_notify.json) to the primary owner
with a cached context_token.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_version_check.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-version-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = tempfile.mkdtemp(prefix="ilink-test-state-")
import ilink  # noqa: E402

RELEASE = {"tag": "v1.2.0"}
SENDS = []
REAL_URL_OPEN = urllib.request.urlopen


class FakeResp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def fake_urlopen(req, timeout=None):
    if RELEASE["tag"] is None:
        raise OSError("network down")
    return FakeResp({"tag_name": RELEASE["tag"]})


def fake_api(*args, **kwargs):
    path = args[1] if len(args) > 1 else ""
    if "sendmessage" in path:
        SENDS.append(path)
        return {"code": 0, "message_id": 999}
    raise AssertionError(f"unexpected api call: {path}")


def run_check(notify=False):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ilink.cmd_version_check(notify=notify)
    return code, buf.getvalue()


class VersionCheckTest(unittest.TestCase):
    def setUp(self):
        ilink.BRIDGE = DATA_DIR
        ilink.VERSION = "1.2.0"
        ilink.SESSION_FILE = os.path.join(DATA_DIR, "session.json")
        ilink.CONTACTS_FILE = os.path.join(DATA_DIR, "contacts.json")
        ilink.SEND_AUDIT_FILE = os.path.join(DATA_DIR, "send_audit.jsonl")
        ilink.SEND_LEDGER_FILE = os.path.join(DATA_DIR, "send_ledger.json")
        ilink.QUOTE_CACHE_FILE = os.path.join(DATA_DIR, "quote_cache.json")
        ilink.QUOTE_CACHE_LOCK_FILE = os.path.join(DATA_DIR, "quote_cache.lock")
        ilink.VERSION_NOTIFY_FILE = os.path.join(DATA_DIR, "version_notify.json")
        for name in os.listdir(DATA_DIR):
            path = os.path.join(DATA_DIR, name)
            if os.path.isfile(path):
                os.remove(path)
        with open(ilink.SESSION_FILE, "w") as f:
            json.dump({"bot_token": "tok", "cursor": ""}, f)
        with open(ilink.CONTACTS_FILE, "w") as f:
            json.dump({"owner-1": {"context_token": "ctx",
                                   "updated_at": int(time.time())}}, f)
        with open(os.path.join(DATA_DIR, "owner.json"), "w") as f:
            json.dump({"owner_ids": ["owner-1"]}, f)
        ilink.api = fake_api
        urllib.request.urlopen = fake_urlopen
        RELEASE["tag"] = "v1.2.0"
        SENDS.clear()

    def tearDown(self):
        urllib.request.urlopen = REAL_URL_OPEN

    def test_version_compare(self):
        self.assertEqual(ilink._compare_versions("1.2.0", "v1.2.0"), 0)
        self.assertEqual(ilink._compare_versions("1.2.0", "1.3.0"), -1)
        self.assertEqual(ilink._compare_versions("1.10.0", "1.9.9"), 1)
        self.assertEqual(ilink._compare_versions("1.2", "1.2.0"), 0)

    def test_up_to_date(self):
        code, out = run_check()
        self.assertEqual(code, 0)
        self.assertIn("UP_TO_DATE", out)
        self.assertEqual(SENDS, [])

    def test_update_available_without_notify(self):
        RELEASE["tag"] = "v1.3.0"
        code, out = run_check()
        self.assertEqual(code, 0)
        self.assertIn("UPDATE_AVAILABLE:v1.3.0", out)
        self.assertEqual(SENDS, [])

    def test_notify_once_per_tag(self):
        RELEASE["tag"] = "v1.3.0"
        code, out = run_check(notify=True)
        self.assertEqual(code, 0)
        self.assertIn("UPDATE_AVAILABLE:v1.3.0", out)
        self.assertEqual(len(SENDS), 1)
        with open(ilink.VERSION_NOTIFY_FILE) as f:
            self.assertEqual(json.load(f)["last_notified_tag"], "v1.3.0")
        # Second run for the same tag sends nothing.
        code, _ = run_check(notify=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(SENDS), 1)

    def test_fetch_failure(self):
        RELEASE["tag"] = None
        code, out = run_check()
        self.assertEqual(code, 1)
        self.assertIn("VERSION_CHECK_FAILED", out)

    def test_notify_without_context_token(self):
        RELEASE["tag"] = "v1.3.0"
        with open(ilink.CONTACTS_FILE, "w") as f:
            json.dump({}, f)
        code, out = run_check(notify=True)
        self.assertEqual(code, 1)
        self.assertIn("NO_CONTEXT_TOKEN", out)
        self.assertEqual(SENDS, [])


if __name__ == "__main__":
    unittest.main()
