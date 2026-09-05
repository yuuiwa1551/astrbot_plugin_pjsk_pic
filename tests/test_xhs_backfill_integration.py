from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from tests.test_xhs_integration import (
    ImageIndexDB, XhsProviderClient, CrawlService, ImportedImageService, ReviewService,
)
from tests.test_xhs_backfill import XhsBackfillService


@unittest.skipUnless(os.environ.get('PJSK_XHS_BACKFILL_INTEGRATION') == '1', 'opt-in live sidecar smoke')
class BackfillIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_second_page_downloads_all_images_to_temporary_gallery(self):
        config = {
            'xhs_provider_kind': 'xiaohongshu_cli',
            'xhs_provider_base_url': os.environ['PJSK_XHS_PROVIDER_URL'],
            'xhs_provider_access_token': os.environ['PJSK_XHS_PROVIDER_TOKEN'],
            'xhs_backfill_page_interval_seconds': 0,
            'xhs_provider_min_interval_seconds': 2,
            'guess_character_tags': False,
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = ImageIndexDB(root / 'image_index.db')
            db.get_or_create_tag('初音未来', is_character=True)
            db.add_platform_term('初音未来', '初音未来', platform='xiaohongshu', term_type='both')
            provider = XhsProviderClient(config)
            importer = ImportedImageService(db, root, timeout_seconds=45)
            crawl = CrawlService(db=db, importer=importer, reviewer=ReviewService(None, db, config),
                                 config=config, xhs_provider_client=provider)
            backfill = XhsBackfillService(db=db, crawl_service=crawl, config=config, provider_client=provider)
            try:
                task_id, _ = await backfill.create_task(tag_text='初音未来', max_pages=2, max_results=5, max_new_jobs=1)
                db.update_xhs_backfill_task(task_id, next_page=2)
                # Capture declared image counts before successful jobs release their snapshots.
                expected_counts = {}
                original = provider.fetch_note_detail
                def capture(note_id, token, **kwargs):
                    detail = original(note_id, token, **kwargs)
                    expected_counts[detail.post_url] = len(detail.images)
                    return detail
                provider.fetch_note_detail = capture
                await backfill.start()
                await asyncio.wait_for(backfill._queue.join(), 180)
                task = db.get_xhs_backfill_task(task_id)
                self.assertEqual('limited', task['status'])
                self.assertEqual(1, task['queued'])
                await crawl.start()
                await asyncio.wait_for(crawl._queue.join(), 180)
                job = db.get_latest_crawl_job()
                self.assertEqual('completed', job['status'], job['error_log'])
                with db._connect() as conn:
                    sources = conn.execute('SELECT COUNT(*) FROM sources WHERE post_url=?', (job['source_url'],)).fetchone()[0]
                    self.assertEqual('ok', conn.execute('PRAGMA integrity_check').fetchone()[0])
                self.assertEqual(expected_counts[job['source_url']], sources)
                self.assertGreater(db.count_open_review_images(platform='xiaohongshu'), 0)
                print(f'real_second_page_smoke: jobs=1 sources={sources} status=completed temporary_db=True')
            finally:
                await backfill.stop()
                await crawl.stop()
                importer.close()
                provider.close()
