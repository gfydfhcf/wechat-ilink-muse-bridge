#!/usr/bin/env python3
"""Tests for ilink.py's media download: AES decrypt + resumable transfer.

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_media_resume.py

Everything runs against a mock CDN on 127.0.0.1 (stdlib http.server) —
no WeChat account, no external network. The mock can:

- serve the full ciphertext in one go (clean download),
- cut the connection mid-transfer, then honor `Range:` on the retry
  (this is the resume path production relies on when the cross-Pacific
  tunnel drops a big image/video halfway),
- ignore `Range:` and always send the full body with 200 (fallback path),
- truncate forever (client must give up and return None, not a bad file).

The fixture uses the exact aes_key form real messages carry:
base64 of the 32-char hex key string (see ilink.py's protocol notes).
"""
import base64
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

# Import ilink.py from the repo root, with an isolated data dir so the
# downloaded media land in a temp folder.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["ILINK_DATA_DIR"] = tempfile.mkdtemp(prefix="ilink-test-data-")
import ilink  # noqa: E402


class MockCDN:
    """Serves `ciphertext`; behavior selected by `mode`. Records requests."""

    def __init__(self, ciphertext, mode, cut_ratio=0.4):
        self.ciphertext = ciphertext
        self.mode = mode
        self.cut = int(len(ciphertext) * cut_ratio)
        self.requests = []  # list of Range headers seen (None = no Range)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                rng = self.headers.get("Range")
                outer.requests.append(rng)
                data = outer.ciphertext
                start = 0
                if rng:  # bytes=<start>-
                    start = int(rng.split("=")[1].split("-")[0])
                if outer.mode == "ignore_range":
                    self._send(200, data, {})
                    return
                if outer.mode == "always_truncate":
                    # Every response (first or resume) delivers only part
                    # of what was asked, then drops: the client makes
                    # progress but can never finish within its retries.
                    piece = data[start : start + max(1, int((len(data) - start) * 0.4))]
                    if start:
                        self._send_truncated(
                            piece, len(data) - start, 206,
                            {"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"},
                        )
                    else:
                        self._send_truncated(piece, len(data), 200, {})
                    return
                if rng:  # proper resume
                    chunk = data[start:]
                    self._send(206, chunk, {
                        "Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}",
                    })
                    return
                if outer.mode == "drop_once" and len(outer.requests) == 1:
                    self._send_truncated(data[: outer.cut], len(data), 200, {})
                    return
                self._send(200, data, {})

            def _send(self, status, body, extra):
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                for k, v in extra.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            def _send_truncated(self, partial, promised_len, status, extra):
                # Promise `promised_len` bytes, deliver fewer, then drop.
                self.send_response(status)
                self.send_header("Content-Length", str(promised_len))
                for k, v in extra.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(partial)
                self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(1)
                self.connection.close()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/c2c/download"


class MediaDownloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = bytes(range(16))
        # ~100 KB of plaintext -> padded ciphertext; the size makes the
        # mid-transfer cut meaningful but keeps the test fast.
        cls.plaintext = os.urandom(100_000)
        cls.ciphertext = ilink._aes_ecb_encrypt(cls.plaintext, cls.key)
        # The form senders actually put in a message item.
        cls.aes_key_b64 = base64.b64encode(cls.key.hex().encode("ascii")).decode("ascii")

    def setUp(self):
        # download_media_item sleeps between retries (2s, 4s, ...); skip
        # the waiting in tests.
        patcher = mock.patch.object(ilink.time, "sleep", lambda *_a: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _item(self, url):
        return {"image_item": {"media": {"aes_key": self.aes_key_b64,
                                         "full_url": url}}}

    def _download(self, mode):
        with MockCDN(self.ciphertext, mode) as cdn:
            path = ilink.download_media_item(self._item(cdn.url), 2)
            requests = list(cdn.requests)
        self.assertIsNotNone(path, f"download failed in mode={mode}")
        with open(path, "rb") as f:
            got = f.read()
        os.unlink(path)
        return got, requests

    def test_clean_download(self):
        got, requests = self._download("clean")
        self.assertEqual(got, self.plaintext)
        self.assertEqual(requests, [None])  # one request, no Range

    def test_resume_after_mid_transfer_drop(self):
        got, requests = self._download("drop_once")
        self.assertEqual(got, self.plaintext)
        # The retry must resume from the cut point, not restart at zero.
        self.assertGreaterEqual(len(requests), 2)
        cut = int(len(self.ciphertext) * 0.4)
        self.assertEqual(requests[1], f"bytes={cut}-")

    def test_server_ignoring_range_still_works(self):
        got, _requests = self._download("ignore_range")
        self.assertEqual(got, self.plaintext)

    def test_permanent_truncation_gives_up(self):
        with MockCDN(self.ciphertext, "always_truncate") as cdn:
            path = ilink.download_media_item(self._item(cdn.url), 2)
        self.assertIsNone(path)  # never writes a corrupt file


class AesKeyParsingTest(unittest.TestCase):
    def test_base64_of_hex_string(self):
        key = bytes(range(16))
        b64 = base64.b64encode(key.hex().encode("ascii")).decode("ascii")
        self.assertEqual(ilink._parse_aes_key(b64), key)

    def test_base64_of_raw_key(self):
        key = bytes(range(16))
        b64 = base64.b64encode(key).decode("ascii")
        self.assertEqual(ilink._parse_aes_key(b64), key)


class MediaItemBuildTest(unittest.TestCase):
    """The send side: item shapes per media type (video added 2026-10)."""

    UP = {"media": {"encrypt_query_param": "p", "aes_key": "k", "encrypt_type": 1},
          "md5": "abc", "rawsize": 123, "cipher_size": 128}

    def test_video_item(self):
        item = ilink._media_item("/tmp/x.mp4", self.UP, 5)
        self.assertEqual(item["type"], 5)
        self.assertEqual(item["video_item"]["video_size"], 128)
        self.assertIs(item["video_item"]["media"], self.UP["media"])

    def test_image_and_file_items_unchanged(self):
        img = ilink._media_item("/tmp/x.jpg", self.UP, 2)
        self.assertEqual(img["image_item"]["mid_size"], 128)
        f = ilink._media_item("/tmp/x.pdf", self.UP, 4)
        self.assertEqual(f["file_item"]["len"], "123")
        self.assertEqual(f["file_item"]["file_name"], "x.pdf")


if __name__ == "__main__":
    unittest.main()
