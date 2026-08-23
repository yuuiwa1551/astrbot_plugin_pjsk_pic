from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta

from astrbot.api import logger

from .db import utcnow_str
from .matcher import normalize_tag_name
from .pixiv_app_api import PixivAppClient
from .pixiv_search_service import PixivSearchHit, PixivSearchService
from .pixiv_tag_terms import known_pixiv_query_terms


PIXIV_SUFFIX_RE = re.compile(
    r"(?:\d+(?:users(?:入り|はいり)?|bookmarks?)|users入り|usersはいり)$",
    re.IGNORECASE,
)


def _pixiv_term_variants(value: str) -> set[str]:
    normalized = normalize_tag_name(value)
    if not normalized:
        return set()
    variants = {normalized}
    stripped = PIXIV_SUFFIX_RE.sub("", normalized).strip()
    if stripped and stripped != normalized:
        variants.add(stripped)
    return variants


class AutoCrawlService:
    def __init__(self, *, db, crawl_service, config, pixiv_client: PixivAppClient | None = None) -> None:
        self.db = db
        self.crawl_service = crawl_service
        self.config = config
        self.search_service = PixivSearchService(config, pixiv_client=pixiv_client)
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._run_lock = asyncio.Lock()

    def enabled(self) -> bool:
        return bool(self.config.get("pixiv_auto_crawl_enabled", False))

    def has_refresh_token(self) -> bool:
        return bool(self.search_service.refresh_token())

    def character_only(self) -> bool:
        return bool(self.config.get("pixiv_auto_crawl_character_only", True))

    def interval_minutes(self) -> int:
        return max(5, int(self.config.get("pixiv_auto_crawl_interval_minutes", 60) or 60))

    def max_results_per_tag(self) -> int:
        return max(1, int(self.config.get("pixiv_auto_crawl_max_results_per_tag", 30) or 30))

    def max_pages_per_tag(self) -> int:
        return max(1, int(self.config.get("pixiv_auto_crawl_max_pages_per_tag", 3) or 3))

    def max_new_jobs_per_cycle(self) -> int:
        return max(1, int(self.config.get("pixiv_auto_crawl_max_new_jobs_per_cycle", 30) or 30))

    def timeout_seconds(self) -> int:
        return max(
            5,
            int(
                self.config.get(
                    "platform_request_timeout",
                    self.config.get("crawler_timeout_seconds", 20),
                )
                or 20
            ),
        )

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        self._stop_event.clear()
        if not self.enabled():
            logger.info("[PJSKPic] Pixiv 自动采集未启用")
            return
        if not self.has_refresh_token():
            logger.warning("[PJSKPic] Pixiv 自动采集缺少 refresh token")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="pjsk-pic-auto-crawl")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run_once(self, *, force: bool = False) -> dict[str, int]:
        async with self._run_lock:
            summary = {
                "subscriptions": 0,
                "checked": 0,
                "discovered": 0,
                "queued": 0,
                "matched": 0,
                "skipped_existing": 0,
                "skipped_rejected": 0,
                "skipped_filtered": 0,
                "errors": 0,
            }
            if not self.enabled() or not self.has_refresh_token():
                return summary

            self._sync_subscriptions()
            subscriptions = self.db.list_crawl_subscriptions(platform="pixiv", enabled_only=True)
            summary["subscriptions"] = len(subscriptions)

            for row in subscriptions:
                if not force and not self._is_due(row):
                    continue
                summary["checked"] += 1
                try:
                    result = await self._process_subscription(row)
                except Exception as exc:
                    summary["errors"] += 1
                    self.db.update_crawl_subscription_state(
                        int(row["id"]),
                        last_error=str(exc),
                    )
                    logger.warning(
                        f"[PJSKPic] Pixiv 自动订阅 #{row['id']} 执行失败: {exc}",
                        exc_info=True,
                    )
                    continue
                for key in (
                    "discovered",
                    "matched",
                    "skipped_existing",
                    "skipped_rejected",
                    "skipped_filtered",
                    "errors",
                ):
                    summary[key] += int(result.get(key, 0) or 0)

            drained = await self._drain_discoveries(max_new_jobs=self.max_new_jobs_per_cycle())
            summary["queued"] += drained["queued"]
            summary["skipped_existing"] += drained["reused"] + drained["resolved_existing"]
            summary["skipped_rejected"] += drained["resolved_rejected"]
            summary["errors"] += drained["errors"]
            return summary

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[PJSKPic] Pixiv 自动采集循环失败: {exc}", exc_info=True)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_minutes() * 60)
            except asyncio.TimeoutError:
                continue

    def _sync_subscriptions(self) -> None:
        tags = self.db.list_tags_for_auto_crawl(character_only=self.character_only())
        enabled_normalized: set[str] = set()
        for row in tags:
            tag_name = str(row["name"])
            tag_id = int(row["id"])
            query_terms = self._query_terms_for_tag(tag_name)[:5]
            primary_query_term = query_terms[0] if query_terms else ""
            query_text = self.search_service.build_query(primary_query_term) if primary_query_term else ""
            subscription_id = self.db.upsert_crawl_subscription(
                platform="pixiv",
                tag_id=tag_id,
                tag_name=tag_name,
                query_text=query_text,
                enabled=bool(query_terms),
            )
            self.db.sync_crawl_subscription_terms(
                subscription_id,
                [
                    (query_term, self.search_service.build_query(query_term))
                    for query_term in query_terms
                ],
            )
            if query_terms:
                enabled_normalized.add(normalize_tag_name(tag_name))
        self.db.disable_missing_crawl_subscriptions(
            platform="pixiv",
            keep_normalized_tags=enabled_normalized,
        )

    def _is_due(self, row) -> bool:
        last_checked = str(row["last_checked_at"] or "").strip()
        if not last_checked:
            return True
        try:
            last_dt = datetime.fromisoformat(last_checked)
        except ValueError:
            return True
        current = datetime.now(last_dt.tzinfo) if last_dt.tzinfo is not None else datetime.utcnow()
        return current - last_dt >= timedelta(minutes=self.interval_minutes())

    async def _process_subscription(self, row) -> dict[str, int]:
        result = {
            "discovered": 0,
            "matched": 0,
            "skipped_existing": 0,
            "skipped_rejected": 0,
            "skipped_filtered": 0,
            "errors": 0,
        }
        subscription_id = int(row["id"])
        tag_name = str(row["tag_name"] or "").strip()
        if not tag_name:
            return result

        term_rows = self.db.list_crawl_subscription_terms(subscription_id)
        if not term_rows:
            self.db.update_crawl_subscription_state(
                subscription_id,
                last_checked_at=utcnow_str(),
                last_error="未配置可靠 Pixiv 搜索词，已跳过自动采集",
                query_text="",
            )
            result["errors"] = 1
            return result

        for term_row in term_rows:
            term_id = int(term_row["id"])
            query_term = str(term_row["query_term"] or "").strip()
            last_seen_source_uid = str(term_row["last_seen_source_uid"] or "").strip()
            checked_at = utcnow_str()
            try:
                hits = await self.search_service.search_tag(
                    query_term,
                    max_results=self.max_results_per_tag(),
                    max_pages=self.max_pages_per_tag(),
                    timeout_seconds=self.timeout_seconds(),
                )
                for hit in hits:
                    if last_seen_source_uid and hit.illust_id == last_seen_source_uid:
                        break
                    if not self._matches_target_tag(tag_name, hit):
                        result["skipped_filtered"] += 1
                        continue
                    result["matched"] += 1
                    if self.db.is_rejected_source_post_url(hit.post_url, platform="pixiv"):
                        result["skipped_rejected"] += 1
                        continue
                    if self.db.has_source_post_url(hit.post_url, platform="pixiv"):
                        result["skipped_existing"] += 1
                        continue
                    _, created = self.db.upsert_crawl_discovery(
                        platform="pixiv",
                        source_uid=hit.illust_id,
                        post_url=hit.post_url,
                        tags=[tag_name],
                    )
                    if created:
                        result["discovered"] += 1
                    else:
                        result["skipped_existing"] += 1
            except Exception as exc:
                result["errors"] += 1
                self.db.update_crawl_subscription_term_state(
                    term_id,
                    last_checked_at=checked_at,
                    last_error=str(exc),
                )
                logger.warning(
                    f"[PJSKPic] Pixiv 自动订阅 #{subscription_id} 查询词 {query_term!r} 失败: {exc}",
                    exc_info=True,
                )
                continue

            newest_source_uid = hits[0].illust_id if hits else last_seen_source_uid
            self.db.update_crawl_subscription_term_state(
                term_id,
                last_seen_source_uid=newest_source_uid,
                last_checked_at=checked_at,
                last_success_at=checked_at,
                last_error="",
            )

        self.db.refresh_crawl_subscription_state(subscription_id)
        return result

    async def _drain_discoveries(self, *, max_new_jobs: int) -> dict[str, int]:
        summary = {
            "queued": 0,
            "reused": 0,
            "resolved_existing": 0,
            "resolved_rejected": 0,
            "errors": 0,
        }
        max_jobs = max(1, int(max_new_jobs or 1))
        rows = self.db.list_pending_crawl_discoveries(
            platform="pixiv",
            limit=max(100, max_jobs * 5),
        )
        for row in rows:
            if summary["queued"] >= max_jobs:
                break
            discovery_id = int(row["id"])
            post_url = str(row["post_url"] or "").strip()
            tags = [
                item.strip()
                for item in str(row["tags_text"] or "").replace("，", ",").split(",")
                if item.strip()
            ]
            try:
                if self.db.is_rejected_source_post_url(post_url, platform="pixiv"):
                    self.db.mark_crawl_discovery_resolved(discovery_id, status="rejected")
                    summary["resolved_rejected"] += 1
                    continue
                if self.db.has_source_post_url(post_url, platform="pixiv"):
                    self.db.mark_crawl_discovery_resolved(discovery_id, status="imported")
                    summary["resolved_existing"] += 1
                    continue
                job_id, created = await self.crawl_service.submit_job_once(
                    "pixiv",
                    post_url,
                    tags,
                    include_tags=[],
                    exclude_tags=[],
                    match_mode="exact",
                )
                self.db.mark_crawl_discovery_submitted(discovery_id, job_id)
                if created:
                    summary["queued"] += 1
                else:
                    summary["reused"] += 1
            except Exception as exc:
                summary["errors"] += 1
                self.db.mark_crawl_discovery_error(discovery_id, str(exc))
                logger.warning(
                    f"[PJSKPic] Pixiv 发现记录 #{discovery_id} 提交失败: {exc}",
                    exc_info=True,
                )
        return summary

    def _query_terms_for_tag(self, tag_name: str) -> list[str]:
        terms: list[str] = []
        seen: set[str] = set()

        def append(value: str) -> None:
            text = str(value or "").strip()
            normalized = normalize_tag_name(text)
            if not text or not normalized or normalized in seen:
                return
            seen.add(normalized)
            terms.append(text)

        for term in known_pixiv_query_terms(tag_name):
            append(term)
        for term in self.db.get_platform_terms_for_tag(
            tag_name=tag_name,
            platform="pixiv",
            purpose="query",
            include_aliases=False,
            include_primary=False,
        ):
            append(term)
        return terms

    def _matches_target_tag(self, tag_name: str, hit: PixivSearchHit) -> bool:
        target = normalize_tag_name(tag_name)
        if not target:
            return False
        target_terms = [
            *known_pixiv_query_terms(tag_name),
            *self.db.get_platform_terms_for_tag(
                tag_name=tag_name,
                platform="pixiv",
                purpose="match",
                include_aliases=False,
                include_primary=True,
            ),
        ]
        normalized_target_terms: set[str] = set()
        for term in target_terms or [tag_name]:
            normalized_target_terms.update(_pixiv_term_variants(term))
        candidates = [*(hit.raw_tags or []), *(hit.translated_tags or [])]
        seen: set[str] = set()
        for tag in candidates:
            variants = _pixiv_term_variants(tag)
            if not variants or variants.issubset(seen):
                continue
            seen.update(variants)
            platform_match = self.db.resolve_platform_term("pixiv", tag)
            if platform_match.matched and normalize_tag_name(platform_match.tag_name or "") == target:
                return True
            if variants & normalized_target_terms:
                return True
        return False
