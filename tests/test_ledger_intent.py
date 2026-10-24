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
        # marking. New semantics (ledger-write failures are loud, not
        # silent): the first call reports LEDGER_WRITE_FAILED with a
        # nonzero exit instead of SEND_OK or a bare exception, and the
        # retry must reuse the intent's client_id (server dedupe then
        # covers the resend).
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
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi",
                             key="k-crash")
        self.assertEqual(code, 1)
        self.assertIn("LEDGER_WRITE_FAILED", out)
        self.assertNotIn("SEND_OK", out)
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


class LedgerWriteFailureTest(unittest.TestCase):
    """Ledger write failures must be loud, never silent (ChatGPT
    follow-up, point 3): an unpersisted intent aborts the send before
    any API call, and an unpersisted success marking is not SEND_OK."""

    def setUp(self):
        _pin_paths(self)

    def _audit_entries(self):
        try:
            with open(ilink.SEND_AUDIT_FILE) as f:
                return [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            return []

    def test_intent_write_failure_aborts_send_before_api(self):
        api = mock.Mock(return_value={"code": 0})
        with mock.patch.object(ilink, "api", api), \
                mock.patch.object(ilink, "_write_send_ledger",
                                  side_effect=OSError("disk full")):
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi",
                             key="k-intent-fail")
        self.assertEqual(code, 1)
        self.assertIn("LEDGER_ERROR", out)
        self.assertNotIn("SEND_OK", out)
        self.assertEqual(api.call_count, 0)
        errors = [e for e in self._audit_entries()
                  if e.get("result") == "ledger_error"]
        self.assertTrue(errors)
        self.assertEqual(errors[0]["error"], "OSError")

    def test_mark_write_failure_not_success_then_retry_reuses_client_id(self):
        # Intent write succeeds, the first success marking fails, the
        # retry's marking succeeds: first call must not claim SEND_OK,
        # and both API calls must share one client_id (server dedupe
        # covers the resend, so the user still gets exactly one msg).
        api_cids = []

        def api(*args, **kwargs):
            api_cids.append(args[2]["msg"]["client_id"])
            return {"code": 0}

        orig_write = ilink._write_send_ledger
        calls = {"n": 0}

        def flaky_write(book):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full on mark")
            return orig_write(book)

        with mock.patch.object(ilink, "api", side_effect=api), \
                mock.patch.object(ilink, "_write_send_ledger",
                                  side_effect=flaky_write):
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi",
                             key="k-mark-fail")
        self.assertEqual(code, 1)
        self.assertIn("LEDGER_WRITE_FAILED", out)
        self.assertNotIn("SEND_OK", out)
        self.assertEqual(_ledger()["k-mark-fail"]["status"], "intent")
        with mock.patch.object(ilink, "api", side_effect=api):
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi",
                             key="k-mark-fail")
        self.assertEqual(code, 0)
        self.assertIn("SEND_OK", out)
        self.assertEqual(len(api_cids), 2)
        self.assertEqual(api_cids[0], api_cids[1])
        self.assertEqual(_ledger()["k-mark-fail"]["status"], "sent")

    def test_media_intent_write_failure_aborts_before_upload(self):
        path = os.path.join(DATA_DIR, "pic.jpg")
        with open(path, "wb") as f:
            f.write(b"fake-jpeg")
        uploads = mock.Mock()
        api = mock.Mock(return_value={"code": 0, "message_id": "mid-1"})
        with mock.patch.object(ilink, "_upload_media", uploads), \
                mock.patch.object(ilink, "api", api), \
                mock.patch.object(ilink, "_write_send_ledger",
                                  side_effect=OSError("disk full")):
            code, out = _run(ilink.cmd_send_media, "u1", "ctx-1",
                             path, 2, key="k-media-fail")
        self.assertEqual(code, 1)
        self.assertIn("LEDGER_ERROR", out)
        self.assertEqual(uploads.call_count, 0)
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


