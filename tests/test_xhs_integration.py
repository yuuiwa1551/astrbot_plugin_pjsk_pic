from __future__ import annotations

import asyncio
import importlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tests.test_xhs_auto_crawl import (
    ImageIndexDB,
    PACKAGE_NAME,
    XhsProviderClient,
)


crawl_module = importlib.import_module(f"{PACKAGE_NAME}.crawl_service")
importer_module = importlib.import_module(f"{PACKAGE_NAME}.importer")
review_module = importlib.import_module(f"{PACKAGE_NAME}.review_service")

CrawlService = crawl_module.CrawlService
ImportedImageService = importer_module.ImportedImageService
ReviewService = review_module.ReviewService


@unittest.skipUnless(
    os.environ.get("PJSK_XHS_INTEGRATION") == "1",
    "set PJSK_XHS_INTEGRATION=1 to run against an isolated provider",
)
class XhsProviderIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_note_downloads_to_an_isolated_staging_database(self) -> None:
        provider_url = os.environ.get("PJSK_XHS_PROVIDER_URL", "http://127.0.0.1:18060")
        config = {
            "xhs_provider_base_url": provider_url,
            "xhs_provider_access_token": os.environ.get("PJSK_XHS_PROVIDER_TOKEN", ""),
            "xhs_provider_min_interval_seconds": 2.0,
            "xhs_provider_timeout_seconds": 45,
            "platform_request_timeout": 45,
            "platform_retry_times": 1,
            "crawler_max_candidates": 6,
            "xhs_max_images_per_note": 60,
            "crawl_keep_primary_tags_only": True,
            "enable_auto_review": False,
            "guess_character_tags": False,
        }
        provider = XhsProviderClient(config)
        crawl_service = None
        with tempfile.TemporaryDirectory() as temp_name:
            data_dir = Path(temp_name)
            db_path = data_dir / "image_index.db"
            db = ImageIndexDB(db_path)
            db.get_or_create_tag("初音未来", is_character=True)
            ok, message = db.add_platform_term(
                "初音未来",
                "初音未来",
                platform="xiaohongshu",
                term_type="both",
            )
            self.assertTrue(ok, message)

            try:
                self.assertEqual("healthy", provider.health()["status"])
                self.assertTrue(provider.login_status())
                hits = provider.search_notes("初音未来", max_results=3)
                self.assertTrue(hits)

                selected = None
                for hit in hits[:3]:
                    detail = provider.fetch_note_detail(hit.note_id, hit.xsec_token)
                    if detail.images:
                        selected = (hit, detail)
                    if detail.images and len(detail.images) <= 3:
                        break
                self.assertIsNotNone(selected)
                hit, detail = selected

                importer = ImportedImageService(
                    db,
                    data_dir,
                    timeout_seconds=45,
                    enable_phash_dedupe=True,
                )
                reviewer = ReviewService(None, db, config)
                crawl_service = CrawlService(
                    db=db,
                    importer=importer,
                    reviewer=reviewer,
                    config=config,
                    xhs_provider_client=provider,
                )
                await crawl_service.start()
                job_id, created = await crawl_service.submit_job_once(
                    "xiaohongshu",
                    hit.post_url,
                    ["初音未来"],
                    source_context={
                        "note_id": hit.note_id,
                        "xsec_token": hit.xsec_token,
                        "provider": "xiaohongshu_mcp_rest",
                    },
                )
                self.assertTrue(created)
                await asyncio.wait_for(crawl_service._queue.join(), timeout=240)

                job = db.get_crawl_job(job_id)
                self.assertIsNotNone(job)
                self.assertEqual("completed", str(job["status"]), str(job["error_log"] or ""))
                self.assertTrue(db.has_source_post_url(hit.post_url, platform="xiaohongshu"))
                self.assertGreater(
                    db.count_open_review_images(platform="xiaohongshu"),
                    0,
                )

                _, duplicate_created = await crawl_service.submit_job_once(
                    "xiaohongshu",
                    hit.post_url,
                    ["初音未来"],
                    source_context={"note_id": hit.note_id, "xsec_token": hit.xsec_token},
                )
                self.assertFalse(duplicate_created)

                with closing(sqlite3.connect(db_path)) as conn:
                    source_count = conn.execute(
                        "SELECT COUNT(*) FROM sources WHERE platform = 'xiaohongshu'"
                    ).fetchone()[0]
                    tag_names = [
                        row[0]
                        for row in conn.execute("SELECT name FROM tags ORDER BY id")
                    ]
                self.assertEqual(len(detail.images), int(source_count))
                self.assertEqual(["初音未来"], tag_names)
            finally:
                if crawl_service is not None:
                    await crawl_service.stop()
                provider.close()


if __name__ == "__main__":
    unittest.main()
