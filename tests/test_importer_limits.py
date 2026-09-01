from __future__ import annotations

import importlib
import tempfile
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tests.test_xhs_auto_crawl import ImageIndexDB, PACKAGE_NAME


importer_module = importlib.import_module(f"{PACKAGE_NAME}.importer")
ImportedImageService = importer_module.ImportedImageService
CrawlCandidate = importer_module.CrawlCandidate
ImportedImage = importer_module.ImportedImage


class FakeDownloadResponse:
    def __init__(self, body: bytes, *, content_length: str = "") -> None:
        self.body = body
        self.headers = {
            "Content-Type": "image/webp",
            "Content-Length": content_length,
        }
        self.url = "https://sns-webpic-qc.xhscdn.com/test.webp"

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]


class FakeDownloadSession:
    def __init__(self, response: FakeDownloadResponse) -> None:
        self.response = response
        self.calls = 0

    def mount(self, *_args, **_kwargs) -> None:
        return None

    def get(self, *_args, **_kwargs) -> FakeDownloadResponse:
        self.calls += 1
        return self.response

    def close(self) -> None:
        return None


class ImporterDownloadLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = ImageIndexDB(Path(self.temp_dir.name) / "image_index.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_declared_oversized_response_is_rejected_without_retry(self) -> None:
        response = FakeDownloadResponse(b"", content_length=str(2 * 1024 * 1024))
        session = FakeDownloadSession(response)
        service = ImportedImageService(
            self.db,
            Path(self.temp_dir.name),
            max_download_bytes=1024 * 1024,
            session=session,
        )
        with self.assertRaisesRegex(ValueError, "超过下载上限"):
            service._download_remote_bytes("https://example.invalid/image", headers={})
        self.assertEqual(1, session.calls)

    def test_stream_larger_than_limit_is_rejected_without_content_length(self) -> None:
        response = FakeDownloadResponse(b"x" * (1024 * 1024 + 1))
        session = FakeDownloadSession(response)
        service = ImportedImageService(
            self.db,
            Path(self.temp_dir.name),
            max_download_bytes=1024 * 1024,
            session=session,
        )
        with self.assertRaisesRegex(ValueError, "实际内容超过下载上限"):
            service._download_remote_bytes("https://example.invalid/image", headers={})
        self.assertEqual(1, session.calls)

    def test_exact_sha_duplicate_skips_second_phash_calculation(self) -> None:
        output = BytesIO()
        Image.new("RGB", (800, 800), (20, 40, 60)).save(output, format="JPEG")
        body = output.getvalue()
        service = ImportedImageService(self.db, Path(self.temp_dir.name))
        first = service._store_imported_bytes(
            body,
            source_name="first.jpg",
            content_type="image/jpeg",
            platform="pixiv",
        )

        with patch.object(importer_module, "compute_image_phash", side_effect=AssertionError("should not run")):
            second = service._store_imported_bytes(
                body,
                source_name="second.jpg",
                content_type="image/jpeg",
                platform="xiaohongshu",
            )

        self.assertEqual(first.image_id, second.image_id)
        service.close()

    def test_phash_lookup_includes_images_older_than_500_rows(self) -> None:
        with self.db._connect() as conn:
            conn.execute(
                """
                INSERT INTO images(
                    file_path, file_name, sha256, phash, width, height, format,
                    is_active, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 100, 100, 'jpeg', 1, ?, ?)
                """,
                ("old.jpg", "old.jpg", "old-sha", "0000000000000000", "2026-01-01", "2026-01-01"),
            )
            old_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            for index in range(501):
                conn.execute(
                    """
                    INSERT INTO images(
                        file_path, file_name, sha256, phash, width, height, format,
                        is_active, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, 100, 100, 'jpeg', 1, ?, ?)
                    """,
                    (
                        f"recent-{index}.jpg",
                        f"recent-{index}.jpg",
                        f"recent-sha-{index}",
                        "ffffffffffffffff",
                        "2026-01-02",
                        "2026-01-02",
                    ),
                )

        matches = self.db.find_similar_images_by_phash(
            "0000000000000000",
            max_distance=0,
        )

        self.assertIn(old_id, {int(row["id"]) for row in matches})


class ImporterConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_import_uses_bounded_concurrency_and_preserves_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = ImageIndexDB(root / "image_index.db")
            service = ImportedImageService(db, root)
            lock = threading.Lock()
            active = 0
            max_active = 0

            def fake_import(candidate: CrawlCandidate) -> ImportedImage:
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                with lock:
                    active -= 1
                index = int(candidate.source_uid)
                return ImportedImage(
                    image_id=index,
                    file_path=root / f"{index}.jpg",
                    sha256=f"sha-{index}",
                    phash="",
                    width=100,
                    height=100,
                    format="jpeg",
                )

            service._import_candidate_sync = fake_import
            candidates = [
                CrawlCandidate(
                    platform="pixiv",
                    post_url="https://www.pixiv.net/artworks/1",
                    image_url=f"https://i.pximg.net/{index}.jpg",
                    source_uid=str(index),
                )
                for index in range(1, 5)
            ]

            results = await service.import_candidates(candidates, concurrency=2)

            self.assertEqual(2, max_active)
            self.assertEqual([1, 2, 3, 4], [result.image_id for result in results])
            service.close()


if __name__ == "__main__":
    unittest.main()
