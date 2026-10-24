#!/usr/bin/env python3
"""Tests for the ledger intent + stable client_id + lock upgrade
(ChatGPT 5th-round review, item 3).

The old ledger was check -> send -> record, which left two windows:
two processes racing on one key could both send, and a crash between
a successful send and its ledger write meant the retry sent again with
a fresh client_id. Now a keyed send holds an inter-process lock across
the whole call, persists an intent (with a stable client_id) BEFORE
calling sendmessage, and only flips it to sent on confirmed success —
so a retry reuses the same client_id and the server's client_id dedupe
covers the send-succeeded-but-unconfirmed window.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_ledger_intent.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-intent-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
import ilink  # noqa: E402


def _pin_paths(testcase):
    ilink.BRIDGE = DATA_DIR
    ilink.SESSION_FILE = os.path.join(DATA_DIR, "session.json")
    ilink.CONTACTS_FILE = os.path.join(DATA_DIR, "contacts.json")
    ilink.SEEN_FILE = os.path.join(DATA_DIR, "seen_ids.json")
    ilink.SEND_AUDIT_FILE = os.path.join(DATA_DIR, "send_audit.jsonl")
    ilink.SEND_LEDGER_FILE = os.path.join(DATA_DIR, "send_ledger.json")
    ilink.SEND_LEDGER_LOCK_FILE = os.path.join(DATA_DIR, "send_ledger.lock")
    with open(ilink.SESSION_FILE, "w") as f:
        json.dump({"bot_token": "tok"}, f)
    with open(ilink.CONTACTS_FILE, "w") as f:
        json.dump({"u1": {"context_token": "ctx-1"}}, f)
    for p in (ilink.SEND_AUDIT_FILE, ilink.SEND_LEDGER_FILE,
              ilink.SEND_LEDGER_LOCK_FILE):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def _run(func, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = func(*args, **kwargs)
    return code, buf.getvalue()


def _ledger():
    try:
        with open(ilink.SEND_LEDGER_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


class IntentTest(unittest.TestCase):
    def setUp(self):
        _pin_paths(self)

    def test_concurrent_same_key_calls_api_once(self):
        # Two threads racing on one key: the lock serializes them, so
        # the second finds the sent entry instead of sending again.
        calls = []

        def slow_api(*args, **kwargs):
            time.sleep(0.3)
            calls.append(args[2]["msg"]["client_id"])
            return {"code": 0}

        # NOTE: per-thread stdout capture is not used here on purpose:
        # redirect_stdout swaps a process-global stream, so two racing
        # threads cannot each own their output. Codes + API count +
        # ledger state prove the serialization instead.
        codes = []

        def worker():
            with contextlib.redirect_stdout(io.StringIO()):
                codes.append(ilink.cmd_send("u1", "ctx-1", "hi",
                                            key="k-race"))

        with mock.patch.object(ilink, "api", side_effect=slow_api):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(sorted(codes), [0, 0])
        self.assertEqual(len(calls), 1)
        self.assertEqual(_ledger()["k-race"]["status"], "sent")

    def test_crash_before_mark_reuses_client_id(self):
        # Simulate a crash between a successful send and its ledger
        # marking: the first call raises, the retry must reuse the
        # intent's client_id (server dedupe then covers the resend).
        api_cids = []

        def api(*args, **kwargs):
            api_cids.append(args[2]["msg"]["client_id"])
            return {"code": 0}

        orig_mark = ilink._mark_send_ledger_sent
        calls = {"n": 0}

        def flaky_mark(key, cid):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("crash before mark")
            return orig_mark(key, cid)

        with mock.patch.object(ilink, "api", side_effect=api), \
                mock.patch.object(ilink, "_mark_send_ledger_sent",
                                  side_effect=flaky_mark):
            with self.assertRaises(RuntimeError):
                _run(ilink.cmd_send, "u1", "ctx-1", "hi", key="k-crash")
        self.assertEqual(_ledger()["k-crash"]["status"], "intent")
        with mock.patch.object(ilink, "api", side_effect=api):
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi",
                             key="k-crash")
        self.assertEqual(code, 0)
        self.assertIn("SEND_OK", out)
        self.assertEqual(len(api_cids), 2)
        self.assertEqual(api_cids[0], api_cids[1])
        self.assertEqual(_ledger()["k-crash"]["status"], "sent")

    def test_keyless_send_takes_no_lock(self):
        # The decorator must not touch the lock at all without a key:
        # a raising lock proves the keyless path never enters it.
        with mock.patch.object(
                ilink, "_send_ledger_lock",
                side_effect=AssertionError("lock must not be taken")), \
                mock.patch.object(ilink, "api", return_value={"code": 0}):
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi")
        self.assertEqual(code, 0)
        self.assertIn("SEND_OK", out)

    def test_legacy_sent_entry_still_counts(self):
        # Pre-intent entries (no status field, result 'ok') count as
        # sent, so old ledger files keep working after the upgrade.
        with open(ilink.SEND_LEDGER_FILE, "w") as f:
            json.dump({"k-old": {"client_id": "c-old",
                                  "at": int(time.time()),
                                  "result": "ok"}}, f)
        api = mock.Mock(return_value={"code": 0})
        with mock.patch.object(ilink, "api", api):
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi", key="k-old")
        self.assertEqual(code, 0)
        self.assertIn("SEND_ALREADY_SENT", out)
        self.assertEqual(api.call_count, 0)


class MediaCapTest(unittest.TestCase):
    def setUp(self):
        _pin_paths(self)
        self.path = os.path.join(DATA_DIR, "pic.jpg")
        with open(self.path, "wb") as f:
            f.write(b"fake-jpeg")

    def _fake_upload(self, *args, **kwargs):
        return {"media": {"encrypt_query_param": "x", "aes_key": "eQ==",
                          "encrypt_type": 1},
                "cipher_size": 10, "md5": "m", "rawsize": 3}

    def test_oversize_refused_before_read_or_upload(self):
        # A nonexistent path plus a huge reported size must hit the
        # size refusal, not a read error: the size check precedes any
        # read. A real small file with a huge reported size must also
        # never reach the uploader.
        uploads = mock.Mock(side_effect=self._fake_upload)
        with mock.patch("os.path.getsize",
                        return_value=ilink.MEDIA_MAX_BYTES + 1), \
                mock.patch.object(ilink, "_upload_media", uploads):
            code, out = _run(ilink.cmd_send_media, "u1", "ctx-1",
                             "/nonexistent/pic.jpg", 2)
            self.assertEqual(code, 1)
            self.assertIn("MEDIA_TOO_LARGE", out)
            code, out = _run(ilink.cmd_send_media, "u1", "ctx-1",
                             self.path, 2)
            self.assertEqual(code, 1)
            self.assertIn("MEDIA_TOO_LARGE", out)
        self.assertEqual(uploads.call_count, 0)

    def test_at_cap_size_still_sends(self):
        # Exactly at the cap is allowed; only strictly over refuses.
        api = mock.Mock(return_value={"code": 0, "message_id": "mid-1"})
        with mock.patch("os.path.getsize",
                        return_value=ilink.MEDIA_MAX_BYTES), \
                mock.patch.object(ilink, "_upload_media",
                                  side_effect=self._fake_upload), \
                mock.patch.object(ilink, "api", api):
            code, out = _run(ilink.cmd_send_media, "u1", "ctx-1",
                             self.path, 2)
        self.assertEqual(code, 0)
        self.assertIn("SEND_OK", out)


if __name__ == "__main__":
    unittest.main()
