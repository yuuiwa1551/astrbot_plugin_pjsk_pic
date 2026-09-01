from __future__ import annotations

import asyncio
import inspect
import json
from difflib import SequenceMatcher
from typing import Awaitable, Callable, Iterable

from astrbot.api import logger

from .crawl_adapter import CrawlAdapterFactory
from .crawl_tag_rules import CrawlTagRules
from .db import ImageIndexDB
from .importer import ImportedImageService
from .matcher import normalize_tag_name
from .pixiv_app_api import PixivAppClient
from .pixiv_tag_terms import known_pixiv_query_terms
from .review_service import ReviewService
from .tag_cleaner import TagCleaner
from .xhs_provider import XhsProviderClient, XhsProviderError


class CrawlService:
    def __init__(
        self,
        *,
        db: ImageIndexDB,
        importer: ImportedImageService,
        reviewer: ReviewService,
        config,
        pixiv_client: PixivAppClient | None = None,
        xhs_provider_client: XhsProviderClient | None = None,
        llm_review_service=None,
    ) -> None:
        self.db = db
        self.importer = importer
        self.reviewer = reviewer
        self.config = config
        self.pixiv_client = pixiv_client
        self.xhs_provider_client = xhs_provider_client
        self.llm_review_service = llm_review_service
        self._xhs_pause_handler: Callable[[XhsProviderError], Awaitable[None] | None] | None = None
        self.tag_cleaner = TagCleaner(config)
        self._queue: asyncio.PriorityQueue[tuple[int, int]] = asyncio.PriorityQueue()
        self._queued_priorities: dict[int, int] = {}
        self._running_ids: set[int] = set()
        self._worker_tasks: list[asyncio.Task] = []
        self._stop_event = asyncio.Event()

    def worker_count(self) -> int:
        return min(max(1, int(self.config.get("crawl_worker_count", 2) or 2)), 4)

    def backfill_queue_high_watermark(self) -> int:
        return min(
            max(1, int(self.config.get("crawl_backfill_queue_high_watermark", 20) or 20)),
            500,
        )

    def image_download_concurrency(self) -> int:
        return min(
            max(1, int(self.config.get("crawl_image_download_concurrency", 3) or 3)),
            8,
        )

    def _keep_primary_tags_only(self) -> bool:
        return bool(self.config.get("crawl_keep_primary_tags_only", True))

    def set_xhs_pause_handler(
        self,
        handler: Callable[[XhsProviderError], Awaitable[None] | None] | None,
    ) -> None:
        self._xhs_pause_handler = handler

    def queue_size(self) -> int:
        return len(self._queued_priorities)

    def worker_running(self) -> bool:
        return bool(self._worker_tasks) and all(not task.done() for task in self._worker_tasks)

    async def start(self) -> None:
        self.db.reset_running_jobs()
        self._stop_event.clear()
        self._worker_tasks = [task for task in self._worker_tasks if not task.done()]
        for row in self.db.get_pending_jobs():
            await self._enqueue_job(
                int(row["id"]),
                priority=int(row["priority"]) if row["priority"] is not None else 20,
            )
        for worker_index in range(len(self._worker_tasks), self.worker_count()):
            self._worker_tasks.append(
                asyncio.create_task(
                    self._worker_loop(),
                    name=f"pjsk-pic-crawl-worker-{worker_index + 1}",
                )
            )

    async def stop(self) -> None:
        self._stop_event.set()
        for task in self._worker_tasks:
            task.cancel()
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks = []

    async def submit_job(
        self,
        platform: str,
        source_url: str,
        tags: list[str],
        *,
        source_context: dict | None = None,
        include_tags: list[str] | None = None,
        exclude_tags: list[str] | None = None,
        match_mode: str = "exact",
        origin: str = "manual",
        priority: int = 0,
    ) -> int:
        normalized_platform = CrawlAdapterFactory.normalize_platform(platform)
        if not CrawlAdapterFactory.supports(normalized_platform):
            raise ValueError(f"暂不支持的平台：{platform}")
        if self.db.is_rejected_source_post_url(source_url, platform=normalized_platform):
            raise ValueError(f"该来源已被人工拒绝，已跳过：{source_url}")
        job_id = self.db.create_crawl_job(
            normalized_platform,
            source_url,
            tags,
            source_context=source_context,
            include_tags=include_tags,
            exclude_tags=exclude_tags,
            match_mode=match_mode,
            origin=origin,
            priority=priority,
        )
        await self._enqueue_job(job_id, priority=priority)
        return job_id

    async def submit_job_once(
        self,
        platform: str,
        source_url: str,
        tags: list[str],
        *,
        source_context: dict | None = None,
        include_tags: list[str] | None = None,
        exclude_tags: list[str] | None = None,
        match_mode: str = "exact",
        origin: str = "auto_incremental",
        priority: int = 20,
    ) -> tuple[int, bool]:
        normalized_platform = CrawlAdapterFactory.normalize_platform(platform)
        if not CrawlAdapterFactory.supports(normalized_platform):
            raise ValueError(f"暂不支持的平台：{platform}")
        if self.db.is_rejected_source_post_url(source_url, platform=normalized_platform):
            raise ValueError(f"该来源已被人工拒绝，已跳过：{source_url}")
        job_id, created = self.db.get_or_create_crawl_job(
            normalized_platform,
            source_url,
            tags,
            source_context=source_context,
            include_tags=include_tags,
            exclude_tags=exclude_tags,
            match_mode=match_mode,
            origin=origin,
            priority=priority,
        )
        if created:
            await self._enqueue_job(job_id, priority=priority)
        else:
            existing = self.db.get_crawl_job(job_id)
            if existing and str(existing["status"] or "") in {"pending", "retry"}:
                await self._enqueue_job(
                    job_id,
                    priority=(
                        int(existing["priority"])
                        if existing["priority"] is not None
                        else priority
                    ),
                )
        return job_id, created

    async def retry_job(self, job_id: int) -> tuple[bool, str]:
        row = self.db.get_crawl_job(job_id)
        if not row:
            return False, f"采集任务不存在：{job_id}"
        try:
            source_context = json.loads(str(row["source_context_json"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            source_context = {}
        if not isinstance(source_context, dict):
            source_context = {}
        source_context.pop("detail_snapshot", None)
        self.db.update_crawl_job(
            job_id,
            status="retry",
            progress=0,
            error_log="",
            source_context=source_context,
        )
        await self._enqueue_job(
            job_id,
            priority=int(row["priority"]) if row["priority"] is not None else 20,
        )
        return True, f"已重新入队采集任务 #{job_id}"

    async def _enqueue_job(self, job_id: int, *, priority: int | None = None) -> None:
        if int(job_id) in self._running_ids:
            return
        if priority is None:
            row = self.db.get_crawl_job(job_id)
            priority = (
                int(row["priority"])
                if row is not None and row["priority"] is not None
                else 20
            )
        resolved_priority = int(priority)
        current_priority = self._queued_priorities.get(int(job_id))
        if current_priority is not None and current_priority <= resolved_priority:
            return
        self._queued_priorities[int(job_id)] = resolved_priority
        await self._queue.put((resolved_priority, int(job_id)))

    async def wait_for_backfill_capacity(self) -> None:
        high_watermark = self.backfill_queue_high_watermark()
        while not self._stop_event.is_set() and self.queue_size() >= high_watermark:
            await asyncio.sleep(0.25)

    async def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                priority, job_id = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if self._queued_priorities.get(job_id) != priority:
                self._queue.task_done()
                continue
            self._queued_priorities.pop(job_id, None)
            self._running_ids.add(job_id)
            try:
                await self._process_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[PJSKPic] 采集任务 #{job_id} 执行异常: {exc}", exc_info=True)
                self.db.update_crawl_job(job_id, status="failed", error_log=str(exc), progress=0)
            finally:
                self._running_ids.discard(job_id)
                self._queue.task_done()

    async def _process_job(self, job_id: int) -> None:
        row = self.db.get_crawl_job(job_id)
        if not row:
            return

        attempt_count = self.db.increment_crawl_job_attempt(job_id)
        platform = str(row["platform"])
        source_url = str(row["source_url"])
        try:
            source_context = json.loads(str(row["source_context_json"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            source_context = {}
        if not isinstance(source_context, dict):
            source_context = {}
        job_rules = CrawlTagRules.from_db_row(row)
        default_rules = (
            CrawlTagRules()
            if bool(source_context.get("filters_applied"))
            else CrawlTagRules.from_config(self.config)
        )
        manual_tags = job_rules.manual_tags
        include_tags = self._normalized_rule_tags(
            [*default_rules.include_tags, *job_rules.include_tags],
            platform=platform,
        )
        exclude_tags = self._normalized_rule_tags(
            [*default_rules.exclude_tags, *job_rules.exclude_tags],
            platform=platform,
        )
        match_mode = str(row["tag_match_mode"] or "exact").strip().lower() or "exact"
        adapter = CrawlAdapterFactory.create(
            platform,
            config=self.config,
            pixiv_client=self.pixiv_client,
            xhs_provider_client=self.xhs_provider_client,
        )
        max_candidates = max(1, int(self.config.get("crawler_max_candidates", 6) or 6))
        timeout_seconds = max(5, int(self.config.get("platform_request_timeout", self.config.get("crawler_timeout_seconds", 20)) or 20))
        self.db.update_crawl_job(job_id, status="running", progress=5, error_log="", result_summary="", attempt_count=attempt_count)
        candidates: list = []
        last_error = ""
        try:
            candidates = await adapter.fetch_candidates(
                source_url,
                max_candidates=max_candidates,
                timeout_seconds=timeout_seconds,
                source_context=source_context,
            )
        except Exception as exc:
            last_error = str(exc)
            if platform == "xiaohongshu" and isinstance(exc, XhsProviderError) and exc.pause_required:
                await self._handle_xhs_provider_pause(exc)
        if not candidates:
            self.db.update_crawl_job(job_id, status="failed", progress=0, error_log=last_error or "未解析到可下载图片")
            return

        imported_count = 0
        tag_links = 0
        pending_reviews = 0
        approved_links = 0
        rejected_links = 0
        skipped_without_tags = 0
        skipped_by_include = 0
        skipped_by_exclude = 0
        similar_hits = 0
        failed_candidates = 0
        candidate_errors: list[str] = []

        raw_tags: list[str] = []
        translated_tags: list[str] = []
        seen_raw: set[str] = set()
        seen_translated: set[str] = set()
        for candidate in candidates:
            for value in candidate.raw_tags:
                text = str(value or "").strip()
                key = normalize_tag_name(text)
                if text and key and key not in seen_raw:
                    seen_raw.add(key)
                    raw_tags.append(text)
            translated = candidate.extra.get("translated_tags") if isinstance(candidate.extra, dict) else []
            for value in translated if isinstance(translated, list) else []:
                text = str(value or "").strip()
                key = normalize_tag_name(text)
                if text and key and key not in seen_translated and key not in seen_raw:
                    seen_translated.add(key)
                    translated_tags.append(text)

        candidate_tags = self.tag_cleaner.normalize_tags(
            [*raw_tags, *translated_tags],
            drop_noise=False,
        )
        filter_reason = self._match_filter_reason(
            candidate_tags,
            include_tags=include_tags,
            exclude_tags=exclude_tags,
            match_mode=match_mode,
        )
        if filter_reason == "exclude":
            skipped_by_exclude = len(candidates)
            candidates = []
        elif filter_reason == "include":
            skipped_by_include = len(candidates)
            candidates = []

        resolved_tags: list[str] = []
        if candidates:
            if platform == "xiaohongshu" or self._keep_primary_tags_only():
                resolved_tags = self._canonicalize_primary_tags(
                    manual_tags=manual_tags,
                    include_tags=include_tags,
                    raw_tags=[*raw_tags, *translated_tags],
                    platform=platform,
                )
            else:
                resolved_tags = self.tag_cleaner.clean_tags(
                    self._merge_tags(manual_tags, [*raw_tags, *translated_tags]),
                    platform=platform,
                )
                resolved_tags = self._collapse_similar_tags(
                    resolved_tags,
                    preferred_tags=[*manual_tags, *include_tags],
                )

        tag_entries: list[tuple[str, int, bool]] = []
        if candidates:
            for tag_name in resolved_tags[: max(1, int(self.config.get("max_tags_per_image", 12) or 12))]:
                tag_id = self.db.get_or_create_tag(tag_name)
                tag_entries.append((tag_name, tag_id, self.reviewer.is_character_tag(tag_name)))
            if not tag_entries:
                skipped_without_tags = len(candidates)
                candidates = []

        import_results = await self.importer.import_candidates(
            candidates,
            concurrency=self.image_download_concurrency(),
        )
        for index, (candidate, imported_result) in enumerate(
            zip(candidates, import_results, strict=True),
            start=1,
        ):
            try:
                if isinstance(imported_result, Exception):
                    raise imported_result
                imported = imported_result
                tag_reviews: list[dict] = []
                for tag_name, tag_id, is_character in tag_entries:
                    decision = await self.reviewer.review_image_for_tag(
                        imported.file_path,
                        tag_name,
                        is_character=is_character,
                    )
                    tag_links += 1
                    if decision.status in {"pending", "uncertain", "rejected"}:
                        pending_reviews += 1
                    if decision.status in {"approved", "manual_approved"}:
                        approved_links += 1
                    if decision.status in {"rejected", "manual_rejected"}:
                        rejected_links += 1
                    tag_reviews.append(
                        {
                            "tag_id": tag_id,
                            "source_type": f"crawl:{platform}",
                            "status": decision.status,
                            "score": decision.confidence,
                            "reason": decision.reason,
                            "model_result": decision.raw_result,
                            "create_review_task": (
                                decision.status in {"pending", "uncertain", "rejected"}
                                or is_character
                            ),
                        }
                    )
                self.db.commit_crawl_image(
                    image_id=imported.image_id,
                    platform=platform,
                    post_url=candidate.normalized_post_url or candidate.post_url,
                    image_url=candidate.image_url,
                    author=candidate.author,
                    raw_tags=candidate.raw_tags,
                    extra_json={
                        "title": candidate.title,
                        "source_uid": candidate.source_uid,
                        "similar_image_ids": imported.similar_image_ids,
                        **(candidate.extra or {}),
                    },
                    tag_reviews=tag_reviews,
                )
                imported_count += 1
                if imported.similar_image_ids:
                    similar_hits += 1
                if self.llm_review_service is not None:
                    try:
                        self.llm_review_service.queue_image(
                            imported.image_id,
                            platform=platform,
                        )
                    except Exception as exc:
                        logger.warning(
                            f"[PJSKPic] 图片 #{imported.image_id} 加入 LLM 审核队列失败：{exc}",
                            exc_info=True,
                        )
            except Exception as exc:
                failed_candidates += 1
                message = f"候选图 #{index} 处理失败: {exc}"
                if len(candidate_errors) < 3:
                    candidate_errors.append(message)
                logger.warning(f"[PJSKPic] 采集任务 #{job_id} {message}", exc_info=True)
                continue

        if imported_count == 0 and failed_candidates > 0:
            self.db.update_crawl_job(
                job_id,
                status="failed",
                progress=0,
                error_log="；".join(candidate_errors) or "候选图片处理失败",
                result_summary=f"候选图 {len(candidates)} 张，全部处理失败",
            )
            return

        summary = (
            f"图片 {imported_count} 张，标签关联 {tag_links} 条，"
            f"通过 {approved_links}，待复核 {pending_reviews}，拒绝 {rejected_links}"
        )
        if skipped_without_tags:
            summary += f"，无 tag 图片 {skipped_without_tags}"
        if skipped_by_include:
            summary += f"，include 跳过 {skipped_by_include}"
        if skipped_by_exclude:
            summary += f"，exclude 跳过 {skipped_by_exclude}"
        if similar_hits:
            summary += f"，疑似重复 {similar_hits}"
        if failed_candidates:
            summary += f"，失败 {failed_candidates}"
        self.db.update_crawl_job(
            job_id,
            status="completed",
            progress=100,
            result_summary=summary,
            error_log="；".join(candidate_errors) if candidate_errors else "",
            clear_source_context=True,
        )

    async def _handle_xhs_provider_pause(self, error: XhsProviderError) -> None:
        handler = self._xhs_pause_handler
        if handler is None:
            self.db.set_crawl_provider_state(
                "xiaohongshu",
                status="paused",
                category=error.category,
                reason=str(error),
            )
            return
        try:
            result = handler(error)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning(f"[PJSKPic] 小红书暂停通知失败: {exc}", exc_info=True)

    @staticmethod
    def _merge_tags(manual_tags: Iterable[str], raw_tags: Iterable[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for source in (manual_tags, raw_tags):
            for item in source:
                tag = str(item or "").strip()
                key = tag.lower()
                if not tag or key in seen:
                    continue
                seen.add(key)
                result.append(tag)
        return result

    def _normalized_rule_tags(self, tags: Iterable[str], *, platform: str = "") -> set[str]:
        normalized = self.tag_cleaner.normalize_tags(list(tags), drop_noise=False)
        result: set[str] = set()

        def add(value: str) -> None:
            key = normalize_tag_name(value)
            if key:
                result.add(key)

        for tag in normalized:
            add(tag)
            if CrawlAdapterFactory.normalize_platform(platform) == "pixiv":
                for term in known_pixiv_query_terms(tag):
                    add(term)
            canonical = self._canonicalize_explicit_tag(tag, platform=platform)
            if not canonical:
                continue
            add(canonical)
            if CrawlAdapterFactory.normalize_platform(platform) == "pixiv":
                for term in known_pixiv_query_terms(canonical):
                    add(term)
            for term in self.db.get_platform_terms_for_tag(
                tag_name=canonical,
                platform=platform or "pixiv",
                purpose="match",
                include_aliases=True,
                include_primary=True,
            ):
                add(term)
        return result

    def resolve_filter_sets(
        self,
        *,
        platform: str,
        include_tags: Iterable[str] = (),
        exclude_tags: Iterable[str] = (),
        include_defaults: bool = True,
    ) -> tuple[set[str], set[str]]:
        defaults = CrawlTagRules.from_config(self.config) if include_defaults else CrawlTagRules()
        resolved_include = self._normalized_rule_tags(
            [*defaults.include_tags, *list(include_tags)],
            platform=platform,
        )
        resolved_exclude = self._normalized_rule_tags(
            [*defaults.exclude_tags, *list(exclude_tags)],
            platform=platform,
        )
        return resolved_include, resolved_exclude

    @classmethod
    def filter_reason_for_tags(
        cls,
        candidate_tags: Iterable[str],
        *,
        include_tags: set[str],
        exclude_tags: set[str],
        match_mode: str = "exact",
    ) -> str | None:
        return cls._match_filter_reason(
            candidate_tags,
            include_tags=include_tags,
            exclude_tags=exclude_tags,
            match_mode=match_mode,
        )

    def _canonicalize_primary_tags(
        self,
        *,
        manual_tags: Iterable[str],
        include_tags: Iterable[str],
        raw_tags: Iterable[str],
        platform: str = "",
    ) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()

        def append_if_missing(tag_name: str) -> None:
            normalized = normalize_tag_name(tag_name)
            if not normalized or normalized in seen:
                return
            seen.add(normalized)
            result.append(tag_name)

        for tag in self.tag_cleaner.normalize_tags(list(manual_tags), drop_noise=False):
            canonical = self._canonicalize_explicit_tag(tag, platform=platform)
            if canonical:
                append_if_missing(canonical)

        for tag in self.tag_cleaner.normalize_tags(list(include_tags), drop_noise=False):
            canonical = self._canonicalize_existing_character_tag(tag, platform=platform)
            if canonical:
                append_if_missing(canonical)

        for tag in self.tag_cleaner.normalize_tags(list(raw_tags), drop_noise=False):
            canonical = self._canonicalize_existing_character_tag(tag, platform=platform)
            if canonical:
                append_if_missing(canonical)

        return result

    def _canonicalize_explicit_tag(self, tag_name: str, *, platform: str = "") -> str | None:
        match = self.db.resolve_tag(tag_name, allow_fuzzy=False)
        if match.matched and match.tag_name:
            return str(match.tag_name)
        if platform:
            platform_match = self.db.resolve_platform_term(platform, tag_name)
            if platform_match.matched and platform_match.tag_name:
                return str(platform_match.tag_name)
        normalized = self.tag_cleaner.normalize_tags([tag_name], drop_noise=False)
        return normalized[0] if normalized else None

    def _canonicalize_existing_character_tag(self, tag_name: str, *, platform: str = "") -> str | None:
        match = self.db.resolve_tag(tag_name, allow_fuzzy=False)
        if (not match.matched or not match.tag_name) and platform:
            match = self.db.resolve_platform_term(platform, tag_name)
        if not match.matched or not match.tag_name:
            return None
        row = self.db.get_tag_row(str(match.tag_name))
        if (
            not row
            or str(row["tag_type"] or "other") != "character"
            or str(row["status"] or "active") != "active"
        ):
            return None
        return str(match.tag_name)

    @classmethod
    def _collapse_similar_tags(cls, tags: list[str], *, preferred_tags: Iterable[str]) -> list[str]:
        if not tags:
            return []

        normalized_preferred: list[str] = []
        seen_preferred: set[str] = set()
        for tag in preferred_tags:
            normalized = normalize_tag_name(str(tag))
            if not normalized or normalized in seen_preferred:
                continue
            seen_preferred.add(normalized)
            normalized_preferred.append(normalized)
        if not normalized_preferred:
            return tags

        consumed_indexes: set[int] = set()
        chosen_indexes: set[int] = set()
        for target in normalized_preferred:
            matches: list[tuple[float, int]] = []
            for index, tag in enumerate(tags):
                score = cls._tag_similarity_score(tag, target)
                if score < 0.72:
                    continue
                matches.append((score, index))
            if len(matches) <= 1:
                continue
            matches.sort(key=lambda item: (-item[0], item[1]))
            winner = matches[0][1]
            chosen_indexes.add(winner)
            for _, index in matches:
                consumed_indexes.add(index)

        if not consumed_indexes:
            return tags

        result: list[str] = []
        for index, tag in enumerate(tags):
            if index in consumed_indexes and index not in chosen_indexes:
                continue
            result.append(tag)
        return result

    @staticmethod
    def _tag_similarity_score(left: str, right: str) -> float:
        normalized_left = normalize_tag_name(str(left))
        normalized_right = normalize_tag_name(str(right))
        if not normalized_left or not normalized_right:
            return 0.0
        if normalized_left == normalized_right:
            return 1.0
        shorter = min(len(normalized_left), len(normalized_right))
        longer = max(len(normalized_left), len(normalized_right))
        if normalized_left in normalized_right or normalized_right in normalized_left:
            return 0.88 + (shorter / max(1, longer)) * 0.12
        return SequenceMatcher(None, normalized_left, normalized_right).ratio()

    @staticmethod
    def _match_filter_reason(
        candidate_tags: Iterable[str],
        *,
        include_tags: set[str],
        exclude_tags: set[str],
        match_mode: str = "exact",
    ) -> str | None:
        candidate_set = {normalize_tag_name(str(tag)) for tag in candidate_tags if str(tag).strip()}
        candidate_set.discard("")
        if exclude_tags and CrawlService._rule_set_matches(candidate_set, exclude_tags, match_mode=match_mode):
            return "exclude"
        if include_tags and not CrawlService._rule_set_matches(candidate_set, include_tags, match_mode=match_mode):
            return "include"
        return None

    @staticmethod
    def _rule_set_matches(candidate_set: set[str], rule_tags: set[str], *, match_mode: str = "exact") -> bool:
        if not candidate_set or not rule_tags:
            return False
        if match_mode != "partial":
            return bool(candidate_set.intersection(rule_tags))
        for candidate in candidate_set:
            for rule in rule_tags:
                if not candidate or not rule:
                    continue
                if candidate == rule or candidate in rule or rule in candidate:
                    return True
        return False
