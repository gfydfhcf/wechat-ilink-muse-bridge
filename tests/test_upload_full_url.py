#!/usr/bin/env python3
"""Tests for preferring getuploadurl's upload_full_url (BACKLOG item 10).

When the server provides a full upload URL, use it verbatim instead of
constructing the CDN URL from upload_param. When it does not, the
constructed URL is used as before. When it provides neither, the upload
fails as before.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_upload_full_url.py
"""
import os
import sys
import tempfile
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-uploadurl-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = tempfile.mkdtemp(prefix="ilink-test-state-")
import ilink  # noqa: E402

CAPTURED = {"url": None, "upload_response": {"upload_param": "P"}}
REAL_URL_OPEN = urllib.request.urlopen


class FakeResp:
    def __init__(self, headers):
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def fake_urlopen(req, timeout=None):
    CAPTURED["url"] = req.full_url
    return FakeResp({"x-encrypted-param": "DLPARAM"})


def fake_api(*args, **kwargs):
    path = args[1] if len(args) > 1 else ""
    if "getuploadurl" in path:
        return dict(CAPTURED["upload_response"])
    raise AssertionError(f"unexpected api call: {path}")


class UploadFullUrlTest(unittest.TestCase):
    def setUp(self):
        ilink.BRIDGE = DATA_DIR
        ilink.api = fake_api
        urllib.request.urlopen = fake_urlopen
        CAPTURED["url"] = None
        CAPTURED["upload_response"] = {"upload_param": "P"}

    def tearDown(self):
        urllib.request.urlopen = REAL_URL_OPEN

    def test_full_url_preferred(self):
        CAPTURED["upload_response"] = {
            "upload_param": "P",
            "upload_full_url": "https://cdn.example/full?x=1"}
        up = ilink._upload_media("u1", b"hello", 4, "tok")
        self.assertEqual(CAPTURED["url"], "https://cdn.example/full?x=1")
        self.assertEqual(up["media"]["encrypt_query_param"], "DLPARAM")

    def test_constructed_url_fallback(self):
        up = ilink._upload_media("u1", b"hello", 4, "tok")
        self.assertTrue(CAPTURED["url"].startswith(
            ilink.CDN_BASE_URL + "/upload?encrypted_query_param="))
        self.assertIn("filekey=", CAPTURED["url"])
        self.assertEqual(up["media"]["encrypt_query_param"], "DLPARAM")

    def test_full_url_without_param(self):
        CAPTURED["upload_response"] = {
            "upload_full_url": "https://cdn.example/only-full"}
        ilink._upload_media("u1", b"hello", 4, "tok")
        self.assertEqual(CAPTURED["url"], "https://cdn.example/only-full")

    def test_neither_raises(self):
        CAPTURED["upload_response"] = {}
        with self.assertRaises(RuntimeError):
            ilink._upload_media("u1", b"hello", 4, "tok")


if __name__ == "__main__":
    unittest.main()
