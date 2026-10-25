#!/usr/bin/env python3
"""Tests for ilink.py's smoke-test end-to-end chain check.

health only inspects local state; smoke-test proves the chain: a live
token probe, local receive readiness, a real proactive text + image
to the owner (unless --no-send), and send-ledger readability. Output
is one SMOKE line per check plus SMOKE_OK / SMOKE_FAILED; exit 0 only
when nothing FAILED (SKIP does not fail).

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_smoke.py

Pinned behavior:
- the login leg reuses health's getconfig probe (no side effects, does
  not touch the message cursor) and deliberately does NOT call
  _report_token_status on -14 — a diagnostic reports, it does not
  flip the token-stale state machine;
- the send legs reuse cmd_proactive / cmd_proactive_media with no key,
  so every run really sends (a keyed repeat would print
  SEND_ALREADY_SENT and prove nothing);
- --no-send skips both send legs without calling them;
- no owner means send FAILs (the bridge is not ready) while the media
  leg skips for lack of a target;
- a corrupt send ledger is a chain FAIL here (health reports it as a
  warning because health never sends).
"""
import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-smoke-")
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
    ilink.MEDIA_DIR = os.path.join(DATA_DIR, "media")
    os.makedirs(ilink.MEDIA_DIR, exist_ok=True)


_pin_paths()


def _write(name, obj):
    with open(os.path.join(DATA_DIR, name), "w") as f:
        json.dump(obj, f)


def run_smoke(no_send=False, to_user_id=None):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ilink.cmd_smoke_test(no_send=no_send, to_user_id=to_user_id)
    return code, buf.getvalue()


class SmokeTest(unittest.TestCase):
    def setUp(self):
        _pin_paths()
        for name in os.listdir(DATA_DIR):
            path = os.path.join(DATA_DIR, name)
            if os.path.isfile(path):
                os.unlink(path)
        _write("session.json", {"bot_token": "TOK", "login_at": 1700000000})
        _write("owner.json", {"owner_id": "o1", "owner_ids": ["o1"]})
        _write("contacts.json", {"o1": {"context_token": "CTX",
                                        "updated_at": 1700000000}})

    def _patched(self, api_ret, pro_ret=0, media_ret=0):
        """Context managers patching the network/send legs."""
        return (mock.patch.object(ilink, "api", return_value=api_ret),
                mock.patch.object(ilink, "cmd_proactive", return_value=pro_ret),
                mock.patch.object(ilink, "cmd_proactive_media", return_value=media_ret))

    def test_all_pass(self):
        api_m, pro_m, media_m = self._patched({"typing_ticket": "T"})
        with api_m, pro_m as pro_mock, media_m as media_mock:
            code, out = run_smoke()
        self.assertEqual(code, 0)
        self.assertIn("SMOKE_OK", out)
        for leg in ("login", "receive", "send", "media", "ledger"):
            self.assertIn(f"SMOKE {leg}: PASS", out)
        pro_mock.assert_called_once_with("o1", ilink._SMOKE_TEXT)
        margs = media_mock.call_args[0]
        self.assertEqual(margs[0], "o1")
        self.assertTrue(margs[1].endswith(".png"))
        self.assertEqual(margs[2], 2)

    def test_login_expired_reports_only(self):
        api_m, pro_m, media_m = self._patched({"code": -14})
        with api_m as api_mock, pro_m, media_m:
            code, out = run_smoke()
        self.assertEqual(code, 1)
        self.assertIn("SMOKE login: FAIL", out)
        self.assertIn("SMOKE_FAILED: login", out)
        api_mock.assert_called_once()

    def test_send_failed(self):
        api_m, pro_m, media_m = self._patched({"typing_ticket": "T"}, pro_ret=1)
        with api_m, pro_m, media_m:
            code, out = run_smoke()
        self.assertEqual(code, 1)
        self.assertIn("SMOKE send: FAIL", out)
        self.assertIn("SMOKE_FAILED: send", out)

    def test_media_failed(self):
        api_m, pro_m, media_m = self._patched({"typing_ticket": "T"}, media_ret=1)
        with api_m, pro_m, media_m:
            code, out = run_smoke()
        self.assertEqual(code, 1)
        self.assertIn("SMOKE media: FAIL", out)
        self.assertIn("SMOKE_FAILED: media", out)

    def test_no_send_skips_both_legs(self):
        api_m, pro_m, media_m = self._patched({"typing_ticket": "T"})
        with api_m, pro_m as pro_mock, media_m as media_mock:
            code, out = run_smoke(no_send=True)
        self.assertEqual(code, 0)
        self.assertIn("SMOKE_OK", out)
        self.assertIn("SMOKE send: SKIP", out)
        self.assertIn("SMOKE media: SKIP", out)
        pro_mock.assert_not_called()
        media_mock.assert_not_called()

    def test_no_owner_fails_send(self):
        os.unlink(os.path.join(DATA_DIR, "owner.json"))
        api_m, pro_m, media_m = self._patched({"typing_ticket": "T"})
        with api_m as api_mock, pro_m, media_m:
            code, out = run_smoke()
        self.assertEqual(code, 1)
        self.assertIn("SMOKE login: SKIP", out)
        self.assertIn("SMOKE send: FAIL", out)
        self.assertIn("SMOKE media: SKIP", out)
        self.assertIn("SMOKE_FAILED: send", out)
        api_mock.assert_not_called()

    def test_ledger_corrupt_is_chain_failure(self):
        with open(os.path.join(DATA_DIR, "send_ledger.json"), "w") as f:
            f.write("not json{{{")
        api_m, pro_m, media_m = self._patched({"typing_ticket": "T"})
        with api_m, pro_m, media_m:
            code, out = run_smoke()
        self.assertEqual(code, 1)
        self.assertIn("SMOKE ledger: FAIL", out)
        self.assertIn("SMOKE_FAILED: ledger", out)

    def test_no_token_fails_login_and_receive(self):
        os.unlink(os.path.join(DATA_DIR, "session.json"))
        api_m, pro_m, media_m = self._patched({"typing_ticket": "T"},
                                               pro_ret=1, media_ret=1)
        with api_m, pro_m, media_m:
            code, out = run_smoke()
        self.assertEqual(code, 1)
        self.assertIn("SMOKE login: FAIL", out)
        self.assertIn("SMOKE receive: FAIL", out)

    def test_smoke_png_constant_is_valid_png(self):
        raw = base64.b64decode(ilink._SMOKE_PNG_B64)
        self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()
