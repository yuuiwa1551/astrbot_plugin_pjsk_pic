from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_xhs_auto_crawl import ImageIndexDB, PACKAGE_NAME


importer_module = importlib.import_module(f"{PACKAGE_NAME}.importer")
ImportedImageService = importer_module.ImportedImageService


class FakeDownloadResponse:
    def __init__(self, body: bytes, *, content_length: str = "") -> None:
        self.body = body
        self.headers = {
            "Content-Type": "image/webp",
            "Content-Length": content_length,
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]

    def geturl(self) -> str:
        return "https://sns-webpic-qc.xhscdn.com/test.webp"


class ImporterDownloadLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = ImageIndexDB(Path(self.temp_dir.name) / "image_index.db")
        self.service = ImportedImageService(
            self.db,
            Path(self.temp_dir.name),
            max_download_bytes=1024 * 1024,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_declared_oversized_response_is_rejected_without_retry(self) -> None:
        response = FakeDownloadResponse(b"", content_length=str(2 * 1024 * 1024))
        with patch.object(importer_module.urllib.request, "urlopen", return_value=response) as mocked:
            with self.assertRaisesRegex(ValueError, "超过下载上限"):
                self.service._download_remote_bytes("https://example.invalid/image", headers={})
        self.assertEqual(1, mocked.call_count)

    def test_stream_larger_than_limit_is_rejected_without_content_length(self) -> None:
        response = FakeDownloadResponse(b"x" * (1024 * 1024 + 1))
        with patch.object(importer_module.urllib.request, "urlopen", return_value=response) as mocked:
            with self.assertRaisesRegex(ValueError, "实际内容超过下载上限"):
                self.service._download_remote_bytes("https://example.invalid/image", headers={})
        self.assertEqual(1, mocked.call_count)


if __name__ == "__main__":
    unittest.main()
