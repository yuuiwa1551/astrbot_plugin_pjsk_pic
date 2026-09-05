from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any, Awaitable, Callable

from astrbot.api import logger

from .db import utcnow_str
from .matcher import normalize_tag_name
from .xhs_provider import (
    XhsNoteDetail,
    XhsProviderClient,
    XhsProviderError,
    XhsSearchHit,
    xhs_note_detail_to_snapshot,
)


class XhsBackfillService:
    PLATFORM = "xiaohongshu"

    def __init__(
        self,
        *,
        db,
        crawl_service,
        config,
        provider_client: XhsProviderClient,
        pause_handler: Callable[[XhsProviderError], Awaitable[None]] | None = None,
        incremental_service=None,
    ) -> None:
        self.db = db
        self.crawl_service = crawl_service
        self.config = config
        self.provider_client = provider_client
        self.pause_handler = pause_handler
        self.incremental_service = incremental_service
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._queued_ids: set[int] = set()
        self._worker_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    def queue_size(self) -> int:
        return int(self._queue.qsize())

    def worker_running(self) -> bool:
        return self._worker_task is not None and not self._worker_task.done()

    def page_size(self) -> int:
        return min(max(1, int(self.config.get("xhs_backfill_page_size", 20) or 20)), 50)

    def page_interval_seconds(self) -> float:
        return max(0.0, float(self.config.get("xhs_backfill_page_interval_seconds", 30) or 0))

    async def start(self) -> None:
        self.db.reset_running_xhs_backfill_tasks()
        self._stop_event.clear()
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._worker_loop(),
                name="pjsk-pic-xhs-backfill",
            )
        for task_id in self.db.get_pending_xhs_backfill_task_ids():
            await self._enqueue(task_id)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    async def create_task(
        self,
        *,
        tag_text: str,
        max_pages: int = 10,
        max_results: int = 200,
        max_new_jobs: int = 50,
    ) -> tuple[int, dict[str, Any]]:
        if not self.provider_client.supports_pagination():
            raise ValueError("当前小红书 provider 不支持真实分页，请先切换到 xiaohongshu_cli。")
        resolved = self._resolve_tag(tag_text)
        tag_name = str(resolved["name"])
        tag = self.db.get_tag_row(tag_name)
        if tag['status'] != 'active' or tag['tag_type'] != 'character':
            raise ValueError("回填只支持启用的角色主 tag。")
        query_terms = self.db.get_platform_terms_for_tag(
            tag_name=tag_name,
            platform=self.PLATFORM,
            purpose="query",
            include_aliases=False,
            include_primary=False,
        )
        match_terms = self.db.get_platform_terms_for_tag(
            tag_name=tag_name,
            platform=self.PLATFORM,
            purpose="match",
            include_aliases=False,
            include_primary=False,
        )
        if not query_terms or not match_terms:
            raise ValueError("该 tag 必须先配置小红书 query 与 match/both 平台词。")
        task_id = self.db.create_xhs_backfill_task(
            tag_id=int(resolved["id"]),
            tag_name=tag_name,
            tag_text=str(tag_text or "").strip(),
            query_terms=self._unique_texts(query_terms)[:5],
            match_terms=self._unique_texts(match_terms),
            max_pages=self._bounded_int(max_pages, 10, 1, 100),
            max_results=self._bounded_int(max_results, 200, 1, 2000),
            max_new_jobs=self._bounded_int(max_new_jobs, 50, 1, 500),
        )
        self.db.update_xhs_backfill_task(task_id, page_size=self.page_size())
        await self._enqueue(task_id)
        return task_id, {
            "tag_id": int(resolved["id"]),
            "tag_name": tag_name,
            "query_terms": self._unique_texts(query_terms)[:5],
            "match_terms": self._unique_texts(match_terms),
        }

    async def retry_task(self, task_id: int) -> tuple[bool, str]:
        row = self.db.get_xhs_backfill_task(int(task_id))
        if not row:
            return False, f"小红书回填任务不存在：{task_id}"
        if row['status'] != 'failed':
            return False, "仅失败任务可从断点重试；已达上限任务请按新预算重新创建。"
        self.db.update_xhs_backfill_task(
            int(task_id),
            status="retry",
            error_log="",
            completed_at="",
        )
        await self._enqueue(int(task_id))
        return True, f"已从 checkpoint 重新入队小红书回填任务 #{task_id}"

    async def _enqueue(self, task_id: int) -> None:
        if task_id in self._queued_ids:
            return
        self._queued_ids.add(task_id)
        await self._queue.put(task_id)

    async def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                task_id = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._process_task(task_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    f"[PJSKPic] 小红书历史回填任务 #{task_id} 执行异常: {exc}",
                    exc_info=True,
                )
                self.db.update_xhs_backfill_task(
                    task_id,
                    status="failed",
                    error_log=str(exc),
                )
            finally:
                self._queued_ids.discard(task_id)
                self._queue.task_done()

    async def _wait_for_turn(self) -> None:
        while True:
            state = self.db.get_crawl_provider_state(self.PLATFORM)
            paused = state is not None and state["status"] == "paused"
            incremental_busy = self.incremental_service is not None and self.incremental_service._run_lock.locked()
            if not paused and not incremental_busy:
                return
            await asyncio.sleep(0.25)

    async def _provider_call(self, method, *args, **kwargs):
        await self._wait_for_turn()
        # An already running request completes; incrementals get the next request.
        try:
            return await asyncio.to_thread(method, *args, **kwargs)
        except XhsProviderError as exc:
            self.db.record_crawl_provider_check(self.PLATFORM, success=False, error=str(exc))
            if exc.pause_required and self.pause_handler is not None:
                await self.pause_handler(exc)
            raise

    async def _process_task(self, task_id: int) -> None:
        row = self.db.get_xhs_backfill_task(task_id)
        if row is None:
            return
        if not self.provider_client.supports_pagination():
            raise ValueError("当前 provider 不支持分页，回填 checkpoint 保留。")
        queries = self._json_texts(row["query_terms_json"])
        matches = self._json_texts(row["match_terms_json"])
        timeout = int(self.config.get("xhs_provider_timeout_seconds", 45))
        include_tags, exclude_tags = self.crawl_service.resolve_filter_sets(platform=self.PLATFORM)
        self.db.update_xhs_backfill_task(task_id, status="running", error_log="")
        while True:
            row = self.db.get_xhs_backfill_task(task_id)
            query_index = int(row["current_query_index"])
            if query_index >= len(queries):
                self._complete_task(task_id)
                return
            if row["scanned"] >= row["max_results"] or row["queued"] >= row["max_new_jobs"]:
                self._complete_task(task_id, limited=True)
                return
            query = queries[query_index]
            page_number = int(row["next_page"])
            if page_number > row["max_pages"]:
                self._complete_task(task_id, limited=True)
                return
            snapshot = row["page_snapshot_json"]
            if not snapshot:
                await self.crawl_service.wait_for_backfill_capacity()
                page = await self._provider_call(
                    self.provider_client.search_notes_page, query, page=page_number,
                    max_results=int(row["page_size"]), timeout_seconds=timeout, publish_time="不限",
                )
                snapshot = json.dumps({
                    "hits": [asdict(hit) for hit in page.hits],
                    "has_more": page.has_more,
                }, ensure_ascii=False)
                self.db.update_xhs_backfill_task(
                    task_id, current_query_text=query, page_snapshot_json=snapshot, page_item_index=0,
                )
                row = self.db.get_xhs_backfill_task(task_id)
            page_data = json.loads(snapshot)
            hits = page_data["hits"]
            index = int(row["page_item_index"])
            if index == len(hits):
                exhausted = not page_data["has_more"]
                if exhausted:
                    self.db.clear_xhs_backfill_saturation(task_id, query)
                self.db.update_xhs_backfill_task(
                    task_id, current_query_index=query_index + int(exhausted),
                    current_query_text="" if exhausted else query,
                    next_page=1 if exhausted else page_number + 1,
                    page_snapshot_json="", page_item_index=0,
                )
                if not exhausted:
                    await asyncio.sleep(self.page_interval_seconds())
                continue
            hit = XhsSearchHit(**hits[index])
            await self._wait_for_turn()
            outcome = ""
            detail = None
            context = None
            if self.db.has_xhs_backfill_item(task_id, hit.note_id):
                outcome = "skipped_duplicate"
            elif self.db.is_rejected_source_post_url(hit.post_url, platform=self.PLATFORM):
                outcome = "skipped_rejected"
            elif (self.db.has_source_post_url(hit.post_url, platform=self.PLATFORM)
                  or self.db.has_crawl_job_source_url(hit.post_url, platform=self.PLATFORM)):
                outcome = "skipped_existing"
            else:
                try:
                    detail = await self._provider_call(
                        self.provider_client.fetch_note_detail, hit.note_id, hit.xsec_token,
                        timeout_seconds=timeout,
                    )
                except XhsProviderError as exc:
                    if exc.pause_required or exc.retryable:
                        raise
                    outcome = "failed_details"
                    self.db.update_xhs_backfill_task(task_id, error_log=str(exc))
                if detail is not None:
                    filtered = self.crawl_service.filter_reason_for_tags(
                        detail.topics, include_tags=include_tags,
                        exclude_tags=exclude_tags, match_mode="exact",
                    )
                    if not detail.images or filtered is not None or not self._matches_target(detail, matches):
                        outcome = "skipped_filtered"
                    else:
                        outcome = "matched"
                        context = {
                            "note_id": hit.note_id, "xsec_token": hit.xsec_token,
                            "provider": self.provider_client.source_name(), "filters_applied": True,
                            "detail_snapshot": xhs_note_detail_to_snapshot(detail),
                        }
            await self.crawl_service.wait_for_backfill_capacity()
            await self._wait_for_turn()
            job_id = self.db.commit_xhs_backfill_item(
                task_id, note_id=hit.note_id, post_url=hit.post_url, next_index=index + 1,
                outcome=outcome, detailed=detail is not None, source_context=context,
            )
            if job_id:
                await self.crawl_service.enqueue_persisted_job(job_id)

    def _complete_task(self, task_id: int, *, limited: bool = False) -> None:
        # A limit is not proof that the historical gap has been covered.
        self.db.update_xhs_backfill_task(
            task_id, status="limited" if limited else "completed",
            completed_at=utcnow_str(),
            **({} if limited else {"page_snapshot_json": "", "page_item_index": 0}),
        )

    def _resolve_tag(self, text: str) -> dict[str, Any]:
        value = str(text or "").strip()
        platform_match = self.db.resolve_platform_term(self.PLATFORM, value)
        if platform_match.matched and platform_match.tag_name:
            return {
                "id": int(platform_match.tag_id or 0),
                "name": str(platform_match.tag_name),
            }
        direct_match = self.db.resolve_tag(value, allow_fuzzy=False)
        if direct_match.matched and direct_match.tag_name:
            return {
                "id": int(direct_match.tag_id or 0),
                "name": str(direct_match.tag_name),
            }
        raise ValueError(f"未找到已存在的主 tag：{value}")

    @staticmethod
    def _matches_target(detail: XhsNoteDetail, match_terms: list[str]) -> bool:
        topics = {normalize_tag_name(value) for value in detail.topics}
        normalized_values = [normalize_tag_name(detail.title), normalize_tag_name(detail.description)]
        for term in match_terms:
            normalized = normalize_tag_name(term)
            if normalized and (normalized in topics or any(normalized in value for value in normalized_values if value)):
                return True
        return False

    @staticmethod
    def _json_texts(value: str) -> list[str]:
        parsed = json.loads(value or "[]")
        return XhsBackfillService._unique_texts(
            [str(item) for item in parsed] if isinstance(parsed, list) else []
        )

    @staticmethod
    def _unique_texts(values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = str(value or "").strip()
            key = normalize_tag_name(text)
            if text and key and key not in seen:
                seen.add(key)
                result.append(text)
        return result

    @staticmethod
    def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        parsed = int(value if value is not None else default)
        return min(max(parsed, minimum), maximum)
