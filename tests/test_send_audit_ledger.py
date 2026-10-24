#!/usr/bin/env python3
"""Tests for the send audit log and the send idempotency ledger
(BACKLOG items 1 and 2, from the 2026-10-23 duplicate-send diagnosis).

The diagnosis showed the failure mode is a second, independent send
invocation (fresh client_id), which server-side client_id dedupe cannot
stop. So: every send attempt is audited (timestamp, direction,
recipient, client_id, result, error type — never message text or
tokens), and a repeated send with the same --key is a no-op.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_send_audit_ledger.py
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
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-audit-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = tempfile.mkdtemp(prefix="ilink-test-state-")
import ilink  # noqa: E402


def _pin_paths(testcase):
    ilink.BRIDGE = DATA_DIR
    ilink.SESSION_FILE = os.path.join(DATA_DIR, "session.json")
    ilink.CONTACTS_FILE = os.path.join(DATA_DIR, "contacts.json")
    ilink.SEEN_FILE = os.path.join(DATA_DIR, "seen_ids.json")
    ilink.SEND_AUDIT_FILE = os.path.join(DATA_DIR, "send_audit.jsonl")
    ilink.SEND_LEDGER_FILE = os.path.join(DATA_DIR, "send_ledger.json")
    with open(ilink.SESSION_FILE, "w") as f:
        json.dump({"bot_token": "tok"}, f)
    with open(ilink.CONTACTS_FILE, "w") as f:
        json.dump({"u1": {"context_token": "ctx-1"}}, f)
    for p in (ilink.SEND_AUDIT_FILE, ilink.SEND_LEDGER_FILE):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def _run(func, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = func(*args, **kwargs)
    return code, buf.getvalue()


def _audit_lines():
    try:
        with open(ilink.SEND_AUDIT_FILE) as f:
            return [json.loads(ln) for ln in f if ln.strip()]
    except FileNotFoundError:
        return []


class AuditTest(unittest.TestCase):
    def setUp(self):
        _pin_paths(self)

    def test_send_success_audited(self):
        with mock.patch.object(ilink, "api", return_value={"code": 0}):
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi")
        self.assertEqual(code, 0)
        self.assertIn("SEND_OK", out)
        lines = _audit_lines()
        self.assertEqual(len(lines), 1)
        e = lines[0]
        self.assertEqual(e["direction"], "send")
        self.assertEqual(e["to"], "u1")
        self.assertEqual(e["result"], "ok")
        self.assertEqual(e["attempt"], 1)
        self.assertTrue(e["client_id"].startswith("muse-wechat-"))
        # Audit carries no message text.
        with open(ilink.SEND_AUDIT_FILE) as f:
            self.assertNotIn("hi", f.read())

    def test_send_app_error_audited(self):
        with mock.patch.object(ilink, "api",
                               return_value={"code": 1, "errmsg": "bad"}):
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi")
        self.assertEqual(code, 1)
        self.assertIn("SEND_FAILED", out)
        self.assertEqual(_audit_lines()[0]["result"], "app_error")

    def test_send_exception_audited_both_attempts(self):
        with mock.patch.object(ilink, "api",
                               side_effect=RuntimeError("boom")), \
                mock.patch.object(time, "sleep"):
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi")
        self.assertEqual(code, 1)
        self.assertIn("SEND_ERROR", out)
        lines = _audit_lines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(e["result"] == "exception" for e in lines))
        self.assertTrue(all(e["error"] == "RuntimeError" for e in lines))
        self.assertEqual([e["attempt"] for e in lines], [1, 2])

    def test_proactive_retry_audited(self):
        calls = {"n": 0}

        def fake_api(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"code": 0, "ret": -2, "errmsg": ""}
            return {"code": 0}

        with mock.patch.object(ilink, "api", side_effect=fake_api):
            code, out = _run(ilink.cmd_proactive, "u1", "hello")
        self.assertEqual(code, 0)
        self.assertIn("RETRY_WITHOUT_TOKEN", out)
        self.assertIn("PROACTIVE_OK", out)
        lines = _audit_lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["result"], "app_error")
        self.assertEqual(lines[0]["direction"], "proactive")
        self.assertEqual(lines[1]["result"], "ok")

    def test_audit_write_failure_does_not_change_result(self):
        ilink.SEND_AUDIT_FILE = os.path.join(DATA_DIR, "no-such-dir",
                                             "audit.jsonl")
        with mock.patch.object(ilink, "api", return_value={"code": 0}):
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi")
        self.assertEqual(code, 0)
        self.assertIn("SEND_OK", out)

    def test_audit_cap_trims_old_lines(self):
        with open(ilink.SEND_AUDIT_FILE, "w") as f:
            for i in range(1005):
                f.write(json.dumps({"ts": i, "direction": "old"}) + "\n")
        with mock.patch.object(ilink, "api", return_value={"code": 0}):
            code, _ = _run(ilink.cmd_send, "u1", "ctx-1", "hi")
        self.assertEqual(code, 0)
        lines = _audit_lines()
        self.assertEqual(len(lines), ilink.SEND_AUDIT_CAP)
        self.assertEqual(lines[-1]["direction"], "send")


class LedgerTest(unittest.TestCase):
    def setUp(self):
        _pin_paths(self)

    def test_same_key_second_call_suppressed(self):
        api = mock.Mock(return_value={"code": 0})
        with mock.patch.object(ilink, "api", api):
            code1, out1 = _run(ilink.cmd_send, "u1", "ctx-1", "hi", key="m-1")
            code2, out2 = _run(ilink.cmd_send, "u1", "ctx-1", "hi", key="m-1")
        self.assertEqual((code1, code2), (0, 0))
        self.assertIn("SEND_OK", out1)
        self.assertIn("SEND_ALREADY_SENT", out2)
        self.assertEqual(api.call_count, 1)

    def test_failure_not_recorded_then_retry_ok(self):
        # A failed send leaves an INTENT, not a success: the retry must
        # still go through — reusing the intent's client_id, so a
        # send-succeeded-but-unconfirmed first attempt is deduped by
        # the server instead of delivering twice.
        api = mock.Mock(side_effect=RuntimeError("boom"))
        with mock.patch.object(ilink, "api", api), \
                mock.patch.object(time, "sleep"):
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi", key="m-2")
        self.assertEqual(code, 1)
        with open(ilink.SEND_LEDGER_FILE) as f:
            ent = json.load(f)["m-2"]
        self.assertEqual(ent["status"], "intent")
        failed_cid = api.call_args[0][2]["msg"]["client_id"]
        with mock.patch.object(ilink, "api",
                               return_value={"code": 0}) as api2:
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi", key="m-2")
            code2, out2 = _run(ilink.cmd_send, "u1", "ctx-1", "hi", key="m-2")
        self.assertIn("SEND_OK", out)
        self.assertIn("SEND_ALREADY_SENT", out2)
        self.assertEqual(api2.call_args[0][2]["msg"]["client_id"],
                         failed_cid)

    def test_different_keys_independent(self):
        api = mock.Mock(return_value={"code": 0})
        with mock.patch.object(ilink, "api", api):
            _run(ilink.cmd_send, "u1", "ctx-1", "hi", key="m-1")
            _run(ilink.cmd_send, "u1", "ctx-1", "hi", key="m-2")
        self.assertEqual(api.call_count, 2)

    def test_corrupt_ledger_aborts_keyed_send(self):
        # Old semantics (corrupt reads as empty, send proceeds) were
        # replaced by fail-closed handling (ChatGPT 6th-round review):
        # an empty fallback would forget recorded successes and let a
        # retry resend under a fresh client_id.
        with open(ilink.SEND_LEDGER_FILE, "w") as f:
            f.write("garbage{")
        api = mock.Mock(return_value={"code": 0})
        with mock.patch.object(ilink, "api", api):
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi", key="m-3")
        self.assertEqual(code, 1)
        self.assertIn("LEDGER_CORRUPT", out)
        self.assertNotIn("SEND_OK", out)
        self.assertEqual(api.call_count, 0)

    def test_expired_ledger_entry_ignored(self):
        with open(ilink.SEND_LEDGER_FILE, "w") as f:
            json.dump({"m-old": {"client_id": "c", "at": int(time.time()) - 90000,
                                  "result": "ok"}}, f)
        api = mock.Mock(return_value={"code": 0})
        with mock.patch.object(ilink, "api", api):
            code, out = _run(ilink.cmd_send, "u1", "ctx-1", "hi", key="m-old")
        self.assertIn("SEND_OK", out)
        self.assertEqual(api.call_count, 1)

    def test_media_key_suppressed_without_reupload(self):
        uploads = {"n": 0}

        def fake_upload(*args, **kwargs):
            uploads["n"] += 1
            return {"media": {"encrypt_query_param": "x", "aes_key": "eQ==",
                              "encrypt_type": 1},
                    "cipher_size": 10, "md5": "m", "rawsize": 3}

        path = os.path.join(DATA_DIR, "pic.jpg")
        with open(path, "wb") as f:
            f.write(b"fake-jpeg")
        api = mock.Mock(return_value={"code": 0, "message_id": "mid-1"})
        with mock.patch.object(ilink, "_upload_media", side_effect=fake_upload), \
                mock.patch.object(ilink, "api", api):
            code1, out1 = _run(ilink.cmd_send_media, "u1", "ctx-1", path, 2,
                               key="m-9")
            code2, out2 = _run(ilink.cmd_send_media, "u1", "ctx-1", path, 2,
                               key="m-9")
        self.assertIn("SEND_OK", out1)
        self.assertIn("SEND_ALREADY_SENT", out2)
        self.assertEqual(uploads["n"], 1)
        self.assertEqual(api.call_count, 1)

    def test_proactive_key_suppressed(self):
        api = mock.Mock(return_value={"code": 0})
        with mock.patch.object(ilink, "api", api):
            code1, out1 = _run(ilink.cmd_proactive, "u1", "hello", key="p-1")
            code2, out2 = _run(ilink.cmd_proactive, "u1", "hello", key="p-1")
        self.assertIn("PROACTIVE_OK", out1)
        self.assertIn("SEND_ALREADY_SENT", out2)
        self.assertEqual(api.call_count, 1)


if __name__ == "__main__":
    unittest.main()
