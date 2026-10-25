#!/usr/bin/env python3
"""Tests for media retention pruning (BACKLOG item 3).

prune-media deletes downloaded media older than the age limit, then
oldest-first down to the size cap. Files still referenced by the quote
cache are exempt from both passes. A dry run counts without deleting.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_prune_media.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-prune-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = tempfile.mkdtemp(prefix="ilink-test-state-")
import ilink  # noqa: E402

DAY = 86400


def write_media(name, size, age_days):
    media_dir = ilink.MEDIA_DIR
    os.makedirs(media_dir, exist_ok=True)
    path = os.path.join(media_dir, name)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    old = int(time.time()) - age_days * DAY
    os.utime(path, (old, old))
    return path


def run_prune(*args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ilink.cmd_prune_media(*args, **kwargs)
    assert code == 0, buf.getvalue()
    return buf.getvalue()


class PruneMediaTest(unittest.TestCase):
    def setUp(self):
        ilink.BRIDGE = DATA_DIR
        ilink.MEDIA_DIR = os.path.join(DATA_DIR, "media")
        ilink.QUOTE_CACHE_FILE = os.path.join(DATA_DIR, "quote_cache.json")
        for name in os.listdir(DATA_DIR):
            path = os.path.join(DATA_DIR, name)
            if os.path.isfile(path):
                os.remove(path)
        if os.path.isdir(ilink.MEDIA_DIR):
            for name in os.listdir(ilink.MEDIA_DIR):
                os.remove(os.path.join(ilink.MEDIA_DIR, name))

    def test_age_prunes_old_but_keeps_new_and_referenced(self):
        write_media("old.jpg", 10, 40)
        write_media("new.jpg", 10, 1)
        quoted = write_media("quoted.jpg", 10, 40)
        with open(ilink.QUOTE_CACHE_FILE, "w") as f:
            json.dump({"by_id": {"7": {"text": "[图片]", "kind": "image",
                                       "from": "user", "at": int(time.time()),
                                       "media_path": quoted}},
                       "by_svr": {}}, f)
        out = run_prune()
        self.assertIn("PRUNE_OK", out)
        self.assertFalse(os.path.exists(os.path.join(ilink.MEDIA_DIR, "old.jpg")))
        self.assertTrue(os.path.exists(os.path.join(ilink.MEDIA_DIR, "new.jpg")))
        self.assertTrue(os.path.exists(os.path.join(ilink.MEDIA_DIR, "quoted.jpg")))

    def test_size_cap_deletes_oldest_first(self):
        write_media("a.jpg", 100, 3)
        write_media("b.jpg", 100, 2)
        write_media("c.jpg", 100, 1)
        out = run_prune(max_age_days=365, max_total_bytes=150)
        self.assertIn("PRUNE_OK", out)
        # 300 bytes total, cap 150: oldest two go, newest stays.
        self.assertFalse(os.path.exists(os.path.join(ilink.MEDIA_DIR, "a.jpg")))
        self.assertFalse(os.path.exists(os.path.join(ilink.MEDIA_DIR, "b.jpg")))
        self.assertTrue(os.path.exists(os.path.join(ilink.MEDIA_DIR, "c.jpg")))

    def test_dry_run_deletes_nothing(self):
        write_media("old.jpg", 10, 40)
        out = run_prune(dry_run=True)
        self.assertIn("PRUNE_DRY_RUN", out)
        self.assertIn("deleted=1", out)
        self.assertTrue(os.path.exists(os.path.join(ilink.MEDIA_DIR, "old.jpg")))

    def test_missing_dir_is_ok(self):
        out = run_prune()
        self.assertIn("PRUNE_OK deleted=0", out)


if __name__ == "__main__":
    unittest.main()
