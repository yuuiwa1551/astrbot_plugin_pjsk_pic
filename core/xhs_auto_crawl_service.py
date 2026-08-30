from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from typing import Any, Mapping

from astrbot.api import logger

from .db import utcnow_str
from .matcher import normalize_tag_name
from .xhs_provider import XhsNoteDetail, XhsProviderClient, XhsProviderError


class XhsAutoCrawlService:
    PLATFORM = "xiaohongshu"

    def __init__(
        self,
        *,
        db,
        crawl_service,
        config: Mapping[str, Any],
        provider_client: XhsProviderClient,
        context=None,
    ) -> None:
        self.db = db
        self.crawl_service = crawl_service
        self.config = config
        self.provider_client = provider_client
        self.context = context
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._run_lock = asyncio.Lock()

    def enabled(self) -> bool:
        return bool(self.config.get("xhs_auto_crawl_enabled", False))

    def character_only(self) -> bool:
        return bool(self.config.get("xhs_auto_crawl_character_only", True))

    def interval_minutes(self) -> int:
        return max(15, int(self.config.get("xhs_auto_crawl_interval_minutes", 180) or 180))

    def max_results_per_term(self) -> int:
        return min(max(1, int(self.config.get("xhs_auto_crawl_max_results_per_term", 20) or 20)), 50)

    def max_subscriptions_per_cycle(self) -> int:
        return min(max(1, int(self.config.get("xhs_auto_crawl_max_subscriptions_per_cycle", 3) or 3)), 20)

    def max_queries_per_cycle(self) -> int:
        return min(max(1, int(self.config.get("xhs_auto_crawl_max_queries_per_cycle", 5) or 5)), 30)

    def max_details_per_cycle(self) -> int:
        return min(max(1, int(self.config.get("xhs_auto_crawl_max_details_per_cycle", 10) or 10)), 100)

    def max_new_jobs_per_cycle(self) -> int:
        return min(max(1, int(self.config.get("xhs_auto_crawl_max_new_jobs_per_cycle", 10) or 10)), 100)

    def seed_max_notes_per_subscription(self) -> int:
        return min(max(1, int(self.config.get("xhs_auto_crawl_seed_max_notes", 3) or 3)), 20)

    def timeout_seconds(self) -> int:
        return max(
            10,
            int(
                self.config.get(
                    "xhs_provider_timeout_seconds",
                    self.config.get("platform_request_timeout", 45),
                )
                or 45
            ),
        )

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def state(self):
        return self.db.get_crawl_provider_state(self.PLATFORM)

    def paused(self) -> bool:
        row = self.state()
        return bool(row and str(row["status"] or "").lower() == "paused")

    async def start(self) -> None:
        self._stop_event.clear()
        if not self.enabled():
            logger.info("[PJSKPic] 小红书自动采集未启用")
            return
        if self.paused():
            logger.warning("[PJSKPic] 小红书自动采集处于暂停状态")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="pjsk-pic-xhs-auto-crawl")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def pause_for_error(self, error: XhsProviderError) -> None:
        changed = self.db.set_crawl_provider_state(
            self.PLATFORM,
            status="paused",
            category=error.category,
            reason=str(error),
        )
        if changed:
            logger.error(f"[PJSKPic] 小红书自动采集已暂停：{error}")
            await self._notify_state_change(
                "[PJSKPic] 小红书自动采集已暂停\n"
                f"类型：{error.category}\n原因：{str(error)[:500]}\n"
                "请检查登录/验证码/风控或上游契约后，再用 .pp 小红书采集恢复。"
            )

    async def pause_manually(self, reason: str = "管理员手动暂停") -> bool:
        error = XhsProviderError(
            str(reason or "管理员手动暂停").strip(),
            category="manual",
            pause_required=True,
        )
        before = self.paused()
        await self.pause_for_error(error)
        return not before

    async def resume(self) -> bool:
        changed = self.db.set_crawl_provider_state(self.PLATFORM, status="active")
        if changed:
            logger.info("[PJSKPic] 小红书自动采集已恢复")
            await self._notify_state_change("[PJSKPic] 小红书自动采集已恢复。")
        if self.enabled() and not self.running():
            await self.start()
        return changed

    async def run_once(self, *, force: bool = False, tag_name: str = "") -> dict[str, int]:
        async with self._run_lock:
            summary = {
                "subscriptions": 0,
                "checked": 0,
                "searched": 0,
                "detailed": 0,
                "matched": 0,
                "discovered": 0,
                "queued": 0,
                "skipped_existing": 0,
                "skipped_rejected": 0,
                "skipped_filtered": 0,
                "errors": 0,
                "paused": 0,
            }
            if not self.enabled() and not force:
                return summary
            if self.paused():
                summary["paused"] = 1
                return summary

            self._sync_subscriptions()
            subscriptions = self.db.list_crawl_subscriptions(
                platform=self.PLATFORM,
                enabled_only=True,
                limit=1000,
            )
            requested_tag = str(tag_name or "").strip()
            if requested_tag:
                match = self.db.resolve_tag(requested_tag, allow_fuzzy=False)
                wanted = normalize_tag_name(str(match.tag_name or requested_tag))
                subscriptions = [
                    row for row in subscriptions if normalize_tag_name(str(row["tag_name"] or "")) == wanted
                ]
            subscriptions = sorted(
                subscriptions,
                key=lambda row: (str(row["last_checked_at"] or ""), int(row["id"])),
            )[: self.max_subscriptions_per_cycle()]
            summary["subscriptions"] = len(subscriptions)
            if not subscriptions:
                return summary

            try:
                await asyncio.to_thread(self.provider_client.health, timeout_seconds=self.timeout_seconds())
                logged_in = await asyncio.to_thread(
                    self.provider_client.login_status,
                    timeout_seconds=self.timeout_seconds(),
                )
                if not logged_in:
                    raise XhsProviderError(
                        "小红书提供者当前未登录",
                        category="authentication",
                        pause_required=True,
                    )
                self.db.record_crawl_provider_check(self.PLATFORM, success=True)
            except XhsProviderError as exc:
                summary["errors"] += 1
                self.db.record_crawl_provider_check(self.PLATFORM, success=False, error=str(exc))
                if exc.pause_required:
                    await self.pause_for_error(exc)
                    summary["paused"] = 1
                return summary

            budget = {
                "queries": self.max_queries_per_cycle(),
                "details": self.max_details_per_cycle(),
            }
            detail_cache: dict[str, XhsNoteDetail] = {}
            for row in subscriptions:
                if budget["queries"] <= 0 or budget["details"] <= 0 or self.paused():
                    break
                if not force and not self._is_due(row):
                    continue
                summary["checked"] += 1
                try:
                    result = await self._process_subscription(
                        row,
                        budget=budget,
                        detail_cache=detail_cache,
                    )
                except XhsProviderError as exc:
                    summary["errors"] += 1
                    self.db.record_crawl_provider_check(
                        self.PLATFORM,
                        success=False,
                        error=str(exc),
                    )
                    self.db.update_crawl_subscription_state(int(row["id"]), last_error=str(exc))
                    if exc.pause_required:
                        await self.pause_for_error(exc)
                        summary["paused"] = 1
                        break
                    if exc.retryable:
                        logger.warning(
                            "[PJSKPic] 小红书提供者出现可重试故障，本轮停止后续订阅："
                            f"{exc}",
                            exc_info=True,
                        )
                        break
                    logger.warning(
                        f"[PJSKPic] 小红书自动订阅 #{row['id']} 执行失败: {exc}",
                        exc_info=True,
                    )
                    continue
                except Exception as exc:
                    summary["errors"] += 1
                    self.db.update_crawl_subscription_state(int(row["id"]), last_error=str(exc))
                    logger.warning(
                        f"[PJSKPic] 小红书自动订阅 #{row['id']} 执行失败: {exc}",
                        exc_info=True,
                    )
                    continue
                for key in (
                    "searched",
                    "detailed",
                    "matched",
                    "discovered",
                    "skipped_existing",
                    "skipped_rejected",
                    "skipped_filtered",
                    "errors",
                ):
                    summary[key] += int(result.get(key, 0) or 0)

            if not self.paused():
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
                logger.warning(f"[PJSKPic] 小红书自动采集循环失败: {exc}", exc_info=True)
            if self.paused():
                return
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_minutes() * 60)
            except asyncio.TimeoutError:
                continue

    def _sync_subscriptions(self) -> None:
        tags = self.db.list_tags_for_auto_crawl(character_only=self.character_only())
        enabled_normalized: set[str] = set()
        for row in tags:
            tag_name = str(row["name"] or "").strip()
            query_terms = self._platform_terms(tag_name, purpose="query")[:5]
            if not query_terms:
                continue
            match_terms = self._platform_terms(tag_name, purpose="match")
            enabled = bool(match_terms)
            subscription_id = self.db.upsert_crawl_subscription(
                platform=self.PLATFORM,
                tag_id=int(row["id"]),
                tag_name=tag_name,
                query_text=query_terms[0],
                enabled=enabled,
            )
            self.db.sync_crawl_subscription_terms(
                subscription_id,
                [(term, term) for term in query_terms],
            )
            if enabled:
                enabled_normalized.add(normalize_tag_name(tag_name))
                self.db.update_crawl_subscription_state(subscription_id, last_error="")
            else:
                self.db.update_crawl_subscription_state(
                    subscription_id,
                    last_error="已配置小红书 query 词，但没有 match/both 词，订阅未启用",
                )
        self.db.disable_missing_crawl_subscriptions(
            platform=self.PLATFORM,
            keep_normalized_tags=enabled_normalized,
        )

    def _platform_terms(self, tag_name: str, *, purpose: str) -> list[str]:
        return self.db.get_platform_terms_for_tag(
            tag_name=tag_name,
            platform=self.PLATFORM,
            purpose=purpose,
            include_aliases=False,
            include_primary=False,
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

    async def _process_subscription(
        self,
        row,
        *,
        budget: dict[str, int],
        detail_cache: dict[str, XhsNoteDetail],
    ) -> dict[str, int]:
        result = {
            "searched": 0,
            "detailed": 0,
            "matched": 0,
            "discovered": 0,
            "skipped_existing": 0,
            "skipped_rejected": 0,
            "skipped_filtered": 0,
            "errors": 0,
        }
        subscription_id = int(row["id"])
        tag_name = str(row["tag_name"] or "").strip()
        match_terms = self._platform_terms(tag_name, purpose="match")
        if not tag_name or not match_terms:
            self.db.update_crawl_subscription_state(
                subscription_id,
                last_checked_at=utcnow_str(),
                last_error="缺少小红书 match/both 平台词，已跳过",
            )
            result["errors"] = 1
            return result

        term_rows = sorted(
            self.db.list_crawl_subscription_terms(subscription_id),
            key=lambda term_row: (
                str(term_row["last_checked_at"] or ""),
                int(term_row["position"] or 0),
                int(term_row["id"]),
            ),
        )
        for term_row in term_rows:
            if budget["queries"] <= 0 or budget["details"] <= 0:
                break
            query_term = str(term_row["query_term"] or "").strip()
            term_id = int(term_row["id"])
            checked_at = utcnow_str()
            budget["queries"] -= 1
            try:
                hits = await asyncio.to_thread(
                    self.provider_client.search_notes,
                    query_term,
                    max_results=self.max_results_per_term(),
                    timeout_seconds=self.timeout_seconds(),
                )
                result["searched"] += 1
            except XhsProviderError as exc:
                self.db.update_crawl_subscription_term_state(
                    term_id,
                    last_checked_at=checked_at,
                    last_error=str(exc),
                )
                self.db.refresh_crawl_subscription_state(subscription_id)
                raise

            last_seen = str(term_row["last_seen_source_uid"] or "").strip()
            pending_hits = []
            for hit in hits:
                if last_seen and hit.note_id == last_seen:
                    break
                pending_hits.append(hit)
            if not last_seen:
                pending_hits = pending_hits[: self.seed_max_notes_per_subscription()]

            processed_any = False
            for hit in reversed(pending_hits):
                if budget["details"] <= 0:
                    break
                processed = False
                if self.db.is_rejected_source_post_url(hit.post_url, platform=self.PLATFORM):
                    result["skipped_rejected"] += 1
                    processed = True
                elif self.db.has_source_post_url(hit.post_url, platform=self.PLATFORM):
                    result["skipped_existing"] += 1
                    processed = True
                else:
                    detail = detail_cache.get(hit.note_id)
                    if detail is None:
                        budget["details"] -= 1
                        try:
                            detail = await asyncio.to_thread(
                                self.provider_client.fetch_note_detail,
                                hit.note_id,
                                hit.xsec_token,
                                timeout_seconds=self.timeout_seconds(),
                            )
                            detail_cache[hit.note_id] = detail
                            result["detailed"] += 1
                        except XhsProviderError as exc:
                            result["errors"] += 1
                            self.db.update_crawl_subscription_term_state(
                                term_id,
                                last_checked_at=checked_at,
                                last_error=str(exc),
                            )
                            if exc.pause_required or exc.retryable:
                                self.db.refresh_crawl_subscription_state(subscription_id)
                                raise
                            processed = True
                    if detail is not None:
                        if not detail.images or not self._matches_target(detail, match_terms):
                            result["skipped_filtered"] += 1
                        else:
                            result["matched"] += 1
                            _, created = self.db.upsert_crawl_discovery(
                                platform=self.PLATFORM,
                                source_uid=hit.note_id,
                                post_url=hit.post_url,
                                tags=[tag_name],
                                source_context={
                                    "note_id": hit.note_id,
                                    "xsec_token": hit.xsec_token,
                                    "provider": "xiaohongshu_mcp_rest",
                                },
                            )
                            if created:
                                result["discovered"] += 1
                            else:
                                result["skipped_existing"] += 1
                        processed = True

                if processed:
                    processed_any = True
                    self.db.update_crawl_subscription_term_state(
                        term_id,
                        last_seen_source_uid=hit.note_id,
                        last_checked_at=checked_at,
                        last_success_at=checked_at,
                        last_error="",
                    )

            if not pending_hits or not processed_any:
                self.db.update_crawl_subscription_term_state(
                    term_id,
                    last_checked_at=checked_at,
                    last_success_at=checked_at,
                    last_error="",
                )
            self.db.refresh_crawl_subscription_state(subscription_id)
        return result

    @staticmethod
    def _matches_target(detail: XhsNoteDetail, match_terms: list[str]) -> bool:
        normalized_topics = {normalize_tag_name(topic) for topic in detail.topics}
        normalized_topics.discard("")
        normalized_title = normalize_tag_name(detail.title)
        normalized_description = normalize_tag_name(detail.description)
        for term in match_terms:
            normalized = normalize_tag_name(term)
            if not normalized:
                continue
            if normalized in normalized_topics:
                return True
            if normalized_title and normalized in normalized_title:
                return True
            if normalized_description and normalized in normalized_description:
                return True
        return False

    async def _drain_discoveries(self, *, max_new_jobs: int) -> dict[str, int]:
        summary = {
            "queued": 0,
            "reused": 0,
            "resolved_existing": 0,
            "resolved_rejected": 0,
            "errors": 0,
        }
        rows = self.db.list_pending_crawl_discoveries(
            platform=self.PLATFORM,
            limit=max(100, max(1, int(max_new_jobs or 1)) * 5),
        )
        for row in rows:
            if summary["queued"] >= max(1, int(max_new_jobs or 1)):
                break
            discovery_id = int(row["id"])
            post_url = str(row["post_url"] or "").strip()
            tags = [
                item.strip()
                for item in str(row["tags_text"] or "").replace("，", ",").split(",")
                if item.strip()
            ]
            try:
                source_context = json.loads(str(row["source_context_json"] or "{}"))
            except (TypeError, json.JSONDecodeError):
                source_context = {}
            if not isinstance(source_context, dict):
                source_context = {}
            try:
                if self.db.is_rejected_source_post_url(post_url, platform=self.PLATFORM):
                    self.db.mark_crawl_discovery_resolved(discovery_id, status="rejected")
                    summary["resolved_rejected"] += 1
                    continue
                if self.db.has_source_post_url(post_url, platform=self.PLATFORM):
                    self.db.mark_crawl_discovery_resolved(discovery_id, status="imported")
                    summary["resolved_existing"] += 1
                    continue
                job_id, created = await self.crawl_service.submit_job_once(
                    self.PLATFORM,
                    post_url,
                    tags,
                    source_context=source_context,
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
                    f"[PJSKPic] 小红书发现记录 #{discovery_id} 提交失败: {exc}",
                    exc_info=True,
                )
        return summary

    async def _notify_state_change(self, text: str) -> None:
        if self.context is None or not bool(self.config.get("xhs_auto_crawl_notify_enabled", True)):
            return
        targets = self._notify_targets()
        if not targets:
            return
        try:
            from astrbot.core.message.message_event_result import MessageChain
        except Exception:
            return
        for target in targets:
            try:
                await self.context.send_message(target, MessageChain().message(str(text or "")))
            except Exception as exc:
                logger.warning(f"[PJSKPic] 小红书状态通知发送失败: target={target}, error={exc}")

    def _notify_targets(self) -> list[str]:
        raw = str(self.config.get("xhs_auto_crawl_notify_targets", "") or "").strip()
        values = [item.strip() for item in re.split(r"[\r\n,，;；]+", raw) if item.strip()]
        if bool(self.config.get("xhs_auto_crawl_notify_use_astr_admins", True)) and self.context is not None:
            try:
                values.extend(
                    str(item).strip()
                    for item in (self.context.get_config().get("admins_id", []) or [])
                    if str(item).strip()
                )
            except Exception:
                pass
        result: list[str] = []
        seen: set[str] = set()
        for item in values:
            target = item if item.count(":") >= 2 and "Message" in item else f"aiocqhttp:FriendMessage:{item}"
            if target in seen:
                continue
            seen.add(target)
            result.append(target)
        return result
