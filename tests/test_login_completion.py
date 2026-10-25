#!/usr/bin/env python3
"""Tests for login completion (BACKLOG item 9).

Beyond the old confirmed / expired / canceled / timeout handling,
cmd_login now also covers branches the old loop could only fail or
time out on: expired auto-refresh (bounded, max 3), binded /
binded_redirect as success when a token is present, one redirect
follow per round, and verification challenges (verify URL surfaced,
typed code resubmitted).

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_login_completion.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-login-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = tempfile.mkdtemp(prefix="ilink-test-state-")
import ilink  # noqa: E402

SCRIPT = {"statuses": [], "qrcodes": []}
STDIN_LINES = []


def fake_api(*args, **kwargs):
    path = args[1] if len(args) > 1 else ""
    if "get_bot_qrcode" in path:
        if SCRIPT["qrcodes"]:
            return SCRIPT["qrcodes"].pop(0)
        return {"qrcode": "QR-NEW", "qrcode_img_content": ""}
    if "get_qrcode_status" in path:
        if SCRIPT["statuses"]:
            return SCRIPT["statuses"].pop(0)
        return {"status": "canceled"}
    if path == "redirect/path":
        return {"status": "confirmed", "bot_token": "tok-redirect"}
    raise AssertionError(f"unexpected api call: {path}")


def run_login(qrcode="QR-1", wait_secs=30, stdin_text=""):
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin_text)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ilink.cmd_login(qrcode, wait_secs)
    finally:
        sys.stdin = old_stdin
    return code, buf.getvalue()


def read_session():
    with open(ilink.SESSION_FILE) as f:
        return json.load(f)


class LoginCompletionTest(unittest.TestCase):
    def setUp(self):
        ilink.BRIDGE = DATA_DIR
        ilink.SESSION_FILE = os.path.join(DATA_DIR, "session.json")
        for name in os.listdir(DATA_DIR):
            path = os.path.join(DATA_DIR, name)
            if os.path.isfile(path):
                os.remove(path)
        with open(ilink.SESSION_FILE, "w") as f:
            json.dump({}, f)
        ilink.api = fake_api
        SCRIPT["statuses"] = []
        SCRIPT["qrcodes"] = []

    def test_confirmed_unchanged(self):
        SCRIPT["statuses"] = [{"status": "confirmed", "bot_token": "tok-1"}]
        code, out = run_login()
        self.assertEqual(code, 0)
        self.assertIn("LOGIN_OK", out)
        self.assertEqual(read_session()["bot_token"], "tok-1")

    def test_expired_auto_refresh_then_confirmed(self):
        SCRIPT["statuses"] = [
            {"status": "expired"},
            {"status": "confirmed", "bot_token": "tok-2"}]
        SCRIPT["qrcodes"] = [{"qrcode": "QR-2", "qrcode_img_content": ""}]
        code, out = run_login()
        self.assertEqual(code, 0)
        self.assertIn("QR_CODE:QR-2", out)
        self.assertIn("LOGIN_OK", out)
        self.assertEqual(read_session()["bot_token"], "tok-2")

    def test_expired_refresh_capped(self):
        SCRIPT["statuses"] = [{"status": "expired"}] * 5
        SCRIPT["qrcodes"] = [{"qrcode": f"QR-{i}",
                              "qrcode_img_content": ""} for i in range(3)]
        code, out = run_login()
        self.assertEqual(code, 1)
        self.assertIn("LOGIN_FAILED:expired", out)
        self.assertEqual(SCRIPT["qrcodes"], [])

    def test_binded_with_token_is_success(self):
        SCRIPT["statuses"] = [{"status": "binded", "bot_token": "tok-3"}]
        code, out = run_login()
        self.assertEqual(code, 0)
        self.assertIn("LOGIN_OK", out)
        self.assertEqual(read_session()["bot_token"], "tok-3")

    def test_binded_redirect_with_token_is_success(self):
        SCRIPT["statuses"] = [{"binded_redirect": True,
                               "bot_token": "tok-4"}]
        code, out = run_login()
        self.assertEqual(code, 0)
        self.assertEqual(read_session()["bot_token"], "tok-4")

    def test_binded_without_token_fails(self):
        SCRIPT["statuses"] = [{"status": "binded"}]
        code, out = run_login()
        self.assertEqual(code, 1)
        self.assertIn("LOGIN_FAILED:binded_no_token", out)

    def test_redirect_followed_to_confirmed(self):
        SCRIPT["statuses"] = [
            {"status": "waiting", "redirect_url": "redirect/path"}]
        code, out = run_login()
        self.assertEqual(code, 0)
        self.assertIn("LOGIN_REDIRECT:redirect/path", out)
        self.assertEqual(read_session()["bot_token"], "tok-redirect")

    def test_verify_url_surfaced_then_confirmed(self):
        SCRIPT["statuses"] = [
            {"status": "need_verify",
             "verify_url": "https://example.com/verify"},
            {"status": "confirmed", "bot_token": "tok-6"}]
        code, out = run_login()
        self.assertEqual(code, 0)
        self.assertIn("VERIFY_URL:https://example.com/verify", out)
        self.assertEqual(read_session()["bot_token"], "tok-6")

    def test_verify_code_resubmitted(self):
        seen_urls = []

        def api_with_capture(*args, **kwargs):
            path = args[1] if len(args) > 1 else ""
            if "get_qrcode_status" in path:
                seen_urls.append(path)
                if "verify_code=123456" in path:
                    return {"status": "confirmed", "bot_token": "tok-7"}
                return {"status": "verify_code", "need_verify_code": True}
            return fake_api(*args, **kwargs)

        ilink.api = api_with_capture
        code, out = run_login(stdin_text="123456\n")
        self.assertEqual(code, 0)
        self.assertIn("VERIFY_CODE_REQUIRED", out)
        self.assertTrue(any("verify_code=123456" in u for u in seen_urls))
        self.assertEqual(read_session()["bot_token"], "tok-7")

    def test_canceled_unchanged(self):
        SCRIPT["statuses"] = [{"status": "canceled"}]
        code, out = run_login()
        self.assertEqual(code, 1)
        self.assertIn("LOGIN_FAILED:canceled", out)


if __name__ == "__main__":
    unittest.main()
