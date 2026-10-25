#!/usr/bin/env python3
"""Tests for self-update (ilink.py update-to).

update-to installs a RELEASE tag: ilink.py always; hook scripts when
--hook-dir is given and the release changed them; AGENTS.md is never
applied — a changed contract is downloaded to AGENTS.md.new for manual
sync. Two phases: download + verify everything first, then swap
everything (backups kept as .bak-<tag>). Any verification failure
replaces nothing.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_update_to.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-update-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = tempfile.mkdtemp(prefix="ilink-test-state-")
import ilink  # noqa: E402

OLD_ILINK = 'VERSION = "1.2.0"\nprint("old")\n'
NEW_ILINK = 'VERSION = "1.3.0"\nprint("new")\n'
OLD_SH = "#!/usr/bin/env bash\necho old\n"
NEW_SH = "#!/usr/bin/env bash\necho new\n"

RAW = {"ilink.py": NEW_ILINK, "watch": NEW_SH, "warm": NEW_SH,
       "agents": "# new contract\n"}
COMPARE = {"files": [{"filename": "ilink.py"}]}


def fake_get_json(url):
    if "compare" in url:
        return COMPARE
    raise AssertionError(f"unexpected json url: {url}")


def fake_get_text(url):
    if url.endswith("/ilink.py"):
        return RAW["ilink.py"]
    if url.endswith("examples/muse-hook/ilink-watch.sh"):
        return RAW["watch"]
    if url.endswith("examples/muse-hook/ilink-warm-wait.sh"):
        return RAW["warm"]
    if url.endswith("/AGENTS.md"):
        return RAW["agents"]
    raise AssertionError(f"unexpected text url: {url}")


def run_update(tag, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ilink.cmd_update_to(tag, **kwargs)
    return code, buf.getvalue()


class UpdateToTest(unittest.TestCase):
    def setUp(self):
        ilink.BRIDGE = DATA_DIR
        ilink.VERSION = "1.2.0"
        ilink.UPDATE_OFFER_FILE = os.path.join(DATA_DIR, "update_offer.json")
        for name in os.listdir(DATA_DIR):
            path = os.path.join(DATA_DIR, name)
            if os.path.isfile(path):
                os.remove(path)
        self.script = os.path.join(DATA_DIR, "ilink.py")
        with open(self.script, "w") as f:
            f.write(OLD_ILINK)
        self.hook_dir = os.path.join(DATA_DIR, "hooks")
        os.makedirs(self.hook_dir, exist_ok=True)
        with open(os.path.join(self.hook_dir, "ilink-watch.sh"), "w") as f:
            f.write(OLD_SH)
        with open(os.path.join(self.hook_dir, "ilink-warm-wait.sh"), "w") as f:
            f.write(OLD_SH)
        self._real_json = ilink._github_get_json
        self._real_text = ilink._github_get_text
        ilink._github_get_json = fake_get_json
        ilink._github_get_text = fake_get_text
        RAW["ilink.py"] = NEW_ILINK
        RAW["watch"] = NEW_SH
        RAW["warm"] = NEW_SH
        RAW["agents"] = "# new contract\n"
        COMPARE["files"] = [{"filename": "ilink.py"}]

    def tearDown(self):
        ilink._github_get_json = self._real_json
        ilink._github_get_text = self._real_text

    def read(self, path):
        with open(path) as f:
            return f.read()

    def test_ilink_only_update(self):
        with open(ilink.UPDATE_OFFER_FILE, "w") as f:
            json.dump({"tag": "v1.3.0", "owners": ["o1"],
                       "offered_at": 1}, f)
        code, out = run_update("v1.3.0", script_path=self.script)
        self.assertEqual(code, 0)
        self.assertIn("UPDATE_OK:v1.3.0", out)
        self.assertIn("UPDATED:ilink.py", out)
        self.assertEqual(self.read(self.script), NEW_ILINK)
        self.assertEqual(self.read(self.script + ".bak-v1.3.0"), OLD_ILINK)
        self.assertFalse(os.path.exists(ilink.UPDATE_OFFER_FILE))

    def test_up_to_date(self):
        code, out = run_update("v1.2.0", script_path=self.script)
        self.assertEqual(code, 0)
        self.assertIn("UP_TO_DATE", out)
        self.assertEqual(self.read(self.script), OLD_ILINK)

    def test_older_refused(self):
        code, out = run_update("v1.1.0", script_path=self.script)
        self.assertEqual(code, 1)
        self.assertIn("UPDATE_FAILED:not_newer", out)
        self.assertEqual(self.read(self.script), OLD_ILINK)

    def test_bad_tag(self):
        code, out = run_update("a/b", script_path=self.script)
        self.assertEqual(code, 1)
        self.assertIn("UPDATE_FAILED:bad_tag", out)

    def test_verify_failure_replaces_nothing(self):
        RAW["ilink.py"] = "def broken(:\n"
        code, out = run_update("v1.3.0", script_path=self.script)
        self.assertEqual(code, 1)
        self.assertIn("UPDATE_FAILED:verify", out)
        self.assertEqual(self.read(self.script), OLD_ILINK)
        self.assertFalse(os.path.exists(self.script + ".bak-v1.3.0"))

    def test_version_mismatch_refused(self):
        RAW["ilink.py"] = 'VERSION = "9.9.9"\nprint("x")\n'
        code, out = run_update("v1.3.0", script_path=self.script)
        self.assertEqual(code, 1)
        self.assertIn("UPDATE_FAILED:verify:version_mismatch", out)
        self.assertEqual(self.read(self.script), OLD_ILINK)

    def test_download_failure(self):
        RAW["ilink.py"] = None
        real_text = ilink._github_get_text

        def none_text(url):
            if url.endswith("/ilink.py"):
                return None
            return real_text(url)
        ilink._github_get_text = none_text
        code, out = run_update("v1.3.0", script_path=self.script)
        self.assertEqual(code, 1)
        self.assertIn("UPDATE_FAILED:download", out)
        self.assertEqual(self.read(self.script), OLD_ILINK)

    def test_hook_scripts_updated(self):
        COMPARE["files"] = [{"filename": "ilink.py"},
                            {"filename": "examples/muse-hook/ilink-watch.sh"},
                            {"filename": "examples/muse-hook/ilink-warm-wait.sh"}]
        code, out = run_update("v1.3.0", script_path=self.script,
                               hook_dir=self.hook_dir)
        self.assertEqual(code, 0)
        self.assertIn("UPDATED:ilink-watch.sh", out)
        self.assertIn("UPDATED:ilink-warm-wait.sh", out)
        self.assertEqual(
            self.read(os.path.join(self.hook_dir, "ilink-watch.sh")), NEW_SH)
        self.assertEqual(
            self.read(os.path.join(self.hook_dir, "ilink-watch.sh.bak-v1.3.0")),
            OLD_SH)

    def test_hook_script_verify_failure_replaces_nothing(self):
        COMPARE["files"] = [{"filename": "ilink.py"},
                            {"filename": "examples/muse-hook/ilink-watch.sh"}]
        RAW["watch"] = "if [\n"
        code, out = run_update("v1.3.0", script_path=self.script,
                               hook_dir=self.hook_dir)
        self.assertEqual(code, 1)
        self.assertIn("UPDATE_FAILED:verify", out)
        # Two-phase: the good ilink.py is NOT swapped either.
        self.assertEqual(self.read(self.script), OLD_ILINK)
        self.assertEqual(
            self.read(os.path.join(self.hook_dir, "ilink-watch.sh")), OLD_SH)

    def test_contract_downloaded_not_applied(self):
        COMPARE["files"] = [{"filename": "ilink.py"},
                            {"filename": "AGENTS.md"}]
        code, out = run_update("v1.3.0", script_path=self.script)
        self.assertEqual(code, 0)
        self.assertIn("CONTRACT:AGENTS.md.new", out)
        self.assertEqual(
            self.read(os.path.join(DATA_DIR, "AGENTS.md.new")),
            "# new contract\n")

    def test_compare_unavailable(self):
        ilink._github_get_json = lambda url: None
        code, out = run_update("v1.3.0", script_path=self.script,
                               hook_dir=self.hook_dir)
        self.assertEqual(code, 0)
        self.assertIn("UPDATED:ilink.py", out)
        self.assertIn("COMPARE:unavailable", out)
        self.assertIn("SKIPPED:hook-scripts:compare-unavailable", out)
        self.assertEqual(
            self.read(os.path.join(self.hook_dir, "ilink-watch.sh")), OLD_SH)

    def test_no_hook_dir_skips_changed_scripts(self):
        COMPARE["files"] = [{"filename": "ilink.py"},
                            {"filename": "examples/muse-hook/ilink-watch.sh"}]
        code, out = run_update("v1.3.0", script_path=self.script)
        self.assertEqual(code, 0)
        self.assertIn("SKIPPED:hook-scripts:no-hook-dir", out)
        self.assertEqual(self.read(self.script), NEW_ILINK)

    def test_swap_failure_rolls_back(self):
        real_replace = os.replace

        def flaky_replace(src, dst):
            if ".tmp-update-" in src:
                raise OSError("injected swap failure")
            return real_replace(src, dst)
        os.replace = flaky_replace
        try:
            code, out = run_update("v1.3.0", script_path=self.script)
        finally:
            os.replace = real_replace
        self.assertEqual(code, 1)
        self.assertIn("UPDATE_FAILED:swap", out)
        self.assertIn("ROLLED_BACK", out)
        self.assertEqual(self.read(self.script), OLD_ILINK)


if __name__ == "__main__":
    unittest.main()
