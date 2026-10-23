#!/usr/bin/env python3
"""Integration tests for the example Muse hook driver script.

Runs examples/muse-hook/ilink-watch.sh in a sandbox against a fake
ilink.py, pinning the two-phase poll contract end to end:

  * a batch is queued BEFORE commit is called, exactly once
  * an empty batch still commits (the cursor must advance even when
    there is nothing to wake the worker for)
  * a failing commit leaves the queue intact (never lost, never
    overwritten — the batch will simply be re-fetched next round)
  * stale-claim recycling never overwrites a non-empty pending queue
  * a hard crash (empty poll output) is a poll failure, not silence

The script is copied into the sandbox with ONE test-only substitution:
`-cmin +10` becomes `-mmin +10` so `touch -d` can age queue files
(ctime cannot be backdated). The logic under test is unchanged.

    python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SCRIPT_SRC = os.path.join(ROOT, "examples", "muse-hook", "ilink-watch.sh")

RUNTIME_STUB = """# Test stub for the Muse hook runtime library (the script sources it).
log() { echo "LOG $*" >> "$TRACE_CALLS"; }
silent() { echo "SILENT $*" >> "$TRACE_CALLS"; }
wake() { echo "WAKE $1 $2" >> "$TRACE_CALLS"; }
"""

FAKE_ILINK = """#!/usr/bin/env python3
import os, sys
trace = os.environ["TRACE_CALLS"]
if len(sys.argv) > 1 and sys.argv[1] == "poll":
    out = os.environ.get("FAKE_OUT", "")
    if out:
        print(out)
elif len(sys.argv) > 1 and sys.argv[1] == "commit":
    data = sys.stdin.read()
    if os.environ.get("FAKE_COMMIT_FAIL"):
        sys.exit(1)
    pend = os.path.join(os.environ.get("ILINK_HOOK_STATE_DIR")
                        or os.environ["ILINK_HOOK_STATE"], "ilink-pending.json")
    order = "ORDER_OK" if os.path.exists(pend) else "ORDER_NA"
    with open(trace, "a") as f:
        f.write("COMMIT " + data.strip() + " " + order + "\\n")
