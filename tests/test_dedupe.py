#!/usr/bin/env python3
"""Tests for inbound dedupe under the two-phase poll protocol.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_dedupe.py

filter_seen() is READ ONLY: it splits a raw batch into (kept, new_ids)
without persisting anything. Ids are recorded by mark_seen(), which the
caller invokes at commit time — after the batch is durably queued.
Recording ids during poll (the original design) lost messages whenever
the process died between poll and queueing: the cursor was advanced
and the id marked, so the message could never be delivered again.
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

    def test_first_delivery_passes_and_is_not_yet_recorded(self):
        batch = [msg(101), msg(102)]
        kept, new_ids = ilink.filter_seen(batch)
        self.assertEqual(kept, batch)
        self.assertEqual(new_ids, ["101", "102"])
        # the whole point of two-phase: nothing is persisted yet
        self.assertEqual(ilink.load_seen(), [])
        self.assertFalse(os.path.exists(self.seen_path))

    def test_uncommitted_batch_is_redelivered_in_full(self):
        # simulates a crash between poll and commit: same batch again
        batch = [msg(1), msg(2), msg(3)]
        ilink.filter_seen(batch)
        kept, _ = ilink.filter_seen(list(batch))
        self.assertEqual(kept, batch)

    def test_committed_batch_is_dropped_on_redelivery(self):
        batch = [msg(1), msg(2), msg(3)]
        _, new_ids = ilink.filter_seen(batch)
        ilink.mark_seen(new_ids)
        kept, newer = ilink.filter_seen(list(batch))
        self.assertEqual(kept, [])
        self.assertEqual(newer, [])

    def test_partial_overlap_keeps_only_new_in_order(self):
        _, ids = ilink.filter_seen([msg(1), msg(2)])
        ilink.mark_seen(ids)
        kept, new_ids = ilink.filter_seen([msg(2), msg(3), msg(1), msg(4)])
        self.assertEqual([m["message_id"] for m in kept], [3, 4])
        self.assertEqual(new_ids, ["3", "4"])

    def test_duplicate_within_one_batch(self):
        kept, new_ids = ilink.filter_seen([msg(7), msg(7), msg(8)])
        self.assertEqual([m["message_id"] for m in kept], [7, 8])
        self.assertEqual(new_ids, ["7", "8"])

    def test_message_without_id_always_passes(self):
        anon = msg(None)
        kept, new_ids = ilink.filter_seen([anon])
        self.assertEqual(kept, [anon])
        self.assertEqual(new_ids, [])
        ilink.mark_seen(new_ids)  # no-op
        kept, _ = ilink.filter_seen([anon])
        self.assertEqual(kept, [anon])  # still passes
        self.assertFalse(os.path.exists(self.seen_path))  # nothing recorded

    def test_string_and_int_ids_are_the_same(self):
        _, ids = ilink.filter_seen([msg(555)])
        ilink.mark_seen(ids)
        kept, _ = ilink.filter_seen([msg("555")])
        self.assertEqual(kept, [])

    def test_cap_rolls_off_oldest(self):
        _, ids = ilink.filter_seen([msg(i) for i in range(ilink.SEEN_CAP + 100)])
        ilink.mark_seen(ids)
        # S3: a single batch larger than the cap is kept whole —
        # truncating it would silently lose dedupe for the dropped ids.
        seen = ilink.load_seen()
        self.assertEqual(len(seen), ilink.SEEN_CAP + 100)
        # The most recent id is still blocked.
        kept, _ = ilink.filter_seen([msg(ilink.SEEN_CAP + 99)])
        self.assertEqual(kept, [])
        # A later small batch rolls the store back down to the cap,
        # dropping the oldest ids first.
        _, more = ilink.filter_seen([msg(i) for i in range(ilink.SEEN_CAP + 100,
                                                          ilink.SEEN_CAP + 200)])
        ilink.mark_seen(more)
        seen = ilink.load_seen()
        self.assertEqual(len(seen), ilink.SEEN_CAP)
        kept, _ = ilink.filter_seen([msg(0)])
        self.assertEqual(len(kept), 1)


if __name__ == "__main__":
    unittest.main()
