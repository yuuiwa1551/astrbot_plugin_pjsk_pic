from __future__ import annotations

import asyncio
import json
from typing import Any

from astrbot.api import logger

from .db import utcnow_str
from .matcher import normalize_tag_name
from .pixiv_search_service import PixivSearchHit, PixivSearchService
from .pixiv_tag_terms import known_pixiv_query_terms


class PixivBackfillService:
    def __init__(self, *, db, crawl_service, config) -> None:
        self.db = db
        self.crawl_service = crawl_service
        self.config = config
        self.search_service = PixivSearchService(config)
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._queued_ids: set[int] = set()
        self._worker_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    def queue_size(self) -> int:
        return int(self._queue.qsize())

    def worker_running(self) -> bool:
        return self._worker_task is not None and not self._worker_task.done()

    async def start(self) -> None:
        self.db.reset_running_pixiv_backfill_tasks()
        self._stop_event.clear()
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop(), name="pjsk-pic-pixiv-backfill")
        for task_id in self.db.get_pending_pixiv_backfill_task_ids():
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
        max_pages: int = 20,
        max_results: int = 200,
        max_new_jobs: int = 100,
        include_tags: list[str] | None = None,
        exclude_tags: list[str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        text = str(tag_text or "").strip()
        if not text:
            raise ValueError("请先填写要回填的 tag。")
        if not self.search_service.refresh_token():
            raise ValueError("未配置 Pixiv refresh token，无法执行历史回填。")

        resolved = self._resolve_tag(text)
        canonical_name = str(resolved.get("name") or text).strip()
        query_terms = self._query_terms_for_input(text, canonical_name)
        if not query_terms:
            query_terms = [text]

        task_id = self.db.create_pixiv_backfill_task(
            tag_id=int(resolved.get("id", 0) or 0),
            tag_name=canonical_name,
            tag_text=text,
            query_terms=query_terms[:5],
            include_tags=self._unique_texts(include_tags or []),
            exclude_tags=self._unique_texts(exclude_tags or []),
            max_pages=self._bounded_int(max_pages, 20, 1, 100),
            max_results=self._bounded_int(max_results, 200, 1, 2000),
            max_new_jobs=self._bounded_int(max_new_jobs, 100, 1, 500),
        )
        await self._enqueue(task_id)
        return task_id, {
            "resolved_tag": resolved or {"id": 0, "name": canonical_name, "match_type": ""},
            "query_terms": query_terms[:5],
        }

    async def retry_task(self, task_id: int) -> tuple[bool, str]:
        row = self.db.get_pixiv_backfill_task(int(task_id))
        if not row:
            return False, f"历史回填任务不存在：{task_id}"
        self.db.update_pixiv_backfill_task(
            int(task_id),
            status="retry",
            current_query_text="",
            current_page=0,
            current_offset="",
            scanned=0,
            matched=0,
            queued=0,
            skipped_existing=0,
            skipped_rejected=0,
            skipped_filtered=0,
            skipped_duplicate=0,
            error_log="",
            completed_at="",
        )
        await self._enqueue(int(task_id))
        return True, f"已重新入队 Pixiv 历史回填任务 #{task_id}"

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

            self._queued_ids.discard(task_id)
            try:
                await self._process_task(task_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[PJSKPic] Pixiv 历史回填任务 #{task_id} 执行异常: {exc}", exc_info=True)
                self.db.update_pixiv_backfill_task(task_id, status="failed", error_log=str(exc))
            finally:
                self._queue.task_done()

    async def _process_task(self, task_id: int) -> None:
        row = self.db.get_pixiv_backfill_task(task_id)
        if not row:
            return

        tag_name = str(row["tag_name"] or "").strip()
        query_terms = self._query_terms_from_row(row)
        include_tags = self._unique_texts(
            [tag_name, *query_terms, *self._csv_texts(str(row["include_tags_text"] or ""))]
        )
        exclude_tags = self._csv_texts(str(row["exclude_tags_text"] or ""))
        max_pages = self._bounded_int(row["max_pages"], 20, 1, 100)
        max_results = self._bounded_int(row["max_results"], 200, 1, 2000)
        max_new_jobs = self._bounded_int(row["max_new_jobs"], 100, 1, 500)
        timeout_seconds = self._bounded_int(
            self.config.get("platform_request_timeout", self.config.get("crawler_timeout_seconds", 20)),
            20,
            5,
            120,
        )

        stats = {
            "scanned": 0,
            "matched": 0,
            "queued": 0,
            "skipped_existing": 0,
            "skipped_rejected": 0,
            "skipped_filtered": 0,
            "skipped_duplicate": 0,
        }
        seen_ids: set[str] = set()
        self.db.update_pixiv_backfill_task(
            task_id,
            status="running",
            error_log="",
            completed_at="",
            current_query_text="",
            current_page=0,
            current_offset="",
            **stats,
        )

        for query_term in query_terms:
            offset: int | None = None
            for page_index in range(1, max_pages + 1):
                self.db.update_pixiv_backfill_task(
                    task_id,
                    current_query_text=query_term,
                    current_page=page_index,
                    current_offset="" if offset is None else str(offset),
                    **stats,
                )
                page = await self.search_service.search_tag_page(
                    query_term,
                    offset=offset,
                    timeout_seconds=timeout_seconds,
                )
                if not page.hits:
                    break
                for hit in page.hits:
                    if stats["scanned"] >= max_results or stats["queued"] >= max_new_jobs:
                        break
                    stats["scanned"] += 1
                    if hit.illust_id in seen_ids:
                        stats["skipped_duplicate"] += 1
                        continue
                    seen_ids.add(hit.illust_id)
                    if not self._matches_target_tag(tag_name, hit):
                        stats["skipped_filtered"] += 1
                        continue
                    stats["matched"] += 1
                    if self.db.is_rejected_source_post_url(hit.post_url, platform="pixiv"):
                        stats["skipped_rejected"] += 1
                        continue
                    if self.db.has_source_post_url(hit.post_url, platform="pixiv") or self.db.has_crawl_job_source_url(
                        hit.post_url,
                        platform="pixiv",
                    ):
                        stats["skipped_existing"] += 1
                        continue
                    await self.crawl_service.submit_job(
                        "pixiv",
                        hit.post_url,
                        [tag_name],
                        include_tags=include_tags,
                        exclude_tags=exclude_tags,
                        match_mode="partial",
                    )
                    stats["queued"] += 1
                self.db.update_pixiv_backfill_task(task_id, **stats)
                if stats["scanned"] >= max_results or stats["queued"] >= max_new_jobs:
                    break
                offset = page.next_offset
                if offset is None:
                    break
            if stats["scanned"] >= max_results or stats["queued"] >= max_new_jobs:
                break

        self.db.update_pixiv_backfill_task(
            task_id,
            status="completed",
            completed_at=utcnow_str(),
            **stats,
        )

    def _matches_target_tag(self, tag_name: str, hit: PixivSearchHit) -> bool:
        target = normalize_tag_name(tag_name)
        if not target:
            return False
        target_terms = self.db.get_platform_terms_for_tag(
            tag_name=tag_name,
            platform="pixiv",
            purpose="match",
        ) or [tag_name]
        normalized_target_terms = {
            normalize_tag_name(term)
            for term in target_terms
            if normalize_tag_name(term)
        }
        candidates = [*(hit.raw_tags or []), *(hit.translated_tags or [])]
        seen: set[str] = set()
        for tag in candidates:
            normalized = normalize_tag_name(tag)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            platform_match = self.db.resolve_platform_term("pixiv", tag)
            if platform_match.matched and normalize_tag_name(platform_match.tag_name or "") == target:
                return True
            direct_match = self.db.resolve_tag(tag, allow_fuzzy=False)
            if direct_match.matched and normalize_tag_name(direct_match.tag_name or "") == target:
                return True
            if normalized in normalized_target_terms:
                return True
            if any(normalized in candidate or candidate in normalized for candidate in normalized_target_terms):
                return True
        return False

    def _resolve_tag(self, text: str) -> dict[str, Any]:
        platform_match = self.db.resolve_platform_term("pixiv", text)
        if platform_match.matched and platform_match.tag_name:
            return {
                "id": int(platform_match.tag_id or 0),
                "name": str(platform_match.tag_name),
                "match_type": str(platform_match.match_type or "platform:pixiv"),
            }
        direct_match = self.db.resolve_tag(text, allow_fuzzy=True, candidate_limit=5)
        if direct_match.matched and direct_match.tag_name:
            return {
                "id": int(direct_match.tag_id or 0),
                "name": str(direct_match.tag_name),
                "match_type": str(direct_match.match_type or ""),
            }
        context = self.db.build_pixiv_review_search_context(text, platform="pixiv")
        matched_tags = context.get("matched_tags") if isinstance(context, dict) else []
        if isinstance(matched_tags, list) and matched_tags:
            first = matched_tags[0]
            if isinstance(first, dict) and first.get("name"):
                return {
                    "id": int(first.get("id", 0) or 0),
                    "name": str(first.get("name", "")),
                    "match_type": str(first.get("match_type", "")),
                }
        return {"id": 0, "name": text, "match_type": "raw"}

    def _query_terms_for_input(self, raw_text: str, canonical_name: str) -> list[str]:
        known_terms = known_pixiv_query_terms(raw_text, canonical_name)
        db_terms = self.db.get_pixiv_query_terms_for_tag(canonical_name) or [canonical_name]
        return self._unique_texts([*known_terms, *db_terms])

    @staticmethod
    def _known_pixiv_query_terms(*values: str) -> list[str]:
        return known_pixiv_query_terms(*values)

    @staticmethod
    def _query_terms_from_row(row) -> list[str]:
        try:
            raw = json.loads(str(row["query_terms_json"] or "[]"))
        except json.JSONDecodeError:
            raw = []
        if not isinstance(raw, list):
            raw = []
        return PixivBackfillService._unique_texts([str(item) for item in raw]) or [str(row["tag_name"] or "")]

    @staticmethod
    def _csv_texts(value: str) -> list[str]:
        return [item.strip() for item in str(value or "").replace("，", ",").split(",") if item.strip()]

    @staticmethod
    def _unique_texts(values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in values:
            text = str(raw or "").strip()
            key = normalize_tag_name(text)
            if not text or not key or key in seen:
                continue
            seen.add(key)
            result.append(text)
        return result

    @staticmethod
    def _bounded_int(value: Any, default: int, min_value: int, max_value: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return min(max(parsed, min_value), max_value)
