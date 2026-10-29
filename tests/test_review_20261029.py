#!/usr/bin/env python3
"""Regression tests for the 2026-10-29 full code review fixes.

Covers the review report's items that had no test coverage, plus the
repairs themselves (M1-M5, L1-L15, S2/S3/S5 as applicable):

- M1: keyed proactive sends serialize (concurrent same-key sends = 1 API call)
- M2/L1: poll merges contacts (typing_ticket survives) and only writes
  contacts when the round actually changed them
- M3: inbound media downloads refuse above MEDIA_MAX_BYTES, both when
  the server declares a huge total and when it lies about it
- M4: unknown poll item types surface as a placeholder entry, not a drop
- M5: concurrent atomic JSON writes never tear the file
- L2: the quote cache loads lazily (first ref_msg only), not every round
- L3: stats skips corrupt audit lines instead of crashing
- L4: health reports the last poll.log line even with a huge log file
- L6: daemon handle failures back off exponentially, alert once at 5,
  and never auto-commit the poison batch
- L7: bind lock is stale only for a dead pid, and release verifies its token
- L8: typing reports NO_TYPING_TICKET when a forced refresh yields none
- L9: commit rejects non-list ids / non-string cursor / non-object payload
- L10: poll normalizes a numeric from_user_id to str before the owner check
- L11: CLI integer args reject garbage with usage + exit 2, no traceback
- L12: update-to verifies a published sha256 when present, and warns
  (without failing) when the AGENTS.md download fails
- L13: concurrent version-notify runs send each owner exactly once, and
  no subscribers is a quiet success (0)
- L14: login QR refreshes do not stretch the original deadline
- L15: cmd_send omits context_token when falsy, like _send_raw
- S2: the send audit log is chmod 600 after append
- S3: save_seen honors an explicit keep; a single oversized batch is
  kept whole by mark_seen (see test_dedupe for the roll-off half)
- _validate_send_text: the 2026-10-27 --key incident guard, finally tested

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_review_20261029.py
"""
import base64
import contextlib
import hashlib
import io
import itertools
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-review29-")
STATE_DIR = tempfile.mkdtemp(prefix="ilink-test-review29-state-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = STATE_DIR
import ilink  # noqa: E402
import daemon  # noqa: E402


def _pin_paths():
    ilink.BRIDGE = DATA_DIR
    ilink.SESSION_FILE = os.path.join(DATA_DIR, "session.json")
    ilink.CONTACTS_FILE = os.path.join(DATA_DIR, "contacts.json")
    ilink.SEEN_FILE = os.path.join(DATA_DIR, "seen_ids.json")
    ilink.SEND_LEDGER_FILE = os.path.join(DATA_DIR, "send_ledger.json")
    ilink.SEND_AUDIT_FILE = os.path.join(DATA_DIR, "send_audit.jsonl")
    ilink.QUOTE_CACHE_FILE = os.path.join(DATA_DIR, "quote_cache.json")
    ilink.MEDIA_DIR = os.path.join(DATA_DIR, "media")
    ilink.VERSION_NOTIFY_FILE = os.path.join(DATA_DIR, "version_notify.json")
    ilink.VERSION_NOTIFY_LOCK_FILE = os.path.join(DATA_DIR, "version_notify.lock")
    ilink.UPDATE_OFFER_FILE = os.path.join(DATA_DIR, "update_offer.json")
    os.environ["ILINK_HOOK_STATE_DIR"] = STATE_DIR


_pin_paths()


class Review29Test(unittest.TestCase):
    def setUp(self):
        _pin_paths()
        for base in (DATA_DIR, STATE_DIR):
            for name in os.listdir(base):
                path = os.path.join(base, name)
                if os.path.isfile(path):
                    os.unlink(path)
                else:
                    shutil.rmtree(path, ignore_errors=True)

    def _run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ilink.main(argv)
        return code, out.getvalue(), err.getvalue()


class ValidateSendTextTest(Review29Test):
    def test_empty_and_whitespace_refused(self):
        self.assertEqual(ilink._validate_send_text(""), "SEND_TEXT_EMPTY")
        self.assertEqual(ilink._validate_send_text("   "), "SEND_TEXT_EMPTY")
        self.assertEqual(ilink._validate_send_text(None), "SEND_TEXT_EMPTY")

    def test_flag_like_refused(self):
        # The 2026-10-27 incident: "--key" reached the text position
        # when the worker dropped the "-" placeholder.
        self.assertEqual(ilink._validate_send_text("--key"),
                         "SEND_TEXT_LOOKS_LIKE_FLAG")
        self.assertEqual(ilink._validate_send_text("  --flag x"),
                         "SEND_TEXT_LOOKS_LIKE_FLAG")

    def test_normal_text_passes(self):
        self.assertIsNone(ilink._validate_send_text("hello"))
        self.assertIsNone(ilink._validate_send_text("a -- b"))


class PollReviewTest(Review29Test):
    def _poll(self, payload, contacts, owners, quote_calls=None):
        saved = {}

        def fake_save(c):
            saved.clear()
            saved.update(json.loads(json.dumps(c)))

        def fake_quote_cache():
            if quote_calls is not None:
                quote_calls.append(1)
            return {"by_id": {}, "by_svr": {}}

        patches = [
            mock.patch.object(ilink, "load_session",
                              return_value={"bot_token": "tok", "cursor": ""}),
            mock.patch.object(ilink, "api", return_value=payload),
            mock.patch.object(ilink, "load_owner_ids", return_value=set(owners)),
            mock.patch.object(ilink, "load_contacts", return_value=contacts),
            mock.patch.object(ilink, "save_contacts", side_effect=fake_save),
            mock.patch.object(ilink, "download_media_item", return_value=None),
            mock.patch.object(ilink, "_quote_cache_upsert", return_value=None),
            mock.patch.object(ilink, "_load_quote_cache",
                              side_effect=fake_quote_cache),
        ]
        for p in patches:
            p.start()
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = ilink.cmd_poll()
            self.assertEqual(code, 0, buf.getvalue())
            return json.loads(buf.getvalue()), saved
        finally:
            for p in reversed(patches):
                p.stop()

    def _msg(self, from_id, items, ctx="ctx-1"):
        m = {"message_id": 1001, "message_type": 1, "from_user_id": from_id,
             "item_list": items}
        if ctx is not None:
            m["context_token"] = ctx
        return m

    def test_unknown_item_type_surfaces_not_dropped(self):
        payload = {"msgs": [self._msg("u1", [{"type": 99,
                                              "mystery_item": {}}])],
                   "get_updates_buf": "BUF"}
        out, _ = self._poll(payload, {}, ["u1"])
        self.assertEqual(len(out["msgs"]), 1)
        self.assertEqual(out["msgs"][0]["text"], "[未知消息类型 99]")
        # The id still flows into the commit block (cursor advances).
        self.assertEqual(out["commit"]["ids"], ["1001"])

    def test_contacts_merge_keeps_typing_ticket(self):
        contacts = {"u1": {"context_token": "old-ctx",
                           "typing_ticket": "TKT",
                           "typing_ticket_at": 123}}
        payload = {"msgs": [self._msg("u1", [{"type": 1, "text_item": {
            "text": "hi"}}], ctx="new-ctx")], "get_updates_buf": "BUF"}
        _, saved = self._poll(payload, contacts, ["u1"])
        self.assertEqual(saved["u1"]["context_token"], "new-ctx")
        self.assertEqual(saved["u1"]["typing_ticket"], "TKT")

    def test_contacts_not_written_when_unchanged(self):
        # No context_token on the message: nothing to merge, no write.
        contacts = {"u1": {"context_token": "old-ctx"}}
        payload = {"msgs": [self._msg("u1", [{"type": 1, "text_item": {
            "text": "hi"}}], ctx=None)], "get_updates_buf": "BUF"}
        _, saved = self._poll(payload, contacts, ["u1"])
        self.assertEqual(saved, {})

    def test_numeric_from_id_normalized(self):
        payload = {"msgs": [self._msg(12345, [{"type": 1, "text_item": {
            "text": "hi"}}])], "get_updates_buf": "BUF"}
        out, _ = self._poll(payload, {}, ["12345"])
        self.assertEqual(len(out["msgs"]), 1)
        self.assertEqual(out["msgs"][0]["from_user_id"], "12345")

    def test_quote_cache_lazy(self):
        calls = []
        payload = {"msgs": [self._msg("u1", [{"type": 1, "text_item": {
            "text": "hi"}}])], "get_updates_buf": "BUF"}
        self._poll(payload, {}, ["u1"], quote_calls=calls)
        self.assertEqual(calls, [])
        # With a ref_msg present, the cache loads exactly once.
        ref_msg = {"message_item": {"msg_id": "999"}, "title": "orig"}
        payload = {"msgs": [self._msg("u1", [{"type": 1, "text_item": {
            "text": "hi"}, "ref_msg": ref_msg}])], "get_updates_buf": "BUF"}
        self._poll(payload, {}, ["u1"], quote_calls=calls)
        self.assertEqual(len(calls), 1)


class BindLockTest(Review29Test):
    def test_acquire_release_roundtrip(self):
        lock = ilink._acquire_bind_lock()
        self.assertIsNotNone(lock)
        fd, token = lock
        self.assertIsInstance(fd, int)
        self.assertTrue(token)
        with open(ilink._bind_lock_path()) as f:
            content = json.load(f)
        self.assertEqual(content["pid"], os.getpid())
        self.assertEqual(content["token"], token)
        ilink._release_bind_lock(lock)
        self.assertFalse(os.path.exists(ilink._bind_lock_path()))

    def test_stale_lock_with_dead_pid_is_taken_over(self):
        with open(ilink._bind_lock_path(), "w") as f:
            json.dump({"pid": 999999, "token": "old",
                       "started_at": 1}, f)
        old = time.time() - 3600
        os.utime(ilink._bind_lock_path(), (old, old))
        lock = ilink._acquire_bind_lock()
        self.assertIsNotNone(lock)
        ilink._release_bind_lock(lock)

    def test_old_lock_with_live_pid_is_not_stolen(self):
        with open(ilink._bind_lock_path(), "w") as f:
            json.dump({"pid": os.getpid(), "token": "mine",
                       "started_at": 1}, f)
        old = time.time() - 3600
        os.utime(ilink._bind_lock_path(), (old, old))
        self.assertIsNone(ilink._acquire_bind_lock())
        self.assertTrue(os.path.exists(ilink._bind_lock_path()))
        os.unlink(ilink._bind_lock_path())

    def test_release_with_wrong_token_keeps_file(self):
        lock = ilink._acquire_bind_lock()
        self.assertIsNotNone(lock)
        fd, _token = lock
        # Simulate a steal: the file now belongs to someone else.
        with open(ilink._bind_lock_path(), "w") as f:
            json.dump({"pid": 999999, "token": "someone-else",
                       "started_at": 1}, f)
        ilink._release_bind_lock(lock)
        self.assertTrue(os.path.exists(ilink._bind_lock_path()))
        os.unlink(ilink._bind_lock_path())


class KeyedProactiveConcurrencyTest(Review29Test):
    def test_same_key_concurrent_sends_once(self):
        sends = []

        def fake_send_raw(to_user_id, ctx, text, token, client_id=None):
            time.sleep(0.3)
            sends.append(client_id)
            return {"code": 0, "message_id": "m1"}

        patches = [
            mock.patch.object(ilink, "load_session",
                              return_value={"bot_token": "tok"}),
            mock.patch.object(ilink, "load_contacts",
                              return_value={"u1": {"context_token": "ctx"}}),
            mock.patch.object(ilink, "_send_raw", side_effect=fake_send_raw),
            mock.patch.object(ilink, "_quote_cache_upsert", return_value=None),
        ]
        for p in patches:
            p.start()
        try:
            results = []

            def run():
                with contextlib.redirect_stdout(io.StringIO()):
                    results.append(ilink.cmd_proactive("u1", "hello",
                                                       key="k-conc"))
            threads = [threading.Thread(target=run) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertEqual(sorted(results), [0, 0])
        self.assertEqual(len(sends), 1)


class VersionNotifyConcurrencyTest(Review29Test):
    def test_concurrent_runs_send_each_owner_once(self):
        with open(ilink.VERSION_NOTIFY_FILE, "w") as f:
            json.dump({"subscribed": ["owner-1"], "notified": {}}, f)
        with open(ilink.CONTACTS_FILE, "w") as f:
            json.dump({"owner-1": {"context_token": "ctx"}}, f)
        sends = []

        def fake_proactive(owner, text):
            time.sleep(0.2)  # force overlap if the lock were missing
            sends.append(owner)
            return 0

        with mock.patch.object(ilink, "cmd_proactive",
                               side_effect=fake_proactive):
            results = []

            def run():
                with contextlib.redirect_stdout(io.StringIO()):
                    results.append(ilink._version_notify_locked(
                        "v9.9.9", {"body": "notes",
                                   "html_url": "https://example.invalid/r"}))
            threads = [threading.Thread(target=run) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(sorted(results), [0, 0])
        self.assertEqual(sends, ["owner-1"])
        with open(ilink.VERSION_NOTIFY_FILE) as f:
            state = json.load(f)
        self.assertEqual(state["notified"], {"owner-1": "v9.9.9"})

    def test_no_subscribers_is_quiet_success(self):
        with open(ilink.VERSION_NOTIFY_FILE, "w") as f:
            json.dump({"subscribed": [], "notified": {}}, f)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ilink._do_version_notify("v9.9.9", {"body": ""})
        self.assertEqual(code, 0)
        self.assertIn("NO_SUBSCRIBERS", buf.getvalue())


class CliIntArgsTest(Review29Test):
    def test_illegal_integers_exit_2_with_usage(self):
        cases = [
            ["ilink.py", "login", "qr", "abc"],
            ["ilink.py", "bind", "abc"],
            ["ilink.py", "watchdog", "--max-age-mins", "abc"],
            ["ilink.py", "prune-media", "--max-age-days", "abc"],
            ["ilink.py", "prune-media", "--max-total-mb", "abc"],
            ["ilink.py", "send-voice", "u1", "ctx", "/tmp/x", "abc"],
            ["ilink.py", "proactive-voice", "u1", "/tmp/x", "abc"],
        ]
        for argv in cases:
            code, _out, err = self._run_main(argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("usage:", err, argv)


class UpdateToReviewTest(Review29Test):
    NEW_ILINK = 'VERSION = "1.3.0"\nprint("new")\n'

    def setUp(self):
        super().setUp()
        self._old_version = ilink.VERSION
        ilink.VERSION = "1.2.0"
        self.addCleanup(setattr, ilink, "VERSION", self._old_version)
        self.script = os.path.join(DATA_DIR, "ilink.py")
        with open(self.script, "w") as f:
            f.write('VERSION = "1.2.0"\nprint("old")\n')

    def _run_update(self, notes_body, compare_files, agents_content):
        def fake_json(url):
            if "compare" in url:
                return {"files": [{"filename": fn} for fn in compare_files]}
            if "/releases/tags/" in url:
                return {"body": notes_body}
            raise AssertionError(f"unexpected json url: {url}")

        def fake_text(url):
            if url.endswith("/ilink.py"):
                return self.NEW_ILINK
            if url.endswith("/AGENTS.md"):
                return agents_content
            raise AssertionError(f"unexpected text url: {url}")

        with mock.patch.object(ilink, "_github_get_json",
                               side_effect=fake_json), \
                mock.patch.object(ilink, "_github_get_text",
                                  side_effect=fake_text):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = ilink.cmd_update_to("v1.3.0", script_path=self.script)
        return code, buf.getvalue()

    def test_hash_mismatch_refuses_and_replaces_nothing(self):
        code, out = self._run_update(
            "ilink.py sha256: " + "0" * 64, ["ilink.py"], None)
        self.assertEqual(code, 1)
        self.assertIn("UPDATE_FAILED:verify:hash_mismatch", out)
        with open(self.script) as f:
            self.assertIn('VERSION = "1.2.0"', f.read())

    def test_hash_match_installs(self):
        digest = hashlib.sha256(self.NEW_ILINK.encode()).hexdigest()
        code, out = self._run_update(
            f"ilink.py sha256: {digest}", ["ilink.py"], None)
        self.assertEqual(code, 0, out)
        with open(self.script) as f:
            self.assertIn('VERSION = "1.3.0"', f.read())

    def test_agents_download_failure_warns_but_updates(self):
        code, out = self._run_update(
            "", ["ilink.py", "AGENTS.md"], None)
        self.assertEqual(code, 0, out)
        self.assertIn("CONTRACT:AGENTS.md:download-failed", out)
        self.assertFalse(os.path.exists(os.path.join(DATA_DIR, "AGENTS.md.new")))
        with open(self.script) as f:
            self.assertIn('VERSION = "1.3.0"', f.read())


class MediaCapTest(Review29Test):
    def setUp(self):
        super().setUp()
        self._old_cap = ilink.MEDIA_MAX_BYTES
        ilink.MEDIA_MAX_BYTES = 100
        self.addCleanup(setattr, ilink, "MEDIA_MAX_BYTES", self._old_cap)
        key = base64.b64encode(b"\x00" * 16).decode("ascii")
        self.item = {"image_item": {"media": {
            "aes_key": key, "full_url": "https://example.invalid/x"}}}

    def test_declared_total_above_cap_refused(self):
        with mock.patch.object(ilink, "_http_get_range",
                               return_value=(200, b"x" * 10, 101, True)):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                result = ilink.download_media_item(self.item, 2)
        self.assertIsNone(result)
        self.assertIn("too large", err.getvalue())

    def test_lying_server_accumulation_capped(self):
        # total omitted, but 206 chunks keep appending past the cap.
        calls = {"n": 0}

        def fake_range(url, start=0, timeout=60):
            calls["n"] += 1
            return (206, b"x" * 60, None, calls["n"] >= 2)

        with mock.patch.object(ilink, "_http_get_range",
                               side_effect=fake_range), \
                mock.patch.object(ilink.time, "sleep", return_value=None):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                result = ilink.download_media_item(self.item, 2)
        self.assertIsNone(result)
        self.assertIn("too large", err.getvalue())

    def test_under_cap_download_still_works(self):
        plaintext = b"hello world!!!!!"  # exactly one AES block
        ciphertext = ilink._aes_ecb_encrypt(plaintext, b"\x00" * 16)
        with mock.patch.object(ilink, "_http_get_range",
                               return_value=(200, ciphertext,
                                             len(ciphertext), True)):
            path = ilink.download_media_item(self.item, 2)
        self.assertIsNotNone(path)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), plaintext)


class StatsCorruptTest(Review29Test):
    def test_corrupt_audit_lines_skipped(self):
        now = int(time.time())
        lines = [
            json.dumps({"ts": now, "result": "ok"}),
            json.dumps({"ts": now, "result": "app_error"}),
            json.dumps({"ts": "not-a-number", "result": "ok"}),
            json.dumps([1, 2, 3]),
            "not json at all",
        ]
        with open(ilink.SEND_AUDIT_FILE, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.makedirs(ilink.MEDIA_DIR, exist_ok=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ilink.cmd_stats()
        self.assertEqual(code, 0)
        self.assertIn("sends today: 1 ok / 1 failed", buf.getvalue())


class HealthTailTest(Review29Test):
    def test_huge_poll_log_reports_last_line(self):
        with open(ilink.SESSION_FILE, "w") as f:
            json.dump({"bot_token": "tok",
                       "login_at": int(time.time()) - 60}, f)
        with open(os.path.join(DATA_DIR, "poll.log"), "w") as f:
            for i in range(5000):
                f.write(f"filler line {i} padding padding padding\n")
            f.write("POLL_LAST_LINE_MARKER\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ilink.cmd_health(offline=True)
        self.assertEqual(code, 0)
        self.assertIn("POLL_LAST_LINE_MARKER", buf.getvalue())


class DaemonBackoffTest(Review29Test):
    def test_handle_failures_back_off_alert_once_never_commit(self):
        delays = []

        def fake_sleep(d):
            delays.append(d)
            if len(delays) >= 6:
                raise KeyboardInterrupt

        commits = []
        with mock.patch.object(daemon, "poll_once",
                               return_value=("ok", [{"message_id": 1}],
                                             {"cursor": "C", "ids": ["1"]})), \
                mock.patch.object(daemon, "collect_burst",
                                  side_effect=lambda m, c: (m, c)), \
                mock.patch.object(daemon, "handle_burst",
                                  side_effect=RuntimeError("boom")), \
                mock.patch.object(daemon, "commit_batch",
                                  side_effect=lambda c: commits.append(c)), \
                mock.patch.object(daemon.time, "sleep",
                                  side_effect=fake_sleep), \
                mock.patch.object(sys, "argv", ["daemon.py"]):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                with self.assertRaises(KeyboardInterrupt):
                    daemon.main()
        self.assertEqual(delays, [2, 4, 8, 16, 32, 60])
        self.assertEqual(out.getvalue().count("HANDLE_FAILING"), 1)
        self.assertEqual(commits, [])


class TypingNoTicketTest(Review29Test):
    def test_forced_refresh_without_ticket_reports_no_ticket(self):
        tickets = []

        def fake_ticket(to_user_id, token, force=False):
            tickets.append(force)
            return None if force else "t1"

        api_calls = []
        with mock.patch.object(ilink, "load_session",
                               return_value={"bot_token": "tok"}), \
                mock.patch.object(ilink, "_typing_ticket",
                                  side_effect=fake_ticket), \
                mock.patch.object(ilink, "api",
                                  side_effect=lambda *a, **k: api_calls.append(a) or {"ret": 1}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = ilink.cmd_typing("u1", "start")
        self.assertEqual(code, 1)
        self.assertIn("NO_TYPING_TICKET", buf.getvalue())
        # Only the first sendtyping attempt went out; no doomed retry.
        self.assertEqual(len(api_calls), 1)


class CommitValidationTest(Review29Test):
    def _run_commit(self, payload):
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = ilink.cmd_commit()
        finally:
            sys.stdin = old_stdin
        return code, buf.getvalue()

    def test_string_ids_rejected_without_seen_write(self):
        code, out = self._run_commit({"cursor": "C", "ids": "4242"})
        self.assertEqual(code, 1)
        self.assertIn("COMMIT_ERROR:ids must be a list", out)
        self.assertFalse(os.path.exists(ilink.SEEN_FILE))

    def test_non_string_cursor_rejected(self):
        code, out = self._run_commit({"cursor": 5, "ids": ["1"]})
        self.assertEqual(code, 1)
        self.assertIn("COMMIT_ERROR:cursor must be a string", out)

    def test_non_object_payload_rejected(self):
        code, out = self._run_commit(["1"])
        self.assertEqual(code, 1)
        self.assertIn("COMMIT_ERROR:payload must be an object", out)

    def test_valid_commit_still_works(self):
        code, out = self._run_commit({"cursor": "C2", "ids": ["7"]})
        self.assertEqual(code, 0)
        self.assertIn("COMMIT_OK", out)
        self.assertEqual(ilink.load_seen(), ["7"])


class SendContextTokenTest(Review29Test):
    def _send(self, ctx):
        bodies = []
        with mock.patch.object(ilink, "load_session",
                               return_value={"bot_token": "tok"}), \
                mock.patch.object(ilink, "api",
                                  side_effect=lambda *a, **k: bodies.append(a[2]) or {"code": 0, "message_id": "m1"}), \
                mock.patch.object(ilink, "_quote_cache_upsert",
                                  return_value=None):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = ilink.cmd_send("u1", ctx, "hi")
        self.assertEqual(code, 0)
        return bodies[0]["msg"]

    def test_falsy_context_token_omitted(self):
        self.assertNotIn("context_token", self._send(""))
        self.assertNotIn("context_token", self._send(None))

    def test_truthy_context_token_sent(self):
        self.assertEqual(self._send("ctx")["context_token"], "ctx")


class AtomicWriteConcurrencyTest(Review29Test):
    def test_concurrent_writes_never_tear(self):
        path = os.path.join(DATA_DIR, "shared.json")
        errors = []

        def write(v):
            try:
                for _ in range(30):
                    ilink._save_json_atomic(path, {"v": v})
            except Exception as e:  # thread exceptions don't fail a
                errors.append(e)    # test by themselves: collect them

        threads = [threading.Thread(target=write, args=(v,))
                   for v in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        with open(path) as f:
            data = json.load(f)
        self.assertIn(data["v"], (0, 1))
        leftovers = [n for n in os.listdir(DATA_DIR) if ".tmp-" in n]
        self.assertEqual(leftovers, [])


class AuditModeTest(Review29Test):
    def test_audit_file_is_600_after_append(self):
        with open(ilink.SEND_AUDIT_FILE, "w") as f:
            f.write("")
        os.chmod(ilink.SEND_AUDIT_FILE, 0o644)
        ilink._audit_send("send", "u1", "c1", "ok")
        mode = os.stat(ilink.SEND_AUDIT_FILE).st_mode & 0o777
        self.assertEqual(mode, 0o600)


class SaveSeenKeepTest(Review29Test):
    def test_save_seen_honors_keep(self):
        ilink.save_seen([str(i) for i in range(600)], keep=500)
        seen = ilink.load_seen()
        self.assertEqual(len(seen), 500)
        self.assertIn("599", seen)
        self.assertNotIn("0", seen)


class LoginDeadlineTest(Review29Test):
    def test_qr_refreshes_do_not_stretch_deadline(self):
        clock = itertools.count(0, 100)
        api_calls = []
        with mock.patch.object(ilink, "api",
                               side_effect=lambda *a, **k: api_calls.append(a) or {"status": "expired"}), \
                mock.patch.object(ilink, "_fetch_qrcode",
                                  return_value={"qrcode": "QR-NEW"}), \
                mock.patch.object(ilink, "_print_qrcode",
                                  return_value="QR-NEW"), \
                mock.patch.object(ilink.time, "time",
                                  side_effect=lambda: next(clock)), \
                mock.patch.object(ilink.time, "sleep",
                                  return_value=None):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = ilink.cmd_login("QR-OLD", 300)
        self.assertEqual(code, 1)
        self.assertIn("LOGIN_TIMEOUT", buf.getvalue())
        # Two polls, then the original 300s deadline fires — refreshes
        # never pushed it out (the old code would have kept refreshing).
        self.assertEqual(len(api_calls), 2)


if __name__ == "__main__":
    unittest.main()
