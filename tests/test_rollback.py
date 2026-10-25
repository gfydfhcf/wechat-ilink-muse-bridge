#!/usr/bin/env python3
"""Tests for ilink.py's rollback command.

Rollback restores ilink.py (and, with --hook-dir, the hook scripts)
from the newest .bak-* backup next to each file. It is all-or-nothing:
every target must have a backup and every backup must verify (compile
check for ilink.py, bash -n for hook scripts) before anything is
swapped. Current files are first copied to .bak-pre-rollback-<epoch>
so a rollback is itself reversible; state files are never touched.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_rollback.py

Pinned behavior:
- a missing backup fails without touching the target;
- an unverifiable backup fails without touching the target;
- a second rollback returns to the state before the first, because
  the pre-rollback copy is itself a valid .bak-* source;
- the post-swap health check is informational only: a fake script
  with no health command still reports ROLLBACK_OK.
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-rollback-")
STATE_DIR = tempfile.mkdtemp(prefix="ilink-test-state-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = STATE_DIR
import ilink  # noqa: E402


def _pin_paths():
    ilink.BRIDGE = DATA_DIR
    os.environ["ILINK_HOOK_STATE_DIR"] = STATE_DIR


_pin_paths()


class RollbackTests(unittest.TestCase):
    def setUp(self):
        _pin_paths()
        self.script_dir = tempfile.mkdtemp(prefix="ilink-test-script-")
        self.script = os.path.join(self.script_dir, "ilink.py")
        with open(self.script, "w") as f:
            f.write("x = 1\n")

    def _run(self, **kwargs):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ilink.cmd_rollback(**kwargs)
        return rc, buf.getvalue()

    def _read(self, path):
        with open(path) as f:
            return f.read()

    def _backup(self, target, content, suffix):
        path = target + suffix
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_restores_newest_backup_and_keeps_pre_rollback_copy(self):
        self._backup(self.script, "x = 2\n", ".bak-v1.0.0")
        rc, out = self._run(script_path=self.script)
        self.assertEqual(rc, 0)
        self.assertIn("ROLLBACK_OK", out)
        self.assertEqual(self._read(self.script), "x = 2\n")
        pre = [n for n in os.listdir(self.script_dir)
               if n.startswith("ilink.py.bak-pre-rollback-")]
        self.assertEqual(len(pre), 1)
        self.assertEqual(self._read(os.path.join(self.script_dir, pre[0])),
                         "x = 1\n")

    def test_second_rollback_returns_to_pre_first_state(self):
        self._backup(self.script, "x = 2\n", ".bak-v1.0.0")
        self._run(script_path=self.script)
        rc, out = self._run(script_path=self.script)
        self.assertEqual(rc, 0)
        self.assertEqual(self._read(self.script), "x = 1\n")

    def test_no_backup_fails_without_touching(self):
        rc, out = self._run(script_path=self.script)
        self.assertEqual(rc, 1)
        self.assertIn("ROLLBACK_FAILED:no-backup", out)
        self.assertEqual(self._read(self.script), "x = 1\n")

    def test_unverifiable_backup_fails_without_touching(self):
        self._backup(self.script, "def broken(:\n", ".bak-v1.0.0")
        rc, out = self._run(script_path=self.script)
        self.assertEqual(rc, 1)
        self.assertIn("ROLLBACK_FAILED:verify", out)
        self.assertEqual(self._read(self.script), "x = 1\n")

    def test_hook_scripts_restored_with_hook_dir(self):
        self._backup(self.script, "x = 2\n", ".bak-v1.0.0")
        hook_dir = tempfile.mkdtemp(prefix="ilink-test-hooks-")
        for name, new, old in (
                ("ilink-watch.sh", "echo new\n", "echo old\n"),
                ("ilink-warm-wait.sh", "echo new2\n", "echo old2\n")):
            target = os.path.join(hook_dir, name)
            with open(target, "w") as f:
                f.write("#!/usr/bin/env bash\n" + new)
            self._backup(target, "#!/usr/bin/env bash\n" + old,
                         ".bak-v1.0.0")
        rc, out = self._run(hook_dir=hook_dir, script_path=self.script)
        self.assertEqual(rc, 0)
        self.assertIn("RESTORED:ilink-watch.sh", out)
        self.assertIn("RESTORED:ilink-warm-wait.sh", out)
        self.assertIn("echo old\n",
                      self._read(os.path.join(hook_dir, "ilink-watch.sh")))
        self.assertIn("echo old2\n",
                      self._read(os.path.join(hook_dir, "ilink-warm-wait.sh")))

    def test_hook_missing_backup_fails_all(self):
        self._backup(self.script, "x = 2\n", ".bak-v1.0.0")
        hook_dir = tempfile.mkdtemp(prefix="ilink-test-hooks-")
        target = os.path.join(hook_dir, "ilink-watch.sh")
        with open(target, "w") as f:
            f.write("#!/usr/bin/env bash\necho new\n")
        # ilink-warm-wait.sh has no backup at all: all-or-nothing.
        rc, out = self._run(hook_dir=hook_dir, script_path=self.script)
        self.assertEqual(rc, 1)
        self.assertIn("ROLLBACK_FAILED:no-backup", out)
        self.assertEqual(self._read(self.script), "x = 1\n")
        self.assertIn("echo new\n", self._read(target))


if __name__ == "__main__":
    unittest.main()