"""

BATCH = json.dumps({
    "msgs": [{"from_user_id": "owner1", "message_type": 1, "message_id": 7,
              "item_list": [{"type": 1, "text_item": {"text": "hi"}}]}],
    "commit": {"cursor": "B2", "ids": ["7"]},
})
EMPTY = json.dumps({"msgs": [], "commit": {"cursor": "B9", "ids": []}})


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash"),
                     "needs bash + jq")
class HookIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ilink-hook-it-")
        self.bridge = os.path.join(self.dir, "bridge")
        self.state = os.path.join(self.dir, "state")
        os.makedirs(self.bridge)
        os.makedirs(self.state)
        self.calls = os.path.join(self.dir, "calls.log")
        open(self.calls, "w").close()
        with open(os.path.join(self.bridge, "ilink.py"), "w") as f:
            f.write(FAKE_ILINK)
        with open(os.path.join(self.bridge, "owner.json"), "w") as f:
            json.dump({"owner_id": "owner1"}, f)
        self.runtime = os.path.join(self.dir, "runtime.sh")
        with open(self.runtime, "w") as f:
            f.write(RUNTIME_STUB)
        with open(SCRIPT_SRC) as f:
            script = f.read().replace("-cmin +10", "-mmin +10")
        self.script = os.path.join(self.dir, "watch.sh")
        with open(self.script, "w") as f:
            f.write(script)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def run_hook(self, fake_out=None, dry=False, extra_env=None,
                 legacy_env=False):
        env = dict(os.environ)
        env.update({
            "ILINK_DATA_DIR": self.bridge,
            # Pin BOTH names to this test's dir: the suite shares one
            # process env, and an unpinned name would leak another
            # module's state dir into the script (canonical wins).
            "ILINK_HOOK_STATE_DIR": self.state,
            "ILINK_HOOK_STATE": self.state,
            "HATCH_HOOK_RUNTIME": self.runtime,
            "HATCH_HOOK_DRY_RUN": "1" if dry else "0",
            "TRACE_CALLS": self.calls,
        })
        if legacy_env:
            # Only the legacy variable set: the script must still find
            # the state dir through the alias.
            env.pop("ILINK_HOOK_STATE_DIR", None)
            env["ILINK_HOOK_STATE"] = self.state
        if fake_out is not None:
            env["FAKE_OUT"] = fake_out
        if extra_env:
            env.update(extra_env)
        r = subprocess.run(["bash", self.script], env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        with open(self.calls) as f:
            return f.read()

    def pending(self):
        p = os.path.join(self.state, "ilink-pending.json")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return json.load(f)

    def test_batch_is_queued_before_commit(self):
        calls = self.run_hook(BATCH)
        self.assertIn("new_wechat_messages", calls)
        self.assertEqual(self.pending(), {"msgs": json.loads(BATCH)["msgs"]})
        self.assertIn('COMMIT {"cursor":"B2","ids":["7"]} ORDER_OK', calls)

    def test_legacy_state_env_alias_still_works(self):
        # Deployments made against the old example used ILINK_HOOK_STATE;
        # with only that variable set the batch must land in the same dir.
        calls = self.run_hook(BATCH, legacy_env=True)
        self.assertIn("new_wechat_messages", calls)
        self.assertEqual(self.pending(), {"msgs": json.loads(BATCH)["msgs"]})

    def test_empty_batch_still_commits(self):
        calls = self.run_hook(EMPTY)
        self.assertNotIn("new_wechat_messages", calls)
        self.assertIsNone(self.pending())
        self.assertIn('COMMIT {"cursor":"B9","ids":[]}', calls)

    def test_failed_commit_keeps_queue(self):
        calls = self.run_hook(BATCH, extra_env={"FAKE_COMMIT_FAIL": "1"})
        self.assertIn("new_wechat_messages", calls)
        self.assertEqual(self.pending(), {"msgs": json.loads(BATCH)["msgs"]})
        with open(os.path.join(self.bridge, "poll.log")) as f:
            self.assertIn("COMMIT_FAILED", f.read())

    def test_recycling_never_overwrites_pending(self):
        proc = os.path.join(self.state, "ilink-processing.json")
        with open(proc, "w") as f:
            json.dump({"msgs": [{"message_id": 1}]}, f)
        subprocess.run(["touch", "-d", "15 minutes ago", proc], check=True)
        pend_path = os.path.join(self.state, "ilink-pending.json")
        with open(pend_path, "w") as f:
            json.dump({"msgs": [{"message_id": 2}]}, f)
        self.run_hook(EMPTY)
        self.assertEqual(self.pending(), {"msgs": [{"message_id": 2}]})
        self.assertTrue(os.path.exists(proc))  # deferred, not moved

    def test_crash_output_is_a_poll_failure(self):
        self.run_hook("")
        with open(os.path.join(self.bridge, "poll.log")) as f:
            self.assertIn("POLL_CRASH", f.read())
        with open(os.path.join(self.bridge, "alert-state.json")) as f:
            self.assertIn("err_since", json.load(f))

    def test_pending_wake_reports_real_message_count(self):
        # Regression (2026-10-22 review): the pending branch counted with
        # `jq 'length'`, which counts the queue object's KEYS (always 1),
        # not the queued messages. The wake payload must carry the real
        # message count.
        pend_path = os.path.join(self.state, "ilink-pending.json")
        with open(pend_path, "w") as f:
            json.dump({"msgs": [{"from_user_id": "owner1", "message_id": n}
                                  for n in (11, 12, 13)]}, f)
        calls = self.run_hook()
        self.assertIn('WAKE pending_wechat_messages {"count":3', calls)


if __name__ == "__main__":
    unittest.main()
