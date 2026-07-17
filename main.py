from __future__ import annotations

import asyncio
import re
import sys
import shutil
import unicodedata
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event import filter as event_filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.message_event_result import MessageChain

from .core import (
    AutoCrawlService,
    CrawlTagRules,
    CrawlService,
    ImageIndexDB,
    ImportedImageService,
    LibraryIndexer,
    PixivBackfillService,
    QQReviewSession,
    QQReviewSessionService,
    ReviewService,
    SubmissionNotifyService,
    SubmissionService,
    extract_query_from_text,
    parse_crawl_rule_text,
)
from .core.webui import GalleryWebUI


class PJSKPicPlugin(Star):
    OPEN_REVIEW_STATUSES = ("pending", "uncertain", "rejected")
    SENDABLE_REVIEW_STATUSES = {"approved", "manual_approved"}
    DIRECT_IMAGE_ID_PATTERN = re.compile(
        r"^\s*(?:看看|看下|看一看|看一下|看)\s*(?:(?:图片|图)\s*)?(?:id|编号|#)\s*(?:[:：#号=为是-]\s*)?([0-9０-９]+)\s*(?:的?(?:图片|图))?\s*$",
        re.IGNORECASE,
    )

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_pjsk_pic")
        self.db = ImageIndexDB(self.data_dir / "image_index.db")
        self.indexer = LibraryIndexer(self.db)
        self.importer = ImportedImageService(
            self.db,
            self.data_dir,
            timeout_seconds=self._crawler_timeout(),
            enable_phash_dedupe=bool(self.config.get("enable_phash_dedupe", True)),
            phash_max_distance=int(self.config.get("phash_max_distance", 8) or 8),
        )
        self.reviewer = ReviewService(context, self.db, config)
        self.crawl_service = CrawlService(
            db=self.db,
            importer=self.importer,
            reviewer=self.reviewer,
            config=config,
        )
        self.auto_crawl_service = AutoCrawlService(
            db=self.db,
            crawl_service=self.crawl_service,
            config=config,
        )
        self.pixiv_backfill_service = PixivBackfillService(
            db=self.db,
            crawl_service=self.crawl_service,
            config=config,
        )
        self.submission_service = SubmissionService(self.db, self.importer, self.reviewer)
        self.submission_notify_service = SubmissionNotifyService(context, self.db, config)
        self.qq_review_service = QQReviewSessionService(self.db, config)
        self.webui = GalleryWebUI(
            self.db,
            self.crawl_service,
            pixiv_backfill_service=self.pixiv_backfill_service,
            context=context,
            config=config,
        )
        self.recent_by_session: dict[str, deque[int]] = defaultdict(
            lambda: deque(maxlen=self._dedupe_count()),
        )

    async def initialize(self) -> None:
        library_root = self._library_root()
        library_root.mkdir(parents=True, exist_ok=True)
        if self.config.get("scan_on_startup", True):
            try:
                await asyncio.to_thread(self.indexer.scan, library_root)
            except Exception as exc:
                logger.error(f"[PJSKPic] 启动扫描失败: {exc}", exc_info=True)
        await self.crawl_service.start()
        await self.pixiv_backfill_service.start()
        await self.auto_crawl_service.start()
        if self._webui_enabled():
            try:
                await self.webui.start(
                    host=self._webui_host(),
                    port=self._webui_port(),
                    access_token=self._webui_access_token(),
                )
            except Exception as exc:
                logger.error(f"[PJSKPic] 独立 WebUI 启动失败: {exc}", exc_info=True)

    async def terminate(self) -> None:
        await self.qq_review_service.clear()
        await self.webui.stop()
        await self.auto_crawl_service.stop()
        await self.pixiv_backfill_service.stop()
        await self.crawl_service.stop()

    def _library_root(self) -> Path:
        configured = str(self.config.get("library_root", "") or "").strip()
        if configured:
            return Path(configured).expanduser().resolve()
        return (self.data_dir / "library").resolve()

    def _dedupe_count(self) -> int:
        count = int(self.config.get("recent_dedupe_count", 20) or 20)
        return max(1, count)

    def _crawler_timeout(self) -> int:
        value = int(self.config.get("platform_request_timeout", self.config.get("crawler_timeout_seconds", 20)) or 20)
        return max(5, value)

    def _webui_enabled(self) -> bool:
        return bool(self.config.get("webui_enabled", True))

    def _webui_host(self) -> str:
        return str(self.config.get("webui_host", "0.0.0.0") or "0.0.0.0").strip() or "0.0.0.0"

    def _webui_port(self) -> int:
        value = int(self.config.get("webui_port", 9099) or 9099)
        return min(max(1, value), 65535)

    def _webui_access_token(self) -> str:
        return str(self.config.get("webui_access_token", "") or "").strip()

    def _submission_review_enabled(self) -> bool:
        return bool(self.config.get("submission_review_enabled", False))

    def _set_submission_review_enabled(self, enabled: bool) -> tuple[bool, str]:
        self.config["submission_review_enabled"] = bool(enabled)
        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            try:
                save_config()
            except Exception as exc:
                logger.error(f"[PJSKPic] 保存投稿审核配置失败: {exc}", exc_info=True)
                return False, f"保存投稿审核配置失败：{exc}"
        return True, ""

    def _recent_queue(self, session_id: str) -> deque[int]:
        key = session_id or "default"
        queue = self.recent_by_session.get(key)
        if queue is None or queue.maxlen != self._dedupe_count():
            queue = deque(list(queue or []), maxlen=self._dedupe_count())
            self.recent_by_session[key] = queue
        return queue

    def _image_id_lookup_enabled(self) -> bool:
        return bool(self.config.get("image_id_lookup_enabled", True))

    def _image_id_lookup_admin_only(self) -> bool:
        return bool(self.config.get("image_id_lookup_admin_only", True))

    def _is_admin_event(self, event: AstrMessageEvent) -> bool:
        try:
            is_admin_attr = getattr(event, "is_admin", None)
            if callable(is_admin_attr):
                if bool(is_admin_attr()):
                    return True
            elif is_admin_attr is not None and bool(is_admin_attr):
                return True
        except Exception:
            pass
        try:
            role = getattr(event, "role", None)
            if isinstance(role, str) and role.lower() == "admin":
                return True
        except Exception:
            pass
        try:
            sender_id = str(event.get_sender_id())
            astrbot_config = self.context.get_config()
            for key in ("admins_id", "admins", "admin_ids", "admin_list", "superusers", "super_users"):
                values = astrbot_config.get(key, [])
                if isinstance(values, (list, tuple, set)) and sender_id in {str(item) for item in values}:
                    return True
        except Exception:
            pass
        return False

    def _can_use_image_id_lookup(self, event: AstrMessageEvent) -> bool:
        return (not self._image_id_lookup_admin_only()) or self._is_admin_event(event)

    def _parse_direct_image_id(self, message: str) -> int | None:
        text = unicodedata.normalize("NFKC", str(message or ""))
        match = self.DIRECT_IMAGE_ID_PATTERN.match(text)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    async def _send_image_detail_by_id(
        self,
        event: AstrMessageEvent,
        image_id: int,
        *,
        prefix: str = "",
    ) -> bool:
        detail = self.db.get_image_detail(int(image_id))
        if not detail:
            await event.send(MessageChain().message(f"没有找到图片：#{int(image_id)}"))
            return False

        image_path = self._find_detail_image_path(detail, prefer_active=True)
        if image_path is None:
            image_path = self._find_trash_path(detail)
        if image_path is not None and image_path.exists():
            await event.send(MessageChain().file_image(str(image_path)))

        detail_text = self._build_image_detail_text(detail)
        if prefix:
            detail_text = f"{prefix}\n{detail_text}"
        await event.send(MessageChain().message(detail_text))
        return True

    async def _handle_direct_image_id_message(self, event: AstrMessageEvent) -> bool:
        direct_image_id = self._parse_direct_image_id(event.message_str)
        if direct_image_id is None:
            return False
        if not self._image_id_lookup_enabled():
            await event.send(MessageChain().message("图片 ID 查看入口当前未启用。"))
            return True
        if not self._can_use_image_id_lookup(event):
            await event.send(MessageChain().message("这个图片 ID 查看入口当前仅管理员可用。"))
            return True
        if direct_image_id <= 0:
            await event.send(MessageChain().message("图片 ID 需要大于 0，例如：看看id123"))
            return True
        await self._send_image_detail_by_id(
            event,
            direct_image_id,
            prefix=f"图片 ID #{direct_image_id}",
        )
        return True

    @staticmethod
    def _format_status_counts(counts: dict[str, int], order: tuple[str, ...]) -> str:
        parts: list[str] = []
        seen: set[str] = set()
        for status in order:
            if int(counts.get(status, 0) or 0) > 0:
                parts.append(f"{status}={int(counts.get(status, 0) or 0)}")
                seen.add(status)
        for status, total in sorted(counts.items()):
            if status in seen or int(total or 0) <= 0:
                continue
            parts.append(f"{status}={int(total or 0)}")
        return "，".join(parts) if parts else "无"

    @staticmethod
    def _short_text(value: str, limit: int = 180) -> str:
        text = str(value or "").strip().replace("\r", " ").replace("\n", " ")
        if len(text) <= limit:
            return text
        return text[: max(1, limit - 1)] + "…"

    def _format_crawl_job_brief(self, row, *, index: int | None = None) -> str:
        prefix = f"{index}. " if index is not None and index > 0 else ""
        error_text = self._short_text(str(row["error_log"] or ""), 180) or "-"
        source_url = self._short_text(str(row["source_url"] or ""), 160) or "-"
        return (
            f"{prefix}#{int(row['id'])} {row['platform']} {row['status']} "
            f"progress={int(row['progress'] or 0)} attempts={int(row['attempt_count'] or 0)}\n"
            f"   URL：{source_url}\n"
            f"   错误：{error_text}\n"
            f"   更新：{row['updated_at']}"
        )

    def _review_image_path(self, image_id: int, fallback_path: str = "") -> Path | None:
        resolved_path = self.db.get_image_file_path(int(image_id)) or str(fallback_path or "")
        if not resolved_path:
            return None
        path = Path(resolved_path)
        if not path.exists():
            return None
        return path

    async def _send_review_group_preview(
        self,
        event: AstrMessageEvent,
        rows: list[dict],
        *,
        display_index: int | None = None,
    ) -> None:
        if not rows:
            return
        first = rows[0]
        image_id = int(first["image_id"])
        image_path = self._review_image_path(image_id, str(first.get("file_path", "") or ""))
        if image_path:
            await event.send(MessageChain().file_image(str(image_path)))

        if display_index is not None and display_index > 0:
            header = f"{display_index}. 图片 #{image_id} 的审核任务（{len(rows)} 条）"
        else:
            header = f"图片 #{image_id} 的审核任务（{len(rows)} 条）"
        lines = [header]
        for row in rows:
            lines.append(
                f"#{row['id']} [{row['status']}] tag={row['tag_name']}\n"
                f"来源：{row.get('source_type') or '-'}\n"
                f"原因：{row.get('reason') or '-'}"
            )
        if rows:
            review_id = int(rows[0]["id"])
            lines.append(f"查看详情：/pjsk图库 审核查看 {review_id}")
        lines.append(f"图片详情：看看id{image_id}")
        await event.send(MessageChain().message("\n\n".join(lines)))

    async def _send_review_task_detail(self, event: AstrMessageEvent, task) -> None:
        image_path = self._review_image_path(int(task["image_id"]), str(task["file_path"] or ""))
        if image_path:
            await event.send(MessageChain().file_image(str(image_path)))
        await event.send(
            MessageChain().message(
                f"审核任务 #{task['id']}\n"
                f"状态：{task['status']}\n"
                f"tag：{task['tag_name']}\n"
                f"image_id：{task['image_id']}\n"
                f"来源：{task['source_type'] or '-'}\n"
                f"原因：{task['reason'] or '-'}\n"
                f"通过：/pjsk图库 审核通过 {task['id']}\n"
                f"拒绝：/pjsk图库 审核拒绝 {task['id']}"
            ),
        )

    async def _send_next_open_review_task(
        self,
        event: AstrMessageEvent,
        *,
        exclude_review_ids: set[int] | None = None,
    ) -> bool:
        excluded = {int(item) for item in (exclude_review_ids or set()) if int(item) > 0}
        rows = self.db.list_review_tasks(statuses=self.OPEN_REVIEW_STATUSES, limit=20)
        for row in rows:
            review_id = int(row["id"])
            if review_id in excluded:
                continue
            await event.send(MessageChain().message("下一张待审核图片："))
            await self._send_review_task_detail(event, row)
            return True
        await event.send(MessageChain().message("当前没有更多待审核图片。"))
        return False

    def _qq_review_enabled(self) -> bool:
        return bool(self.config.get("qq_review_enabled", True))

    def _qq_review_auto_next(self) -> bool:
        return bool(self.config.get("qq_review_auto_next", True))

    def _qq_review_source_term_limit(self) -> int:
        raw_value = self.config.get("qq_review_source_term_limit", 12)
        value = int(raw_value) if raw_value is not None and str(raw_value).strip() else 12
        return min(max(value, 0), 30)

    @staticmethod
    def _qq_review_identity(event: AstrMessageEvent) -> tuple[str, str]:
        origin = str(getattr(event, "unified_msg_origin", "default") or "default")
        try:
            reviewer_id = str(event.get_sender_id() or "unknown")
        except Exception:
            reviewer_id = "unknown"
        return origin, reviewer_id

    def _resolve_qq_review_tag(self, raw_query: str) -> tuple[str | None, str, list[str]]:
        query = str(raw_query or "").strip()
        if not query:
            return None, "", []
        direct = self.db.resolve_tag(query, allow_fuzzy=False)
        if direct.matched and direct.tag_name:
            return str(direct.tag_name), str(direct.match_type or ""), []
        platform = self.db.resolve_platform_term("pixiv", query)
        if platform.matched and platform.tag_name:
            return str(platform.tag_name), str(platform.match_type or "platform:pixiv"), []
        fuzzy = self.db.resolve_tag(
            query,
            allow_fuzzy=True,
            candidate_limit=int(self.config.get("ambiguous_candidate_limit", 5) or 5),
        )
        if fuzzy.matched and fuzzy.tag_name:
            return None, "", [str(fuzzy.tag_name)]
        return None, "", [str(item) for item in (fuzzy.candidates or [])]

    async def _send_qq_review_session(
        self,
        event: AstrMessageEvent,
        session: QQReviewSession,
        *,
        remaining: int | None = None,
    ) -> bool:
        image_path = self._review_image_path(session.image_id)
        if image_path is None:
            return False

        detail = self.db.get_image_detail(session.image_id, sync_files=False) or {}
        tasks = self.db.get_review_tasks_for_image(
            session.image_id,
            statuses=QQReviewSessionService.OPEN_STATUSES,
        )
        candidate_tags = []
        seen_candidates: set[str] = set()
        for task in tasks:
            tag_name = str(task["tag_name"] or "").strip()
            key = tag_name.casefold()
            if tag_name and key not in seen_candidates:
                seen_candidates.add(key)
                candidate_tags.append(tag_name)

        pixiv_source = next(
            (
                item
                for item in list(detail.get("sources") or [])
                if str(item.get("platform") or "").strip().lower() == "pixiv"
            ),
            {},
        )
        extra = pixiv_source.get("extra") if isinstance(pixiv_source.get("extra"), dict) else {}
        source_terms: list[str] = []
        seen_terms: set[str] = set()
        for value in [
            *list(pixiv_source.get("raw_tags") or []),
            *list(extra.get("translated_tags") or []),
        ]:
            text = str(value or "").strip()
            key = text.casefold()
            if text and key not in seen_terms:
                seen_terms.add(key)
                source_terms.append(text)
        source_limit = self._qq_review_source_term_limit()
        visible_terms = source_terms[:source_limit] if source_limit > 0 else []
        if source_limit > 0 and len(source_terms) > source_limit:
            visible_terms.append(f"…另 {len(source_terms) - source_limit} 个")

        lines = [
            f"Pixiv 群友审核 · 图片 #{session.image_id}",
            "候选 tag：" + ("、".join(candidate_tags) if candidate_tags else "无"),
        ]
        if session.filter_tag_name:
            lines.append(f"当前筛选：{session.filter_tag_name}")
        title = str(extra.get("title") or "").strip()
        author = str(pixiv_source.get("author") or "").strip()
        if title:
            lines.append(f"标题：{title}")
        if author:
            lines.append(f"作者：{author}")
        if visible_terms:
            lines.append("Pixiv 来源词：" + "、".join(visible_terms))
        post_url = str(pixiv_source.get("post_url") or "").strip()
        if post_url:
            lines.append(f"来源：{post_url}")
        if remaining is not None:
            lines.append(f"当前队列：约 {max(0, int(remaining))} 张待审")
        lines.extend(
            [
                "通过并归类：/pp 审图通过 <最终tag>",
                "整图不要：/pp 审图拒绝 [原因]",
                "换一张：/pp 审图跳过",
                "提示：整图拒绝会阻止这个 Pixiv 作品以后再次被抓取。",
            ]
        )
        await event.send(MessageChain().file_image(str(image_path)))
        await event.send(MessageChain().message("\n".join(lines)))
        return True

    async def _claim_and_send_qq_review(
        self,
        event: AstrMessageEvent,
        *,
        filter_tag_id: int = 0,
        filter_tag_name: str = "",
        replace_current: bool = True,
    ) -> bool:
        origin, reviewer_id = self._qq_review_identity(event)
        for _ in range(3):
            session, remaining = await self.qq_review_service.claim_next(
                origin=origin,
                reviewer_id=reviewer_id,
                filter_tag_id=filter_tag_id,
                filter_tag_name=filter_tag_name,
                replace_current=replace_current,
            )
            if session is None:
                scope = f"候选 tag“{filter_tag_name}”下" if filter_tag_name else ""
                await event.send(MessageChain().message(f"当前{scope}没有可领取的 Pixiv 待审图片。"))
                return False
            if await self._send_qq_review_session(event, session, remaining=remaining):
                return True
            await self.qq_review_service.release_current(
                origin=origin,
                reviewer_id=reviewer_id,
                remember=True,
            )
            replace_current = True
        await event.send(MessageChain().message("连续抽到文件不可用的待审记录，请稍后重试或联系管理员检查图库文件。"))
        return False

    def _resolve_existing_tag_name(self, raw_query: str, *, allow_fuzzy: bool = False) -> tuple[str | None, str]:
        query = str(raw_query or "").strip()
        if not query:
            return None, ""
        match = self.db.resolve_tag(
            query=query,
            allow_fuzzy=allow_fuzzy,
            candidate_limit=int(self.config.get("ambiguous_candidate_limit", 5) or 5),
        )
        if match.matched and match.tag_name:
            return str(match.tag_name), str(match.match_type or "")
        return None, ""

    @staticmethod
    def _parse_alias_csv(alias_text: str) -> list[str]:
        if not alias_text:
            return []
        raw = (
            str(alias_text)
            .replace("，", ",")
            .replace("、", ",")
            .replace("；", ";")
        )
        items: list[str] = []
        seen: set[str] = set()
        for chunk in raw.replace(";", ",").split(","):
            alias = chunk.strip()
            normalized = alias.casefold()
            if not alias or normalized in seen:
                continue
            seen.add(normalized)
            items.append(alias)
        return items

    @staticmethod
    def _parse_shortcut_args(raw_message: str, command_names: set[str]) -> tuple[str, list[str]]:
        text = str(raw_message or "").strip()
        if not text:
            return "", []
        parts = text.split(maxsplit=1)
        head = parts[0].lstrip("/!！.。．").strip().lower()
        body = parts[1].strip() if len(parts) > 1 and head in command_names else text
        if not body:
            return "", []
        target, _, rest = body.partition(" ")
        aliases = PJSKPicPlugin._parse_alias_csv(rest.strip())
        return target.strip(), aliases

    @staticmethod
    def _parse_alias_command_args(raw_message: str) -> tuple[str, list[str]]:
        return PJSKPicPlugin._parse_shortcut_args(raw_message, {"alias", "别名"})

    def _batch_add_aliases(self, canonical_tag_name: str, aliases: list[str]) -> tuple[list[str], list[str]]:
        added: list[str] = []
        skipped: list[str] = []
        for alias in aliases:
            ok, message = self.db.add_alias(canonical_tag_name, alias)
            if ok:
                added.append(alias)
            else:
                skipped.append(f"{alias}（{message}）")
        return added, skipped

    def _batch_remove_aliases(self, canonical_tag_name: str, aliases: list[str]) -> tuple[list[str], list[str]]:
        removed: list[str] = []
        skipped: list[str] = []
        for alias in aliases:
            ok, message = self.db.remove_alias(canonical_tag_name, alias)
            if ok:
                removed.append(alias)
            else:
                skipped.append(f"{alias}（{message}）")
        return removed, skipped

    def _sync_auto_crawl_subscriptions_safe(self) -> None:
        try:
            self.auto_crawl_service._sync_subscriptions()
        except Exception as exc:
            logger.warning(f"[PJSKPic] 自动采集订阅同步失败: {exc}", exc_info=True)

    @staticmethod
    def _collect_display_tag_names(tags: list[dict], *, sendable_only: bool = False) -> list[str]:
        selected = tags
        if sendable_only:
            visible = [
                tag for tag in tags
                if str(tag.get("review_status") or "") in PJSKPicPlugin.SENDABLE_REVIEW_STATUSES
            ]
            if visible:
                selected = visible
        result: list[str] = []
        seen: set[str] = set()
        for tag in selected:
            name = str(tag.get("name") or "").strip()
            normalized = name.casefold()
            if not name or normalized in seen:
                continue
            seen.add(normalized)
            result.append(name)
        return result

    @staticmethod
    def _find_detail_image_path(detail: dict, *, prefer_active: bool = True) -> Path | None:
        candidates: list[str] = []
        image = dict(detail.get("image") or {})
        file_locations = list(detail.get("file_locations") or [])

        if prefer_active and image.get("is_active") and image.get("file_path"):
            candidates.append(str(image["file_path"]))
        for row in file_locations:
            if prefer_active and row.get("is_active") and row.get("file_path"):
                candidates.append(str(row["file_path"]))

        if image.get("file_path"):
            candidates.append(str(image["file_path"]))
        for row in file_locations:
            if row.get("file_path"):
                candidates.append(str(row["file_path"]))

        seen: set[str] = set()
        for candidate in candidates:
            normalized = str(candidate).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            path = Path(normalized)
            if path.exists():
                return path
        return None

    @staticmethod
    def _build_source_brief_line(detail: dict | None) -> str:
        sources = list((detail or {}).get("sources") or [])
        submission_fallback = ""
        for source in sources:
            platform = str(source.get("platform") or "").strip().lower()
            extra = dict(source.get("extra") or {})
            if platform == "submission" or str(extra.get("source_kind") or "").strip() == "user_submission":
                submission_fallback = "来源：来自投稿"
                continue
            post_url = str(source.get("post_url") or "").strip()
            if post_url:
                return f"来源：{post_url}"
        return submission_fallback

    def _build_image_brief_text(self, image_id: int, *, matched_tag: str = "") -> str:
        detail = self.db.get_image_detail(int(image_id))
        tag_names = []
        if detail:
            tag_names = self._collect_display_tag_names(list(detail.get("tags") or []), sendable_only=True)
        if not tag_names and matched_tag:
            tag_names = [matched_tag]
        tag_text = "、".join(tag_names) if tag_names else "-"
        lines = [f"#{image_id}", f"tag：{tag_text}"]
        source_line = self._build_source_brief_line(detail)
        if source_line:
            lines.append(source_line)
        return "\n".join(lines)

    def _build_image_detail_text(self, detail: dict) -> str:
        image = dict(detail.get("image") or {})
        image_id = int(image.get("id") or 0)
        width = int(image.get("width") or 0)
        height = int(image.get("height") or 0)
        format_name = str(image.get("format") or "-").upper()
        status_text = "可发送" if int(image.get("is_active") or 0) == 1 else "已移出可发送列表"

        tags = list(detail.get("tags") or [])
        tag_segments = [
            f"{tag['name']}[{tag['review_status']}]"
            for tag in tags
            if str(tag.get("name") or "").strip()
        ]
        tag_text = "、".join(tag_segments) if tag_segments else "无"

        sources = list(detail.get("sources") or [])
        source_lines = []
        for source in sources[:3]:
            source_lines.append(
                f"- {source['platform']} / {source['author'] or '-'} / {source['post_url'] or '-'}"
            )
        if not source_lines:
            source_lines.append("- 无")

        file_locations = list(detail.get("file_locations") or [])
        location_lines = []
        for row in file_locations[:4]:
            state = "active" if row.get("is_active") else "inactive"
            location_lines.append(f"- [{row.get('storage_type')}/{state}] {row.get('file_path')}")
        if not location_lines:
            location_lines.append(f"- {image.get('file_path') or '-'}")

        return (
            f"图片：#{image_id}\n"
            f"状态：{status_text}\n"
            f"尺寸：{width}x{height}\n"
            f"格式：{format_name}\n"
            f"当前路径：{image.get('file_path') or '-'}\n"
            f"tag：{tag_text}\n"
            f"来源：\n" + "\n".join(source_lines) + "\n"
            f"文件位置：\n" + "\n".join(location_lines)
        )

    def _trash_root(self) -> Path:
        return (self.data_dir / "trash" / "images").resolve()

    def _build_trash_destination(self, image_id: int, current_path: Path) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        parent = self._trash_root() / str(image_id)
        parent.mkdir(parents=True, exist_ok=True)
        candidate = parent / f"{stamp}_{current_path.name}"
        index = 1
        while candidate.exists():
            candidate = parent / f"{stamp}_{index}_{current_path.name}"
            index += 1
        return candidate

    def _find_trash_path(self, detail: dict) -> Path | None:
        file_locations = list(detail.get("file_locations") or [])
        seen: set[str] = set()
        for row in file_locations:
            if str(row.get("storage_type") or "") != "trash":
                continue
            raw_path = str(row.get("file_path") or "").strip()
            if not raw_path or raw_path in seen:
                continue
            seen.add(raw_path)
            path = Path(raw_path)
            if path.exists():
                return path
        return None

    def _build_restore_destination(self, detail: dict, source_path: Path) -> Path:
        image = dict(detail.get("image") or {})
        image_id = int(image.get("id") or 0)
        file_locations = list(detail.get("file_locations") or [])

        candidate_paths: list[Path] = []
        current_path = str(image.get("file_path") or "").strip()
        if current_path and "/trash/" not in current_path.replace("\\", "/").lower():
            candidate_paths.append(Path(current_path))
        for row in file_locations:
            raw_path = str(row.get("file_path") or "").strip()
            if not raw_path:
                continue
            normalized = raw_path.replace("\\", "/").lower()
            if "/trash/" in normalized:
                continue
            candidate_paths.append(Path(raw_path))

        target = candidate_paths[0] if candidate_paths else (self.data_dir / "images" / "restored" / str(image_id) / source_path.name)
        target = target.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target == source_path:
            target = (self.data_dir / "images" / "restored" / str(image_id) / source_path.name).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)

        if not target.exists():
            return target

        stem = target.stem
        suffix = target.suffix
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        candidate = target.with_name(f"{stem}.restored-{stamp}{suffix}")
        index = 1
        while candidate.exists():
            candidate = target.with_name(f"{stem}.restored-{stamp}-{index}{suffix}")
            index += 1
        return candidate

    async def _send_tag_image(
        self,
        event: AstrMessageEvent,
        raw_query: str,
        count: int = 1,
        silent_on_tool: bool = False,
    ) -> str | None:
        query = (raw_query or "").strip()
        if not query:
            if not silent_on_tool:
                await event.send(MessageChain().message("没看懂你要看什么图。"))
            return "empty_query"

        match = self.db.resolve_tag(
            query=query,
            allow_fuzzy=bool(self.config.get("allow_fuzzy_match", True)),
            candidate_limit=int(self.config.get("ambiguous_candidate_limit", 5) or 5),
        )

        if not match.matched:
            if match.candidates:
                msg = f"你想看的是不是：{'、'.join(match.candidates)}"
            else:
                msg = f"图库里还没有“{query}”这个 tag。"
            if not silent_on_tool:
                await event.send(MessageChain().message(msg))
            return "tag_not_found"

        send_count = max(1, min(int(count or 1), 3))
        sent = 0
        queue = self._recent_queue(getattr(event, "unified_msg_origin", "default"))

        for _ in range(send_count):
            row = self.db.get_random_image_for_tag(match.tag_id, list(queue))
            if not row:
                if sent == 0:
                    await event.send(
                        MessageChain().message(f"“{match.tag_name}”这个 tag 目前没有可发送图片。"),
                    )
                    return "empty_tag"
                break

            resolved_path = self.db.get_image_file_path(int(row["id"]))
            if not resolved_path:
                continue

            image_path = Path(resolved_path)
            if not image_path.exists():
                continue

            await event.send(MessageChain().file_image(str(image_path)))
            brief_text = self._build_image_brief_text(
                int(row["id"]),
                matched_tag=str(match.tag_name),
            )
            await event.send(
                MessageChain().message(brief_text),
            )
            queue.append(int(row["id"]))
            self.db.record_send_log(
                getattr(event, "unified_msg_origin", "default"),
                int(row["id"]),
                str(match.tag_name),
            )
            sent += 1

        if sent == 0:
            return "send_failed"
        return None

    @filter.regex(r"^(?!(?:看看|看下|看一看|看一下|看)\s*[0-9０-９]+\s*$)(?:看看|看下|看一看|看一下|看|来张|来一张|发一张|来点).+", priority=sys.maxsize)
    async def send_image_by_natural_language(self, event: AstrMessageEvent):
        if await self._handle_direct_image_id_message(event):
            event.stop_event()
            return
        query = extract_query_from_text(event.message_str)
        if not query:
            return
        await self._send_tag_image(event, query, silent_on_tool=True)
        event.stop_event()

    def _parse_submission_request(self, raw_message: str):
        request = self.submission_service.parse_submission_text(raw_message)
        if request and request.tag_name:
            return request
        text = str(raw_message or "").strip()
        if not text:
            return None
        candidates: list[str] = [text]
        if " " in text:
            body = text.partition(" ")[2].strip()
            if body and body not in candidates:
                candidates.append(body)
        for candidate in candidates:
            fallback = self.submission_service.parse_submission_text(f"\u6295\u7A3F {candidate}")
            if fallback and fallback.tag_name:
                return fallback
        return None

    async def _handle_submission_event(self, event: AstrMessageEvent, *, missing_tag_reply: str | None = None) -> bool:
        request = self._parse_submission_request(event.message_str)
        if not request or not request.tag_name:
            if missing_tag_reply:
                await event.send(MessageChain().message(missing_tag_reply))
                event.stop_event()
            return False
        result = await self.submission_service.submit_from_event(
            event,
            request.tag_name,
            aliases=request.aliases,
            review_enabled=self._submission_review_enabled(),
        )
        if result.reply_message:
            await event.send(MessageChain().message(result.reply_message))
        if result.ok:
            await self.submission_notify_service.notify(event, result)
        event.stop_event()
        return bool(result.ok)

    @filter.command("\u6295\u7A3F", alias={"tg"})
    async def submit_image_by_user_command(self, event: AstrMessageEvent):
        await self._handle_submission_event(
            event,
            missing_tag_reply="\u8BF7\u5728\u6295\u7A3F\u547D\u4EE4\u540E\u63D0\u4F9B\u89D2\u8272 tag\uFF0C\u4F8B\u5982\uFF1A/tg \u521D\u97F3\u672A\u6765",
        )

    @filter.regex(r"^\s*(?:@.+?\(\d+\)\s+)*(?:[/!！.。．])?(?:投稿|tg)\s+.+$")
    async def submit_image_by_user(self, event: AstrMessageEvent):
        await self._handle_submission_event(event)

    @filter.command("alias", alias={"别名"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def alias_shortcut(self, event: AstrMessageEvent):
        target_input, alias_values = self._parse_alias_command_args(event.message_str)
        if not target_input:
            yield event.plain_result("用法：/alias <tag或alias> [新alias1,新alias2]")
            return

        canonical_tag_name, match_type = self._resolve_existing_tag_name(target_input, allow_fuzzy=False)
        if not canonical_tag_name:
            yield event.plain_result(f"没有找到 tag 或 alias：{target_input}")
            return

        lines = [f"主 tag：{canonical_tag_name}"]
        if match_type == "exact_alias":
            lines.append(f"输入“{target_input}”命中 alias，已归并到主 tag。")

        if alias_values:
            added, skipped = self._batch_add_aliases(canonical_tag_name, alias_values)
            if added:
                lines.append("已添加别名：" + "、".join(added))
            if skipped:
                lines.append("以下别名未添加：" + "；".join(skipped[:10]))

        aliases = self.db.list_aliases(canonical_tag_name)
        lines.append("当前别名：" + ("、".join(aliases) if aliases else "无"))
        yield event.plain_result("\n".join(lines))

    @filter.command("unalias", alias={"删别名"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def unalias_shortcut(self, event: AstrMessageEvent):
        target_input, alias_values = self._parse_shortcut_args(event.message_str, {"unalias", "删别名"})
        if not target_input or not alias_values:
            yield event.plain_result("用法：/unalias <tag或alias> <alias1,alias2>")
            return

        canonical_tag_name, match_type = self._resolve_existing_tag_name(target_input, allow_fuzzy=False)
        if not canonical_tag_name:
            yield event.plain_result(f"没有找到 tag 或 alias：{target_input}")
            return

        removed, skipped = self._batch_remove_aliases(canonical_tag_name, alias_values)
        lines = [f"主 tag：{canonical_tag_name}"]
        if match_type == "exact_alias":
            lines.append(f"输入“{target_input}”命中 alias，已归并到主 tag。")
        if removed:
            lines.append("已删除别名：" + "、".join(removed))
        if skipped:
            lines.append("以下别名未删除：" + "；".join(skipped[:10]))
        aliases = self.db.list_aliases(canonical_tag_name)
        lines.append("当前别名：" + ("、".join(aliases) if aliases else "无"))
        yield event.plain_result("\n".join(lines))

    @event_filter.llm_tool(name="send_local_image_by_tag")
    async def send_local_image_by_tag(self, event: AstrMessageEvent, tag: str, count: int = 1):
        """
        从本地图库按 tag 或别名随机发送图片。

        Args:
            tag(string): 想看的图片 tag、角色名或 tag 别名
            count(number): 发送图片数量，默认 1，当前最多 3
        """
        if not self.config.get("enable_llm_tool", True):
            return "该工具当前未启用。"
        await self._send_tag_image(event, tag, count=count, silent_on_tool=False)
        return None

    @filter.command_group("pjsk图库", alias={"pp"})
    async def pjsk_gallery(self):
        """PJSK 图片库管理命令。"""

    @pjsk_gallery.command("重扫")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def rescan_library(self, event: AstrMessageEvent):
        library_root = self._library_root()
        yield event.plain_result(f"开始扫描图库：{library_root}")
        result = await asyncio.to_thread(self.indexer.scan, library_root)
        yield event.plain_result(
            "扫描完成："
            f"扫描 {result['scanned']}，入库 {result['indexed']}，关联 {result['linked']}，"
            f"跳过 {result['skipped']}，失效 {result['missing_marked_inactive']}"
        )

    @pjsk_gallery.command("帮助", alias={"help", "菜单", "命令"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def show_gallery_help(self, event: AstrMessageEvent, section: str = ""):
        yield event.plain_result(self._build_gallery_help_text(section))

    @pjsk_gallery.command("统计")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def show_stats(self, event: AstrMessageEvent):
        stats = self.db.get_stats()
        yield event.plain_result(
            "图库统计："
            f"图片 {stats['images']} 张，tag {stats['tags']} 个，alias {stats['aliases']} 个，"
            f"采集任务 {stats['crawl_jobs']} 个，自动订阅 {stats['crawl_subscriptions']} 个，"
            f"待处理审核 {stats['pending_reviews']} 个。"
        )

    @pjsk_gallery.command("查看")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def show_tag_info(self, event: AstrMessageEvent, tag_name: str):
        canonical_tag_name, match_type = self._resolve_existing_tag_name(tag_name, allow_fuzzy=False)
        if not canonical_tag_name:
            yield event.plain_result(f"没有找到 tag：{tag_name}")
            return
        count = self.db.count_images_for_tag(canonical_tag_name)
        all_count = self.db.count_images_for_tag(canonical_tag_name, include_unapproved=True)
        aliases = self.db.list_aliases(canonical_tag_name)
        row = self.db.get_tag_row(canonical_tag_name)
        if count == 0 and not aliases and row is None:
            yield event.plain_result(f"没有找到 tag：{tag_name}")
            return
        alias_text = "、".join(aliases) if aliases else "无"
        character_text = "是" if row and int(row["is_character"]) == 1 else "否"
        lines = []
        if match_type == "exact_alias":
            lines.append(f"输入“{tag_name}”命中 alias，已归并到主 tag。")
        lines.append(
            f"tag：{canonical_tag_name}\n"
            f"可发送图片数：{count}\n"
            f"全部图片数：{all_count}\n"
            f"角色 tag：{character_text}\n"
            f"别名：{alias_text}"
        )
        yield event.plain_result("\n".join(lines))

    @pjsk_gallery.command("看图")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def show_image_detail(self, event: AstrMessageEvent, image_id: int):
        detail = self.db.get_image_detail(int(image_id))
        if not detail:
            yield event.plain_result(f"没有找到图片：#{int(image_id)}")
            return

        image_path = self._find_detail_image_path(detail, prefer_active=True)
        if image_path is None:
            image_path = self._find_trash_path(detail)
        if image_path is not None and image_path.exists():
            await event.send(MessageChain().file_image(str(image_path)))

        yield event.plain_result(self._build_image_detail_text(detail))

    @pjsk_gallery.command("别名添加")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def add_alias(self, event: AstrMessageEvent, tag_name: str, alias: str):
        canonical_tag_name, match_type = self._resolve_existing_tag_name(tag_name, allow_fuzzy=False)
        if not canonical_tag_name:
            yield event.plain_result(f"添加失败：没有找到 tag 或 alias：{tag_name}")
            return
        alias_values = self._parse_alias_csv(alias)
        if not alias_values:
            yield event.plain_result("添加失败：请提供至少一个 alias，可用逗号分隔多个别名。")
            return
        added, skipped = self._batch_add_aliases(canonical_tag_name, alias_values)
        lines = []
        if match_type == "exact_alias":
            lines.append(f"输入“{tag_name}”命中 alias，已归并到主 tag。")
        if added:
            lines.append("已添加别名：" + "、".join(added))
        if skipped:
            lines.append("以下别名未添加：" + "；".join(skipped[:10]))
        aliases = self.db.list_aliases(canonical_tag_name)
        lines.append(f"{canonical_tag_name} 当前别名：" + ("、".join(aliases) if aliases else "无"))
        yield event.plain_result("\n".join(lines))

    @pjsk_gallery.command("别名删除")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def remove_alias(self, event: AstrMessageEvent, tag_name: str, alias: str):
        canonical_tag_name, match_type = self._resolve_existing_tag_name(tag_name, allow_fuzzy=False)
        if not canonical_tag_name:
            yield event.plain_result(f"删除失败：没有找到 tag 或 alias：{tag_name}")
            return
        alias_values = self._parse_alias_csv(alias)
        if not alias_values:
            yield event.plain_result("删除失败：请提供至少一个 alias，可用逗号分隔多个别名。")
            return
        removed, skipped = self._batch_remove_aliases(canonical_tag_name, alias_values)
        lines = []
        if match_type == "exact_alias":
            lines.append(f"输入“{tag_name}”命中 alias，已归并到主 tag。")
        if removed:
            lines.append("已删除别名：" + "、".join(removed))
        if skipped:
            lines.append("以下别名未删除：" + "；".join(skipped[:10]))
        aliases = self.db.list_aliases(canonical_tag_name)
        lines.append(f"{canonical_tag_name} 当前别名：" + ("、".join(aliases) if aliases else "无"))
        yield event.plain_result("\n".join(lines))

    @pjsk_gallery.command("别名查看")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def list_aliases(self, event: AstrMessageEvent, tag_name: str):
        canonical_tag_name, match_type = self._resolve_existing_tag_name(tag_name, allow_fuzzy=False)
        if not canonical_tag_name:
            yield event.plain_result(f"没有找到 tag 或 alias：{tag_name}")
            return
        aliases = self.db.list_aliases(canonical_tag_name)
        lines = []
        if match_type == "exact_alias":
            lines.append(f"输入“{tag_name}”命中 alias，已归并到主 tag。")
        if not aliases:
            lines.append(f"tag “{canonical_tag_name}” 当前没有别名。")
            yield event.plain_result("\n".join(lines))
            return
        lines.append(f"{canonical_tag_name} 的别名：{'、'.join(aliases)}")
        yield event.plain_result("\n".join(lines))

    @pjsk_gallery.command("tag列表")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def list_tags_command(self, event: AstrMessageEvent, scope: str = ""):
        scope_text = str(scope or "").strip()
        lowered = scope_text.lower()
        character_only: bool | None = True
        keyword = ""
        if lowered in {"全部", "all"}:
            character_only = None
        elif lowered in {"普通", "非角色", "noise", "normal"}:
            character_only = False
        elif scope_text and lowered not in {"角色", "character"}:
            keyword = scope_text

        rows = self.db.list_tags(keyword=keyword, limit=200, character_only=character_only)
        if not rows:
            yield event.plain_result("当前没有符合条件的 tag。")
            return

        if character_only is True:
            header = "当前角色主 tag："
        elif character_only is False:
            header = "当前普通 tag（建议清理或先合并）："
        else:
            header = "当前全部主 tag："
        lines = [header]
        for row in rows[:60]:
            state = "角色" if int(row["is_character"] or 0) == 1 else "普通"
            lines.append(
                f"- {row['name']}（{state}，图 {int(row['image_count'] or 0)}，alias {int(row['alias_count'] or 0)}）"
            )
        if len(rows) > 60:
            lines.append(f"其余 {len(rows) - 60} 个未展开，可加关键词继续筛。")
        yield event.plain_result("\n".join(lines))

    @pjsk_gallery.command("tag合并")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def merge_tags_command(self, event: AstrMessageEvent, target_tag_name: str, source_tags: str):
        source_values = self._parse_alias_csv(source_tags)
        if not source_values:
            yield event.plain_result("用法：/pjsk图库 tag合并 <目标tag> <来源tag1,来源tag2>")
            return

        ok, summary = self.db.merge_tags(target_tag_name, source_values)
        if not ok and summary.get("message", "").startswith("目标 tag 不存在"):
            yield event.plain_result(f"合并失败：{summary['message']}")
            return
        if ok:
            self._sync_auto_crawl_subscriptions_safe()

        lines: list[str] = []
        target_name = str(summary.get("target_tag") or target_tag_name)
        if summary.get("target_match_type") == "exact_alias":
            lines.append(f"输入“{target_tag_name}”命中 alias，已归并到主 tag。")
        lines.append(str(summary.get("message") or ("已归并到主 tag：" + target_name)))
        merged_tags = list(summary.get("merged_tags") or [])
        aliases_added = list(summary.get("aliases_added") or [])
        skipped = list(summary.get("skipped") or [])
        aliases_skipped = list(summary.get("aliases_skipped") or [])
        if merged_tags:
            lines.append("已合并 tag：" + "、".join(merged_tags))
        if aliases_added:
            lines.append("已直接挂为 alias：" + "、".join(aliases_added))
        metrics = [
            f"图片关联迁移 {int(summary.get('image_links_migrated') or 0)}",
            f"审核任务迁移 {int(summary.get('review_tasks_migrated') or 0)}",
            f"审核任务合并 {int(summary.get('review_tasks_merged') or 0)}",
            f"alias 迁移 {int(summary.get('aliases_migrated') or 0)}",
            f"订阅迁移 {int(summary.get('subscriptions_migrated') or 0)}",
            f"订阅合并 {int(summary.get('subscriptions_merged') or 0)}",
            f"订阅移除 {int(summary.get('subscriptions_removed') or 0)}",
        ]
        lines.append("；".join(metrics))
        if aliases_skipped:
            lines.append("以下 alias 未迁移：" + "；".join(aliases_skipped[:10]))
        if skipped:
            lines.append("以下项未处理：" + "；".join(skipped[:10]))
        aliases = self.db.list_aliases(target_name)
        lines.append(f"{target_name} 当前别名：" + ("、".join(aliases) if aliases else "无"))
        yield event.plain_result("\n".join(lines))

    @pjsk_gallery.command("主tag切换")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def switch_primary_tag_command(self, event: AstrMessageEvent, tag_name_or_alias: str, new_primary_name: str):
        ok, summary = self.db.switch_primary_tag(tag_name_or_alias, new_primary_name)
        if not ok:
            yield event.plain_result(f"切换失败：{summary['message']}")
            return
        self._sync_auto_crawl_subscriptions_safe()

        lines = []
        if summary.get("match_type") == "exact_alias":
            lines.append(f"输入“{tag_name_or_alias}”命中 alias，已归并到主 tag。")
        lines.append(str(summary.get("message") or "已切换主 tag。"))
        aliases = self.db.list_aliases(str(summary.get("new_name") or new_primary_name))
        lines.append(
            f"当前主 tag：{summary.get('new_name')}\n"
            f"当前别名：{('、'.join(aliases) if aliases else '无')}"
        )
        yield event.plain_result("\n".join(lines))

    @pjsk_gallery.command("角色标记")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_character_tag(self, event: AstrMessageEvent, tag_name: str, is_character_text: str):
        value = str(is_character_text or "").strip().lower()
        is_character = value in {"1", "true", "yes", "y", "是"}
        ok, message = self.db.set_tag_character(tag_name, is_character)
        if ok:
            self._sync_auto_crawl_subscriptions_safe()
        yield event.plain_result(message if ok else f"设置失败：{message}")

    @pjsk_gallery.command("tag清理预览")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def preview_tag_cleanup(self, event: AstrMessageEvent):
        rows = self.db.preview_non_character_tag_cleanup(limit=200)
        if not rows:
            yield event.plain_result("当前没有普通 tag 需要清理。")
            return
        lines = [
            "以下普通 tag 清理后会被删除；如需保留，请先用 /pjsk图库 tag合并 归并到角色主 tag：",
        ]
        for row in rows[:60]:
            lines.append(f"- {row['name']}（图 {int(row['image_count'] or 0)}，alias {int(row['alias_count'] or 0)}）")
        if len(rows) > 60:
            lines.append(f"其余 {len(rows) - 60} 个未展开。")
        lines.append("执行命令：/pjsk图库 tag清理执行 确认")
        yield event.plain_result("\n".join(lines))

    @pjsk_gallery.command("tag清理执行")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def execute_tag_cleanup(self, event: AstrMessageEvent, confirm_text: str = ""):
        if str(confirm_text or "").strip().lower() not in {"确认", "confirm", "yes", "y"}:
            yield event.plain_result("该操作会删除所有普通 tag 及其图片关联。确认执行：/pjsk图库 tag清理执行 确认")
            return
        summary = self.db.cleanup_non_character_tags()
        self._sync_auto_crawl_subscriptions_safe()
        yield event.plain_result(
            "普通 tag 清理完成：\n"
            f"删除 tag {summary['tags_removed']} 个，"
            f"图片关联 {summary['image_links_removed']} 条，"
            f"审核任务 {summary['review_tasks_removed']} 条，"
            f"alias {summary['aliases_removed']} 条，"
            f"自动订阅 {summary['subscriptions_removed']} 条。"
        )

    @pjsk_gallery.command("采集添加")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def add_crawl_job(self, event: AstrMessageEvent, platform: str, source_url: str, tags_csv: str = ""):
        rules = parse_crawl_rule_text(tags_csv)
        try:
            job_id = await self.crawl_service.submit_job(
                platform,
                source_url,
                rules.manual_tags,
                include_tags=rules.include_tags,
                exclude_tags=rules.exclude_tags,
            )
        except Exception as exc:
            yield event.plain_result(f"创建采集任务失败：{exc}")
            return
        lines = [
            f"已创建采集任务 #{job_id}",
            f"平台：{platform}",
            f"链接：{source_url}",
            f"标签：{self._format_crawl_tags(rules.manual_tags, fallback='自动提取')}",
        ]
        if rules.include_tags:
            lines.append(f"包含采集：{self._format_crawl_tags(rules.include_tags)}")
        if rules.exclude_tags:
            lines.append(f"排除采集：{self._format_crawl_tags(rules.exclude_tags)}")
        yield event.plain_result("\n".join(lines))

    @pjsk_gallery.command("采集列表")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def list_crawl_jobs(self, event: AstrMessageEvent):
        rows = self.db.list_crawl_jobs(limit=10)
        if not rows:
            yield event.plain_result("当前没有采集任务。")
            return
        lines = ["最近采集任务："]
        for row in rows:
            job_rules = CrawlTagRules.from_db_row(row)
            rule_lines = [
                f"标签: {self._format_crawl_tags(job_rules.manual_tags, fallback='自动提取')}",
            ]
            if job_rules.include_tags:
                rule_lines.append(f"包含采集: {self._format_crawl_tags(job_rules.include_tags)}")
            if job_rules.exclude_tags:
                rule_lines.append(f"排除采集: {self._format_crawl_tags(job_rules.exclude_tags)}")
            if str(row["tag_match_mode"] or "exact") != "exact":
                rule_lines.append(f"标签匹配: {row['tag_match_mode']}")
            lines.append(
                "\n".join(
                    [
                        f"#{row['id']} [{row['status']}] {row['platform']} {row['progress']}%",
                        f"URL: {row['source_url']}",
                        *rule_lines,
                        f"结果: {row['result_summary'] or row['error_log'] or '-'}",
                    ]
                )
            )
        yield event.plain_result("\n\n".join(lines))

    @pjsk_gallery.command("采集诊断")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def crawl_diagnostics(self, event: AstrMessageEvent):
        job_counts = self.db.count_crawl_jobs_by_status()
        backfill_counts = self.db.count_pixiv_backfill_tasks_by_status()
        pixiv_subs = self.db.list_crawl_subscriptions(platform="pixiv", limit=1000)
        enabled_pixiv_subs = [row for row in pixiv_subs if int(row["enabled"] or 0) == 1]
        last_checked = max([str(row["last_checked_at"] or "") for row in pixiv_subs] or [""]) or "-"
        last_success = max([str(row["last_success_at"] or "") for row in pixiv_subs] or [""]) or "-"
        latest_job = self.db.get_latest_crawl_job()
        latest_failed = self.db.get_latest_crawl_job(statuses=("failed",))
        latest_subscription_error = self.db.get_latest_crawl_subscription_error(platform="pixiv")

        lines = [
            "采集诊断：",
            f"采集 worker：{'运行中' if self.crawl_service.worker_running() else '未运行'}，队列 {self.crawl_service.queue_size()}",
            f"Pixiv 自动采集：{'启用' if self.auto_crawl_service.enabled() else '未启用'} / {'运行中' if self.auto_crawl_service.running() else '未运行'}",
            f"Pixiv refresh token：{'已配置' if self.auto_crawl_service.has_refresh_token() else '未配置'}",
            f"Pixiv 自动订阅：启用 {len(enabled_pixiv_subs)} / 总计 {len(pixiv_subs)}",
            f"最近检查：{last_checked}",
            f"最近成功：{last_success}",
            f"采集任务状态：{self._format_status_counts(job_counts, ('pending', 'retry', 'running', 'failed', 'completed'))}",
            f"历史回填状态：{self._format_status_counts(backfill_counts, ('pending', 'retry', 'running', 'failed', 'completed'))}",
            f"历史回填 worker：{'运行中' if self.pixiv_backfill_service.worker_running() else '未运行'}，队列 {self.pixiv_backfill_service.queue_size()}",
        ]
        if latest_job:
            lines.append(
                "最近采集任务："
                + f" #{latest_job['id']} [{latest_job['status']}] {latest_job['platform']} "
                + f"{latest_job['progress']}% 更新 {latest_job['updated_at']}"
            )
        if latest_failed:
            lines.append("最近失败任务：\n" + self._format_crawl_job_brief(latest_failed))
        if latest_subscription_error:
            lines.append(
                "最近 Pixiv 自动采集错误："
                + f" #{latest_subscription_error['id']} {latest_subscription_error['tag_name']}\n"
                + f"   query：{latest_subscription_error['query_text'] or '-'}\n"
                + f"   错误：{self._short_text(str(latest_subscription_error['last_error'] or ''), 180) or '-'}\n"
                + f"   更新：{latest_subscription_error['updated_at']}"
            )
        lines.append("失败任务可用 /pp 失败列表 查看，或 /pp 失败重试 <job_id> 重新入队。")
        yield event.plain_result("\n".join(lines))

    @pjsk_gallery.command("失败列表")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def list_failed_crawl_jobs_command(self, event: AstrMessageEvent, platform: str = ""):
        platform_text = str(platform or "").strip().lower()
        if platform_text in {"全部", "all", "*"}:
            platform_text = ""
        rows = self.db.list_failed_crawl_jobs(platform=platform_text, limit=10)
        if not rows:
            scope = f" {platform_text}" if platform_text else ""
            yield event.plain_result(f"当前没有{scope}失败采集任务。")
            return
        lines = ["最近失败采集任务："]
        for index, row in enumerate(rows, start=1):
            lines.append(self._format_crawl_job_brief(row, index=index))
        lines.append("可用 /pp 失败重试 <job_id> 或 /pp 失败重试 全部。")
        yield event.plain_result("\n\n".join(lines))

    @pjsk_gallery.command("失败重试")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def retry_failed_crawl_jobs_command(self, event: AstrMessageEvent, job_id: str):
        target = str(job_id or "").strip().lower()
        if target in {"全部", "all", "*"}:
            rows = self.db.list_failed_crawl_jobs(limit=20)
            if not rows:
                yield event.plain_result("当前没有失败采集任务可重试。")
                return
            ok_count = 0
            failed: list[str] = []
            for row in rows:
                ok, message = await self.crawl_service.retry_job(int(row["id"]))
                if ok:
                    ok_count += 1
                else:
                    failed.append(message)
            lines = [f"已重新入队 {ok_count} 个失败采集任务。"]
            if failed:
                lines.append("失败：" + "；".join(failed[:5]))
            yield event.plain_result("\n".join(lines))
            return
        try:
            numeric_job_id = int(target)
        except ValueError:
            yield event.plain_result("用法：/pp 失败重试 <job_id|全部>")
            return
        ok, message = await self.crawl_service.retry_job(numeric_job_id)
        yield event.plain_result(message if ok else f"重试失败：{message}")

    @pjsk_gallery.command("自动采集状态")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def auto_crawl_status(self, event: AstrMessageEvent):
        stats = self.db.get_stats()
        rows = self.db.list_crawl_subscriptions(platform="pixiv", enabled_only=True, limit=200)
        yield event.plain_result(
            "Pixiv 自动采集状态：\n"
            f"已启用：{'是' if self.auto_crawl_service.enabled() else '否'}\n"
            f"已配置 refresh token：{'是' if self.auto_crawl_service.has_refresh_token() else '否'}\n"
            f"角色 tag 限定：{'是' if self.auto_crawl_service.character_only() else '否'}\n"
            f"检索词后缀：{self.config.get('pixiv_auto_crawl_query_suffix', 'user') or 'user'}\n"
            f"轮询间隔：{self.auto_crawl_service.interval_minutes()} 分钟\n"
            f"自动订阅数：{len(rows)} / 统计 {stats['crawl_subscriptions']}\n"
            f"单轮最多新任务：{self.auto_crawl_service.max_new_jobs_per_cycle()}\n"
            f"每个 tag 最多检查：{self.auto_crawl_service.max_results_per_tag()} 条结果"
        )

    @pjsk_gallery.command("自动采集列表")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def auto_crawl_list(self, event: AstrMessageEvent):
        rows = self.db.list_crawl_subscriptions(platform="pixiv", enabled_only=True, limit=20)
        if not rows:
            yield event.plain_result("当前没有启用中的 Pixiv 自动采集订阅。")
            return
        lines = ["当前 Pixiv 自动采集订阅："]
        for row in rows:
            lines.append(
                "\n".join(
                    [
                        f"#{row['id']} {row['tag_name']}",
                        f"query: {row['query_text'] or '-'}",
                        f"last_seen: {row['last_seen_source_uid'] or '-'}",
                        f"last_checked: {row['last_checked_at'] or '-'}",
                        f"last_success: {row['last_success_at'] or '-'}",
                        f"last_error: {row['last_error'] or '-'}",
                    ]
                )
            )
        yield event.plain_result("\n\n".join(lines))

    @pjsk_gallery.command("自动采集执行")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def run_auto_crawl_once(self, event: AstrMessageEvent):
        summary = await self.auto_crawl_service.run_once(force=True)
        yield event.plain_result(
            "Pixiv 自动采集执行完成：\n"
            f"订阅 {summary['subscriptions']} 个，检查 {summary['checked']} 个，"
            f"命中过滤 {summary['matched']} 个，入队 {summary['queued']} 个，"
            f"已存在跳过 {summary['skipped_existing']} 个，"
            f"过滤跳过 {summary['skipped_filtered']} 个，错误 {summary['errors']} 个。"
        )

    @pjsk_gallery.command("历史回填添加")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def add_pixiv_backfill_task(
        self,
        event: AstrMessageEvent,
        tag_text: str,
        max_pages: int = 20,
        max_results: int = 200,
        max_new_jobs: int = 100,
    ):
        try:
            task_id, info = await self.pixiv_backfill_service.create_task(
                tag_text=tag_text,
                max_pages=max_pages,
                max_results=max_results,
                max_new_jobs=max_new_jobs,
            )
        except Exception as exc:
            yield event.plain_result(f"创建 Pixiv 历史回填任务失败：{exc}")
            return
        resolved = info.get("resolved_tag") or {}
        yield event.plain_result(
            "已创建 Pixiv 历史回填任务：\n"
            f"#{task_id} {tag_text} -> {resolved.get('name') or tag_text}\n"
            f"搜索词：{'、'.join(info.get('query_terms') or []) or tag_text}\n"
            f"页数上限：{max_pages}，扫描上限：{max_results}，入队上限：{max_new_jobs}"
        )

    @pjsk_gallery.command("历史回填列表")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def list_pixiv_backfill_tasks(self, event: AstrMessageEvent):
        rows = self.db.list_pixiv_backfill_tasks(limit=10)
        if not rows:
            yield event.plain_result("当前没有 Pixiv 历史回填任务。")
            return
        lines = ["最近 Pixiv 历史回填任务："]
        for row in rows:
            lines.append(
                "\n".join(
                    [
                        f"#{row['id']} [{row['status']}] {row['tag_text'] or row['tag_name']} -> {row['tag_name']}",
                        f"当前：{row['current_query_text'] or '-'} 第 {row['current_page'] or 0}/{row['max_pages']} 页",
                        f"扫描 {row['scanned']}，匹配 {row['matched']}，入队 {row['queued']}",
                        f"跳过：已存在 {row['skipped_existing']}，已拒绝 {row['skipped_rejected']}，过滤 {row['skipped_filtered']}，重复 {row['skipped_duplicate']}",
                        f"错误：{row['error_log'] or '-'}",
                    ]
                )
            )
        yield event.plain_result("\n\n".join(lines))

    @pjsk_gallery.command("采集重试")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def retry_crawl_job(self, event: AstrMessageEvent, job_id: int):
        ok, message = await self.crawl_service.retry_job(int(job_id))
        yield event.plain_result(message if ok else f"重试失败：{message}")

    @pjsk_gallery.command("审图帮助")
    async def show_qq_review_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "\n".join(
                [
                    "PJSK 群友审图命令：",
                    "/pp 随机审核 [候选tag]：随机领取一张 Pixiv 待审图",
                    "/pp 审图通过 <最终tag>：归入指定现有主 tag",
                    "/pp 审图拒绝 [原因]：整图拒绝并阻止以后重复抓取",
                    "/pp 审图跳过：不修改审核结果并换一张",
                    "/pp 审图当前：重发当前领取的图片",
                    "/pp 审图结束：释放当前图片并退出",
                    "如果图片有价值但候选 tag 错了，请用“审图通过 正确tag”，不要整图拒绝。",
                ]
            )
        )

    @pjsk_gallery.command("随机审核", alias={"抽审"})
    async def claim_random_qq_review(self, event: AstrMessageEvent, candidate_tag: str = ""):
        if not self._qq_review_enabled():
            yield event.plain_result("群友审图当前未启用。")
            return
        filter_tag_id = 0
        filter_tag_name = ""
        query = str(candidate_tag or "").strip()
        if query:
            resolved, _, candidates = self._resolve_qq_review_tag(query)
            if not resolved:
                hint = f"你想找的是不是：{'、'.join(candidates)}" if candidates else "请使用已经存在的主 tag、alias 或 Pixiv 平台词。"
                yield event.plain_result(f"没有精确找到候选 tag“{query}”。{hint}")
                return
            row = self.db.get_tag_row(resolved)
            if row is None:
                yield event.plain_result(f"候选 tag 不存在：{resolved}")
                return
            filter_tag_id = int(row["id"])
            filter_tag_name = str(row["name"])
        await self._claim_and_send_qq_review(
            event,
            filter_tag_id=filter_tag_id,
            filter_tag_name=filter_tag_name,
            replace_current=True,
        )

    @pjsk_gallery.command("审图通过")
    async def approve_current_qq_review(self, event: AstrMessageEvent, tag_name: str = ""):
        if not self._qq_review_enabled():
            yield event.plain_result("群友审图当前未启用。")
            return
        query = str(tag_name or "").strip()
        if not query:
            yield event.plain_result("请指定最终 tag，例如：/pp 审图通过 晓山瑞希")
            return
        resolved, match_type, candidates = self._resolve_qq_review_tag(query)
        if not resolved:
            hint = f"你想选的是不是：{'、'.join(candidates)}" if candidates else "请先在图库中建立这个主 tag。"
            yield event.plain_result(f"没有精确找到 tag“{query}”，未提交审核。{hint}")
            return
        origin, reviewer_id = self._qq_review_identity(event)
        ok, result = await self.qq_review_service.approve_current(
            origin=origin,
            reviewer_id=reviewer_id,
            tag_name=resolved,
        )
        if not ok:
            await event.send(MessageChain().message(f"处理失败：{result.get('message') or '未知错误'}"))
            if result.get("code") == "stale_session" and self._qq_review_auto_next():
                await self._claim_and_send_qq_review(event)
            return
        await event.send(
            MessageChain().message(
                f"已通过图片 #{int(result.get('image_id', 0) or 0)}，归入 {resolved}"
                + (f"（{match_type}）" if match_type else "")
            )
        )
        if self._qq_review_auto_next():
            await self._claim_and_send_qq_review(
                event,
                filter_tag_id=int(result.get("filter_tag_id", 0) or 0),
                filter_tag_name=str(result.get("filter_tag_name", "") or ""),
            )

    @pjsk_gallery.command("审图拒绝")
    async def reject_current_qq_review(self, event: AstrMessageEvent, reason: str = ""):
        if not self._qq_review_enabled():
            yield event.plain_result("群友审图当前未启用。")
            return
        origin, reviewer_id = self._qq_review_identity(event)
        ok, result = await self.qq_review_service.reject_current(
            origin=origin,
            reviewer_id=reviewer_id,
            reason=reason,
        )
        if not ok:
            await event.send(MessageChain().message(f"处理失败：{result.get('message') or '未知错误'}"))
            if result.get("code") == "stale_session" and self._qq_review_auto_next():
                await self._claim_and_send_qq_review(event)
            return
        await event.send(
            MessageChain().message(
                f"已整图拒绝图片 #{int(result.get('image_id', 0) or 0)}；该 Pixiv 作品以后不会再次进入采集队列。"
            )
        )
        if self._qq_review_auto_next():
            await self._claim_and_send_qq_review(
                event,
                filter_tag_id=int(result.get("filter_tag_id", 0) or 0),
                filter_tag_name=str(result.get("filter_tag_name", "") or ""),
            )

    @pjsk_gallery.command("审图跳过")
    async def skip_current_qq_review(self, event: AstrMessageEvent):
        if not self._qq_review_enabled():
            yield event.plain_result("群友审图当前未启用。")
            return
        origin, reviewer_id = self._qq_review_identity(event)
        session = await self.qq_review_service.release_current(
            origin=origin,
            reviewer_id=reviewer_id,
            remember=True,
        )
        if session is None:
            yield event.plain_result("当前没有领取中的审核图片，请先发送 /pp 随机审核。")
            return
        await event.send(MessageChain().message(f"已跳过图片 #{session.image_id}，审核状态未改变。"))
        await self._claim_and_send_qq_review(
            event,
            filter_tag_id=session.filter_tag_id,
            filter_tag_name=session.filter_tag_name,
        )

    @pjsk_gallery.command("审图当前")
    async def show_current_qq_review(self, event: AstrMessageEvent):
        if not self._qq_review_enabled():
            yield event.plain_result("群友审图当前未启用。")
            return
        origin, reviewer_id = self._qq_review_identity(event)
        session = await self.qq_review_service.get_current(origin=origin, reviewer_id=reviewer_id)
        if session is None:
            yield event.plain_result("当前没有领取中的审核图片，请先发送 /pp 随机审核。")
            return
        remaining = self.db.count_open_pixiv_review_images(
            statuses=QQReviewSessionService.OPEN_STATUSES,
            candidate_tag_id=session.filter_tag_id or None,
        )
        if not await self._send_qq_review_session(event, session, remaining=remaining):
            await self.qq_review_service.release_current(origin=origin, reviewer_id=reviewer_id, remember=True)
            yield event.plain_result("当前图片文件不可用，已释放领取；请重新发送 /pp 随机审核。")

    @pjsk_gallery.command("审图结束")
    async def end_current_qq_review(self, event: AstrMessageEvent):
        origin, reviewer_id = self._qq_review_identity(event)
        session = await self.qq_review_service.release_current(
            origin=origin,
            reviewer_id=reviewer_id,
            remember=False,
        )
        if session is None:
            yield event.plain_result("当前没有进行中的群友审图会话。")
            return
        yield event.plain_result(f"已结束审图并释放图片 #{session.image_id}。")

    @pjsk_gallery.command("审核列表")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def list_review_tasks(self, event: AstrMessageEvent, status: str = ""):
        status_text = str(status or "").strip().lower()
        statuses = list(self.OPEN_REVIEW_STATUSES) if not status_text else None
        wanted = None if not status_text or status_text in {"all", "全部"} else status_text
        rows = self.db.list_review_tasks(status=wanted, statuses=statuses, limit=10)
        if not rows:
            yield event.plain_result("当前没有审核任务。")
            return
        grouped: dict[int, list[dict]] = {}
        ordered_image_ids: list[int] = []
        for row in rows:
            item = dict(row)
            image_id = int(item["image_id"])
            if image_id not in grouped:
                grouped[image_id] = []
                ordered_image_ids.append(image_id)
            grouped[image_id].append(item)

        await event.send(
            MessageChain().message(
                (
                    "当前待处理审核任务："
                    if statuses
                    else "最近审核任务："
                )
                + f"{len(rows)} 条，涉及 {len(ordered_image_ids)} 张图片。"
            ),
        )

        preview_limit = min(5, len(ordered_image_ids))
        for index, image_id in enumerate(ordered_image_ids[:preview_limit], start=1):
            await self._send_review_group_preview(event, grouped.get(image_id, []), display_index=index)

        if len(ordered_image_ids) > preview_limit:
            yield event.plain_result(
                f"其余 {len(ordered_image_ids) - preview_limit} 张图片未展开。可用 /pjsk图库 审核查看 <review_id> 查看单条审核。",
            )
            return
        yield event.plain_result("可继续使用 /pjsk图库 审核查看 <review_id> 查看单条审核。")

    @pjsk_gallery.command("审核查看")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def show_review_task(self, event: AstrMessageEvent, review_id: int = 0):
        task = self.db.get_review_task(int(review_id)) if int(review_id or 0) > 0 else None
        if task is None:
            rows = self.db.list_review_tasks(statuses=self.OPEN_REVIEW_STATUSES, limit=1)
            task = rows[0] if rows else None
        if task is None:
            yield event.plain_result("当前没有待处理审核图片。")
            return
        await self._send_review_task_detail(event, task)

    @pjsk_gallery.command("审核通过")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def approve_review_task(self, event: AstrMessageEvent, review_id: int):
        ok, message = self.db.apply_manual_review(int(review_id), approved=True)
        if ok:
            await event.send(MessageChain().message(message))
            await self._send_next_open_review_task(event, exclude_review_ids={int(review_id)})
            return
        yield event.plain_result(f"处理失败：{message}")

    @pjsk_gallery.command("审核拒绝")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def reject_review_task(self, event: AstrMessageEvent, review_id: int):
        ok, message = self.db.apply_manual_review(int(review_id), approved=False)
        if ok:
            await event.send(MessageChain().message(message))
            await self._send_next_open_review_task(event, exclude_review_ids={int(review_id)})
            return
        yield event.plain_result(f"处理失败：{message}")

    @pjsk_gallery.command("投稿审核状态")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def submission_review_status(self, event: AstrMessageEvent):
        enabled = self._submission_review_enabled()
        yield event.plain_result(
            "投稿审核当前状态："
            + ("开启\n新投稿会进入审核链路。" if enabled else "关闭\n新投稿会默认直接入库并可参与发图。")
        )

    @pjsk_gallery.command("投稿审核开启")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def enable_submission_review(self, event: AstrMessageEvent):
        ok, message = self._set_submission_review_enabled(True)
        if not ok:
            yield event.plain_result(message)
            return
        yield event.plain_result("投稿审核已开启；后续新投稿会进入审核链路。")

    @pjsk_gallery.command("投稿审核关闭")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def disable_submission_review(self, event: AstrMessageEvent):
        ok, message = self._set_submission_review_enabled(False)
        if not ok:
            yield event.plain_result(message)
            return
        yield event.plain_result("投稿审核已关闭；后续新投稿将默认直接入库并可参与发图。")

    @pjsk_gallery.command("删图")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def delete_image(self, event: AstrMessageEvent, image_id: int):
        image_id = int(image_id)
        detail = self.db.get_image_detail(image_id)
        if not detail:
            yield event.plain_result(f"删图失败：没有找到图片 #{image_id}")
            return

        image = dict(detail.get("image") or {})
        if int(image.get("is_active") or 0) != 1:
            yield event.plain_result(f"图片 #{image_id} 当前已不在可发送状态；可用 /pjsk图库 看图 {image_id} 查看详情。")
            return

        current_path = self._find_detail_image_path(detail, prefer_active=True)
        tag_names = self._collect_display_tag_names(list(detail.get("tags") or []), sendable_only=False)
        tag_text = "、".join(tag_names) if tag_names else "-"

        if current_path is None:
            ok, message = self.db.trash_image(image_id, trash_path=None)
            yield event.plain_result(
                (f"已仅在数据库中禁用图片 #{image_id}\n"
                 f"tag：{tag_text}\n"
                 f"原因：原文件不存在，无法移入回收站。")
                if ok else f"删图失败：{message}"
            )
            return

        trash_path = self._build_trash_destination(image_id, current_path)
        try:
            await asyncio.to_thread(shutil.move, str(current_path), str(trash_path))
        except Exception as exc:
            yield event.plain_result(f"删图失败：移动到回收站失败：{exc}")
            return

        ok, message = self.db.trash_image(image_id, trash_path=str(trash_path))
        if not ok:
            try:
                if trash_path.exists():
                    trash_path.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(shutil.move, str(trash_path), str(current_path))
            except Exception:
                logger.error(f"[PJSKPic] 删图回滚失败: image_id={image_id}, trash={trash_path}, original={current_path}", exc_info=True)
            yield event.plain_result(f"删图失败：{message}")
            return

        yield event.plain_result(
            f"已删除图片 #{image_id}\n"
            f"tag：{tag_text}\n"
            f"原路径：{current_path}\n"
            f"回收站：{trash_path}"
        )

    @pjsk_gallery.command("恢复图")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def restore_image(self, event: AstrMessageEvent, image_id: int):
        image_id = int(image_id)
        detail = self.db.get_image_detail(image_id)
        if not detail:
            yield event.plain_result(f"恢复失败：没有找到图片 #{image_id}")
            return

        image = dict(detail.get("image") or {})
        if int(image.get("is_active") or 0) == 1:
            yield event.plain_result(f"图片 #{image_id} 当前已经是可发送状态。")
            return

        trash_path = self._find_trash_path(detail)
        if trash_path is None:
            yield event.plain_result(f"恢复失败：图片 #{image_id} 在回收站中没有找到可恢复文件。")
            return

        restore_path = self._build_restore_destination(detail, trash_path)
        try:
            await asyncio.to_thread(shutil.move, str(trash_path), str(restore_path))
        except Exception as exc:
            yield event.plain_result(f"恢复失败：移动回原位置失败：{exc}")
            return

        ok, message = self.db.restore_image(image_id, restored_path=str(restore_path), trash_path=str(trash_path))
        if not ok:
            try:
                if restore_path.exists():
                    trash_path.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(shutil.move, str(restore_path), str(trash_path))
            except Exception:
                logger.error(f"[PJSKPic] 恢复图片回滚失败: image_id={image_id}, trash={trash_path}, restored={restore_path}", exc_info=True)
            yield event.plain_result(f"恢复失败：{message}")
            return

        yield event.plain_result(
            f"已恢复图片 #{image_id}\n"
            f"恢复路径：{restore_path}\n"
            f"可用命令：/pjsk图库 看图 {image_id}"
        )

    @pjsk_gallery.command("重复忽略")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def ignore_duplicate_pair(self, event: AstrMessageEvent, image_id1: int, image_id2: int, reason: str = ""):
        ok, message = self.db.add_similarity_ignore(int(image_id1), int(image_id2), reason)
        if not ok:
            yield event.plain_result(f"重复忽略失败：{message}")
            return
        suffix = f"\n原因：{reason}" if str(reason or "").strip() else ""
        yield event.plain_result(f"{message}{suffix}\n后续投稿 / 采集疑似重复提示会过滤这对图片。")

    @pjsk_gallery.command("重复恢复")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def restore_duplicate_pair_warning(self, event: AstrMessageEvent, image_id1: int, image_id2: int):
        ok, message = self.db.remove_similarity_ignore(int(image_id1), int(image_id2))
        yield event.plain_result(message if ok else f"重复恢复失败：{message}")

    @pjsk_gallery.command("重复忽略列表")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def list_duplicate_pair_ignores(self, event: AstrMessageEvent, image_id: int = 0):
        rows = self.db.list_similarity_ignores(int(image_id or 0) or None, limit=20)
        if not rows:
            scope = f" #{int(image_id)}" if int(image_id or 0) > 0 else ""
            yield event.plain_result(f"当前没有{scope}相关的重复忽略记录。")
            return
        lines = ["重复忽略记录："]
        for row in rows:
            reason = str(row["reason"] or "").strip()
            lines.append(
                f"#{row['id']}：{row['image_id_low']} <-> {row['image_id_high']}"
                + (f"；原因：{reason}" if reason else "")
            )
        lines.append("可用 /pp 重复恢复 <id1> <id2> 恢复疑似重复提示。")
        yield event.plain_result("\n".join(lines))

    @pjsk_gallery.command("面板地址")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def show_webui_address(self, event: AstrMessageEvent):
        if not self._webui_enabled():
            yield event.plain_result("独立 WebUI 当前已禁用。")
            return
        urls = self.webui.get_access_urls()
        if not urls:
            yield event.plain_result("独立 WebUI 当前未启动。")
            return
        lines = ["PJSK 独立 WebUI 地址：", *urls]
        if self._webui_access_token():
            lines.append("当前已启用访问令牌。")
        else:
            lines.append("当前未配置访问令牌；若开放局域网访问，请注意安全。")
        yield event.plain_result("\n".join(lines))

    @staticmethod
    def _format_crawl_tags(tags: list[str], *, fallback: str = "-") -> str:
        return "、".join(str(tag).strip() for tag in tags if str(tag).strip()) or fallback

    @staticmethod
    def _build_gallery_help_text(section: str = "") -> str:
        key = str(section or "").strip().lower()
        aliases = {
            "投稿": "submission",
            "tg": "submission",
            "submit": "submission",
            "submission": "submission",
            "tag": "tag",
            "标签": "tag",
            "别名": "tag",
            "alias": "tag",
            "审核": "review",
            "review": "review",
            "采集": "crawl",
            "抓图": "crawl",
            "crawl": "crawl",
            "pixiv": "crawl",
        }
        topic = aliases.get(key, "")
        if topic == "submission":
            return "\n".join(
                [
                    "PJSK 投稿命令：",
                    "/投稿 <tag> 或 /tg <tag>：附图投稿到指定 tag",
                    "/投稿 <tag> 别名 <alias1,alias2>：投稿时顺手补 alias",
                    "/tg <tag> alias <alias1,alias2>：同上",
                    "也可以先回复一条带图消息，再发送投稿命令。",
                ]
            )
        if topic == "tag":
            return "\n".join(
                [
                    "PJSK tag / alias 管理：",
                    "/pp 查看 <tag>：查看图片数和别名",
                    "/pp tag列表 [全部|普通|关键词]：列出主 tag",
                    "/pp 别名添加 <tag> <alias1,alias2>",
                    "/pp 别名删除 <tag> <alias1,alias2>",
                    "/pp tag合并 <目标tag> <来源tag1,来源tag2>",
                    "/pp 主tag切换 <旧tag或alias> <新主tag>",
                ]
            )
        if topic == "review":
            return "\n".join(
                [
                    "PJSK 审核命令：",
                    "/pp 随机审核 [候选tag]：群友随机领取 Pixiv 待审图",
                    "/pp 审图通过 <最终tag> / 审图拒绝 [原因] / 审图跳过",
                    "/pp 审图当前 / 审图结束 / 审图帮助",
                    "/pp 审核列表 [status]：查看最近审核任务",
                    "/pp 审核查看 [review_id]：查看单条或下一条待审",
                    "/pp 审核通过 <review_id>",
                    "/pp 审核拒绝 <review_id>",
                    "/pp 投稿审核状态",
                    "/pp 投稿审核开启 或 /pp 投稿审核关闭",
                ]
            )
        if topic == "crawl":
            return "\n".join(
                [
                    "PJSK 采集命令：",
                    "/pp 采集添加 <platform> <url> [tags_csv]",
                    "/pp 采集列表",
                    "/pp 采集诊断",
                    "/pp 失败列表 [platform]",
                    "/pp 失败重试 <job_id|全部>",
                    "/pp 自动采集状态",
                    "/pp 历史回填添加 <tag> [页数上限] [扫描上限] [入队上限]",
                ]
            )
        return "\n".join(
            [
                "PJSK 图库常用命令：",
                "发图：看看初音未来 / 来张 miku / 看看id123",
                "投稿：/tg <tag>，可用 /pp 帮助 投稿 查看 alias 写法",
                "群友审图：/pp 随机审核，可用 /pp 审图帮助 查看完整流程",
                "管理：/pp 统计、/pp 查看 <tag>、/pp 看图 <image_id>",
                "面板：/pp 面板地址",
                "分组帮助：/pp 帮助 投稿 / tag / 审核 / 采集",
                "完整维护操作建议优先使用 WebUI。",
            ]
        )
