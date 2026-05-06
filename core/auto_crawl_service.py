from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from astrbot.api import logger

from .matcher import normalize_tag_name
from .pixiv_search_service import PixivSearchHit, PixivSearchService


class AutoCrawlService:
    def __init__(self, *, db, crawl_service, config) -> None:
        self.db = db
        self.crawl_service = crawl_service
        self.config = config
        self.search_service = PixivSearchService(config)
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
        return max(5, int(self.config.get("platform_request_timeout", self.config.get("crawler_timeout_seconds", 20)) or 20))

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        self._stop_event.clear()
        if not self.enabled():
            logger.info("[PJSKPic] Pixiv ???????")
            return
        if not self.has_refresh_token():
            logger.warning("[PJSKPic] Pixiv ?????????? refresh token")
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
            remaining_jobs = self.max_new_jobs_per_cycle()

            for row in subscriptions:
                if remaining_jobs <= 0:
                    break
                if not force and not self._is_due(row):
                    continue
                summary["checked"] += 1
                try:
                    result = await self._process_subscription(row, remaining_jobs=remaining_jobs)
                except Exception as exc:
                    summary["errors"] += 1
                    self.db.update_crawl_subscription_state(
                        int(row["id"]),
                        last_error=str(exc),
                    )
                    logger.warning(f"[PJSKPic] Pixiv ?????? #{row['id']} ????: {exc}", exc_info=True)
                    continue
                for key in ("queued", "matched", "skipped_existing", "skipped_rejected", "skipped_filtered"):
                    summary[key] += int(result.get(key, 0) or 0)
                remaining_jobs -= int(result.get("queued", 0) or 0)
            return summary

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[PJSKPic] Pixiv ????????: {exc}", exc_info=True)
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
            query_terms = self.db.get_platform_terms_for_tag(
                tag_name=tag_name,
                platform="pixiv",
                purpose="query",
            )
            primary_query_term = query_terms[0] if query_terms else tag_name
            query_text = self.search_service.build_query(primary_query_term)
            self.db.upsert_crawl_subscription(
                platform="pixiv",
                tag_id=tag_id,
                tag_name=tag_name,
                query_text=query_text,
                enabled=True,
            )
            enabled_normalized.add(normalize_tag_name(tag_name))
        self.db.disable_missing_crawl_subscriptions(platform="pixiv", keep_normalized_tags=enabled_normalized)

    def _is_due(self, row) -> bool:
        last_checked = str(row["last_checked_at"] or "").strip()
        if not last_checked:
            return True
        try:
            last_dt = datetime.fromisoformat(last_checked)
        except ValueError:
            return True
        return datetime.utcnow() - last_dt >= timedelta(minutes=self.interval_minutes())

    async def _process_subscription(self, row, *, remaining_jobs: int) -> dict[str, int]:
        tag_name = str(row["tag_name"] or "").strip()
        if not tag_name:
            return {"queued": 0, "matched": 0, "skipped_existing": 0, "skipped_rejected": 0, "skipped_filtered": 0}

        query_terms = self.db.get_platform_terms_for_tag(
            tag_name=tag_name,
            platform="pixiv",
            purpose="query",
        ) or [tag_name]
        hits: list[PixivSearchHit] = []
        seen_illust_ids: set[str] = set()
        wanted = self.max_results_per_tag()
        for query_term in query_terms[:5]:
            remaining = max(1, wanted - len(hits))
            query_hits = await self.search_service.search_tag(
                query_term,
                max_results=remaining,
                max_pages=self.max_pages_per_tag(),
                timeout_seconds=self.timeout_seconds(),
            )
            for hit in query_hits:
                if hit.illust_id in seen_illust_ids:
                    continue
                seen_illust_ids.add(hit.illust_id)
                hits.append(hit)
                if len(hits) >= wanted:
                    break
            if len(hits) >= wanted:
                break
        newest_source_uid = hits[0].illust_id if hits else str(row["last_seen_source_uid"] or "")
        last_seen_source_uid = str(row["last_seen_source_uid"] or "").strip()

        pending_hits: list[PixivSearchHit] = []
        matched = 0
        skipped_filtered = 0
        skipped_existing = 0
        skipped_rejected = 0

        for hit in hits:
            if last_seen_source_uid and hit.illust_id == last_seen_source_uid:
                break
            if not self._matches_target_tag(tag_name, hit):
                skipped_filtered += 1
                continue
            matched += 1
            if self.db.is_rejected_source_post_url(hit.post_url, platform="pixiv"):
                skipped_rejected += 1
                continue
            if self.db.has_source_post_url(hit.post_url, platform="pixiv"):
                skipped_existing += 1
                continue
            pending_hits.append(hit)
            if len(pending_hits) >= remaining_jobs:
                break

        queued = 0
        for hit in reversed(pending_hits):
            await self.crawl_service.submit_job(
                "pixiv",
                hit.post_url,
                [tag_name],
                include_tags=[tag_name],
                exclude_tags=[],
                match_mode="partial",
            )
            queued += 1

        self.db.update_crawl_subscription_state(
            int(row["id"]),
            last_checked_at=datetime.utcnow().isoformat(timespec="seconds"),
            last_success_at=datetime.utcnow().isoformat(timespec="seconds"),
            last_error="",
            last_seen_source_uid=newest_source_uid,
            query_text=self.search_service.build_query(query_terms[0] if query_terms else tag_name),
        )
        return {
            "queued": queued,
            "matched": matched,
            "skipped_existing": skipped_existing,
            "skipped_rejected": skipped_rejected,
            "skipped_filtered": skipped_filtered,
        }

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
