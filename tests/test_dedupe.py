#!/usr/bin/env python3
"""Tests for ilink.py's inbound message dedupe (filter_seen).

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_dedupe.py

getupdates can redeliver a message (same message_id) after a lost
response — without dedupe the user would receive two replies. These
tests pin down the filter's behavior against a temp seen-ids file;
no network involved.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["ILINK_DATA_DIR"] = tempfile.mkdtemp(prefix="ilink-test-data-")
import ilink  # noqa: E402


def msg(mid, **extra):
    m = {"from_user_id": "u1", "message_type": 1, "item_list": []}
    if mid is not None:
        m["message_id"] = mid
    m.update(extra)
    return m


class FilterSeenTest(unittest.TestCase):
    def setUp(self):
        fd, self.seen_path = tempfile.mkstemp(prefix="seen-", suffix=".json")
        os.close(fd)
        os.unlink(self.seen_path)  # start with no file at all
        self._orig = ilink.SEEN_FILE
        ilink.SEEN_FILE = self.seen_path
        self.addCleanup(self._restore)

    def _restore(self):
        ilink.SEEN_FILE = self._orig
        if os.path.exists(self.seen_path):
            os.unlink(self.seen_path)

    def test_first_delivery_passes_and_is_recorded(self):
        batch = [msg(101), msg(102)]
        kept = ilink.filter_seen(batch)
        self.assertEqual(kept, batch)
        self.assertEqual(ilink.load_seen(), ["101", "102"])

    def test_full_redelivery_is_dropped(self):
        batch = [msg(1), msg(2), msg(3)]
        ilink.filter_seen(batch)
        self.assertEqual(ilink.filter_seen(list(batch)), [])

    def test_partial_overlap_keeps_only_new_in_order(self):
        ilink.filter_seen([msg(1), msg(2)])
        kept = ilink.filter_seen([msg(2), msg(3), msg(1), msg(4)])
        self.assertEqual([m["message_id"] for m in kept], [3, 4])

    def test_duplicate_within_one_batch(self):
        kept = ilink.filter_seen([msg(7), msg(7), msg(8)])
        self.assertEqual([m["message_id"] for m in kept], [7, 8])

    def test_message_without_id_always_passes(self):
        anon = msg(None)
        self.assertEqual(ilink.filter_seen([anon]), [anon])
        self.assertEqual(ilink.filter_seen([anon]), [anon])  # still passes
        self.assertFalse(os.path.exists(self.seen_path))  # nothing recorded

    def test_string_and_int_ids_are_the_same(self):
        ilink.filter_seen([msg(555)])
        self.assertEqual(ilink.filter_seen([msg("555")]), [])

    def test_cap_rolls_off_oldest(self):
        ilink.filter_seen([msg(i) for i in range(ilink.SEEN_CAP + 100)])
        seen = ilink.load_seen()
        self.assertEqual(len(seen), ilink.SEEN_CAP)
        # The most recent id is still blocked, the rolled-off oldest is not.
        self.assertEqual(ilink.filter_seen([msg(ilink.SEEN_CAP + 99)]), [])
        self.assertEqual(len(ilink.filter_seen([msg(0)])), 1)


if __name__ == "__main__":
    unittest.main()
