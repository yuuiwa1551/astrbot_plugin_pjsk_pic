from __future__ import annotations

import importlib
import asyncio
import tempfile
import unittest
from pathlib import Path

from tests.test_xhs_auto_crawl import (
    FakeCrawlService,
    ImageIndexDB,
    PACKAGE_NAME,
    XhsImageRef,
    XhsNoteDetail,
    XhsProviderError,
    XhsSearchHit,
    XhsSearchPage,
)


backfill_module = importlib.import_module(f"{PACKAGE_NAME}.xhs_backfill_service")
XhsBackfillService = backfill_module.XhsBackfillService


class BackfillCrawlService(FakeCrawlService):
    async def enqueue_persisted_job(self, job_id):
        self.jobs[job_id] = {'origin': 'backfill', 'priority': 50}
    async def submit_job_once(self, platform, source_url, tags, **kwargs):
        job_id, created = await super().submit_job_once(
            platform,
            source_url,
            tags,
            **kwargs,
        )
        job = self.jobs[source_url]
        job["origin"] = kwargs.get("origin")
        job["priority"] = kwargs.get("priority")
        return job_id, created


class PagedProvider:
    def __init__(self, *, fail_page_2_once: bool = False) -> None:
        self.page_calls: list[int] = []
        self.detail_calls: list[str] = []
        self.fail_page_2_once = fail_page_2_once

    @staticmethod
    def supports_pagination() -> bool:
        return True

    @staticmethod
    def source_name() -> str:
        return "xiaohongshu_cli_rest"

    def search_notes_page(self, _keyword: str, *, page: int, **_kwargs) -> XhsSearchPage:
        self.page_calls.append(page)
        if page == 2 and self.fail_page_2_once:
            self.fail_page_2_once = False
            raise XhsProviderError("page 2 timeout", category="timeout", retryable=True)
        ids = ["note-1", "note-2"] if page == 1 else ["note-3"]
        return XhsSearchPage(
            hits=[
                XhsSearchHit(
                    note_id=note_id,
                    xsec_token=f"context-{note_id}",
                    post_url=f"https://www.xiaohongshu.com/explore/{note_id}",
                )
                for note_id in ids
            ],
            page=page,
            has_more=page == 1,
        )

    def fetch_note_detail(self, note_id: str, xsec_token: str, **_kwargs) -> XhsNoteDetail:
        self.detail_calls.append(note_id)
        return XhsNoteDetail(
            note_id=note_id,
            xsec_token=xsec_token,
            post_url=f"https://www.xiaohongshu.com/explore/{note_id}",
            title="初音未来",
            topics=["初音未来"],
            images=[
                XhsImageRef(
                    url=f"https://sns-webpic-qc.xhscdn.com/{note_id}.webp",
                    index=1,
                )
            ],
        )


class XhsBackfillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = ImageIndexDB(Path(self.temp_dir.name) / "image_index.db")
        self.db.get_or_create_tag("初音未来", is_character=True)
        self.db.add_platform_term(
            "初音未来",
            "初音未来",
            platform="xiaohongshu",
            term_type="both",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_two_page_backfill_queues_low_priority_jobs(self) -> None:
        provider = PagedProvider()
        crawl_service = BackfillCrawlService()
        service = XhsBackfillService(
            db=self.db,
            crawl_service=crawl_service,
            config={"xhs_backfill_page_interval_seconds": 0},
            provider_client=provider,
        )
        task_id, _info = await service.create_task(
            tag_text="初音未来",
            max_pages=2,
            max_results=10,
            max_new_jobs=10,
        )

        await service.start()
        await service._queue.join()
        await service.stop()

        row = self.db.get_xhs_backfill_task(task_id)
        self.assertEqual("completed", str(row["status"]))
        self.assertEqual(3, int(row["scanned"]))
        self.assertEqual(3, int(row["detailed"]))
        self.assertEqual(3, int(row["queued"]))
        self.assertEqual([1, 2], provider.page_calls)
        self.assertEqual(3, len(crawl_service.jobs))
        self.assertEqual(
            {("backfill", 50)},
            {(job["origin"], job["priority"]) for job in crawl_service.jobs.values()},
        )

    async def test_retry_resumes_from_failed_page_checkpoint(self) -> None:
        provider = PagedProvider(fail_page_2_once=True)
        crawl_service = BackfillCrawlService()
        service = XhsBackfillService(
            db=self.db,
            crawl_service=crawl_service,
            config={"xhs_backfill_page_interval_seconds": 0},
            provider_client=provider,
        )
        task_id, _info = await service.create_task(
            tag_text="初音未来",
            max_pages=2,
            max_results=10,
            max_new_jobs=10,
        )
        await service.start()
        await service._queue.join()
        failed = self.db.get_xhs_backfill_task(task_id)
        self.assertEqual("failed", str(failed["status"]))
        self.assertEqual(2, int(failed["next_page"]))

        ok, _message = await service.retry_task(task_id)
        self.assertTrue(ok)
        await service._queue.join()
        await service.stop()

        completed = self.db.get_xhs_backfill_task(task_id)
        self.assertEqual("completed", str(completed["status"]))
        self.assertEqual([1, 2, 2], provider.page_calls)
        self.assertEqual(3, len(crawl_service.jobs))

    async def test_half_page_restart_preserves_budget_and_skips_processed_note(self):
        provider = PagedProvider()
        original = provider.fetch_note_detail
        def fail_second(note_id, token, **kwargs):
            if note_id == 'note-2':
                raise XhsProviderError('temporary', retryable=True)
            return original(note_id, token, **kwargs)
        provider.fetch_note_detail = fail_second
        service = XhsBackfillService(db=self.db, crawl_service=BackfillCrawlService(),
            config={'xhs_backfill_page_interval_seconds': 0}, provider_client=provider)
        task_id, _ = await service.create_task(tag_text='初音未来', max_new_jobs=2)
        await service.start()
        await service._queue.join()
        await service.stop()
        row = self.db.get_xhs_backfill_task(task_id)
        self.assertEqual((1, 1, 1), (row['next_page'], row['page_item_index'], row['queued']))
        provider.fetch_note_detail = original
        resumed = XhsBackfillService(db=self.db, crawl_service=BackfillCrawlService(),
            config={'xhs_backfill_page_interval_seconds': 0}, provider_client=provider)
        await resumed.retry_task(task_id)
        await resumed.start()
        await resumed._queue.join()
        await resumed.stop()
        row = self.db.get_xhs_backfill_task(task_id)
        self.assertEqual(('limited', 2, 2), (row['status'], row['scanned'], row['queued']))
        self.assertEqual([1], provider.page_calls)
        self.assertEqual(['note-1', 'note-2'], provider.detail_calls)
        self.assertEqual(2, sum(self.db.count_crawl_jobs_by_status().values()))

    async def test_pause_and_incremental_cycle_hold_backfill_requests(self):
        provider = PagedProvider()
        incremental = type('Incremental', (), {'_run_lock': asyncio.Lock()})()
        service = XhsBackfillService(db=self.db, crawl_service=BackfillCrawlService(),
            config={'xhs_backfill_page_interval_seconds': 0}, provider_client=provider,
            incremental_service=incremental)
        await service.create_task(tag_text='初音未来', max_new_jobs=1)
        self.db.set_crawl_provider_state('xiaohongshu', status='paused')
        await service.start()
        await asyncio.sleep(0.3)
        self.assertEqual([], provider.page_calls)
        await incremental._run_lock.acquire()
        self.db.set_crawl_provider_state('xiaohongshu', status='active')
        await asyncio.sleep(0.3)
        self.assertEqual([], provider.page_calls)
        incremental._run_lock.release()
        await asyncio.wait_for(service._queue.join(), 5)
        await service.stop()
        self.assertEqual([1], provider.page_calls)


if __name__ == "__main__":
    unittest.main()