class LedgerFailClosedTest(unittest.TestCase):
    """Fail-closed ledger handling (ChatGPT 6th-round review, P1):
    a lock that cannot be acquired must abort a keyed send instead of
    proceeding unlocked, and a damaged ledger file must abort instead
    of being read as empty (an empty fallback would forget recorded
    successes and let a retry resend under a fresh client_id)."""

    def setUp(self):
        _pin_paths(self)

    def _audit_entries(self):
        try:
            with open(ilink.SEND_AUDIT_FILE) as f:
                return [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            return []

    def test_lock_failure_aborts_keyed_send(self):
        api = mock.Mock(return_value={"code": 0})
        with mock.patch.object(ilink, "api", api), \
                mock.patch.object(ilink.fcntl, "flock",
                                  side_effect=OSError("flock broke")):
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi",
                             key="k-lock-fail")
        self.assertEqual(code, 1)
        self.assertIn("LOCK_ERROR", out)
        self.assertNotIn("SEND_OK", out)
        self.assertEqual(api.call_count, 0)
        errors = [e for e in self._audit_entries()
                  if e.get("result") == "lock_error"]
        self.assertTrue(errors)
        self.assertEqual(errors[0]["error"], "OSError")

    def test_lock_failure_keyless_send_unaffected(self):
        api = mock.Mock(return_value={"code": 0})
        with mock.patch.object(ilink, "api", api), \
                mock.patch.object(ilink.fcntl, "flock",
                                  side_effect=OSError("flock broke")):
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi")
        self.assertEqual(code, 0)
        self.assertIn("SEND_OK", out)
        self.assertEqual(api.call_count, 1)

    def test_fcntl_missing_keeps_noop(self):
        # Non-POSIX platforms have no fcntl module at all: the lock is
        # documented no-op there, and keyed sends must still work.
        api = mock.Mock(return_value={"code": 0})
        with mock.patch.object(ilink, "api", api), \
                mock.patch.object(ilink, "fcntl", None):
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi",
                             key="k-no-fcntl")
        self.assertEqual(code, 0)
        self.assertIn("SEND_OK", out)
        self.assertEqual(api.call_count, 1)

    def test_corrupt_ledger_aborts_keyed_send(self):
        with open(ilink.SEND_LEDGER_FILE, "w") as f:
            f.write("garbage{")
        api = mock.Mock(return_value={"code": 0})
        with mock.patch.object(ilink, "api", api):
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi",
                             key="k-corrupt")
        self.assertEqual(code, 1)
        self.assertIn("LEDGER_CORRUPT", out)
        self.assertNotIn("SEND_OK", out)
        self.assertEqual(api.call_count, 0)
        # The damaged file is left in place for inspection, not
        # silently replaced by an empty ledger.
        with open(ilink.SEND_LEDGER_FILE) as f:
            self.assertEqual(f.read(), "garbage{")

    def test_non_object_ledger_aborts_keyed_send(self):
        with open(ilink.SEND_LEDGER_FILE, "w") as f:
            json.dump([1, 2], f)
        api = mock.Mock(return_value={"code": 0})
        with mock.patch.object(ilink, "api", api):
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi",
                             key="k-list-ledger")
        self.assertEqual(code, 1)
        self.assertIn("LEDGER_CORRUPT", out)
        self.assertEqual(api.call_count, 0)

    def test_missing_ledger_file_sends_normally(self):
        # Missing is not corrupt: a fresh deploy has no ledger yet.
        api = mock.Mock(return_value={"code": 0})
        with mock.patch.object(ilink, "api", api):
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi",
                             key="k-fresh")
        self.assertEqual(code, 0)
        self.assertIn("SEND_OK", out)
        self.assertEqual(api.call_count, 1)

    def test_corrupt_ledger_media_intent_aborts_before_upload(self):
        with open(ilink.SEND_LEDGER_FILE, "w") as f:
            f.write("garbage{")
        api = mock.Mock(return_value={"code": 0})
        upload = mock.Mock()
        with mock.patch.object(ilink, "api", api), \
                mock.patch.object(ilink, "_upload_media", upload):
            code, out = _run(ilink.cmd_send_media, "u1", "ctx-1",
                             "/nonexistent/x.png", 2, key="k-media-corrupt")
        self.assertEqual(code, 1)
        self.assertIn("LEDGER_CORRUPT", out)
        self.assertEqual(upload.call_count, 0)
        self.assertEqual(api.call_count, 0)

    def test_mark_load_corrupt_reports_write_failed(self):
        # Ledger readable at intent time, damaged before the success
        # marking: same unconfirmed exit as a mark write failure (the
        # message may already be delivered, but success is unconfirmed
        # and the retry reuses the intent's client_id).
        api = mock.Mock(return_value={"code": 0})
        loads = [{}, ilink.LedgerCorrupt("boom")]
        with mock.patch.object(ilink, "api", api), \
                mock.patch.object(ilink, "_load_send_ledger",
                                  side_effect=loads):
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi",
                             key="k-mark-corrupt")
        self.assertEqual(code, 1)
        self.assertIn("LEDGER_WRITE_FAILED", out)
        self.assertNotIn("SEND_OK", out)
        self.assertEqual(api.call_count, 1)
