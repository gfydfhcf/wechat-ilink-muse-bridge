#!/usr/bin/env python3
"""Tests for the outbound voice item builder.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_voice_item.py

The iLink channel silently drops outbound voice items (verified live
2026-10-22; the API returns success, the client never renders the
message). These tests only pin the item/upload shapes so the code path
stays well-formed if the channel ever starts delivering voice.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["ILINK_DATA_DIR"] = tempfile.mkdtemp(prefix="ilink-test-data-")
import ilink  # noqa: E402

UP = {"media": {"encrypt_query_param": "p", "aes_key": "k", "encrypt_type": 1},
      "md5": "0" * 32, "rawsize": 100, "cipher_size": 112}


class VoiceItemTest(unittest.TestCase):
    def test_voice_item_shape(self):
        item = ilink._media_item("v.silk", UP, 3, playtime_ms=4560)
        self.assertEqual(item["type"], 3)
        v = item["voice_item"]
        self.assertEqual(v["media"], UP["media"])
        self.assertEqual(v["encode_type"], 6)  # SILK
        self.assertEqual(v["sample_rate"], 24000)
        self.assertEqual(v["bits_per_sample"], 16)
        self.assertEqual(v["playtime"], 4560)

    def test_other_items_unaffected(self):
        img = ilink._media_item("a.jpg", UP, 2)
        self.assertEqual(img["type"], 2)
        fil = ilink._media_item("a.pdf", UP, 4)
        self.assertEqual(fil["type"], 4)
        self.assertEqual(fil["file_item"]["len"], "100")


if __name__ == "__main__":
    unittest.main()
