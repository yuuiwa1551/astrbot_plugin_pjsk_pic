from __future__ import annotations

import asyncio
import re
import urllib.parse

from .common import BaseCrawlAdapter
from ..models import CrawlCandidate
from ..xhs_provider import (
    XhsProviderClient,
    XhsProviderError,
    canonical_xhs_post_url,
    xhs_note_detail_from_snapshot,
)


NOTE_ID_PATTERN = re.compile(r"/(?:explore|discovery/item|note)/([0-9a-zA-Z]+)")


class XiaohongshuAdapter(BaseCrawlAdapter):
    def __init__(
        self,
        config: dict | None = None,
        *,
        provider_client: XhsProviderClient | None = None,
    ) -> None:
        super().__init__("xiaohongshu", config=config)
        self.provider_client = provider_client or XhsProviderClient(self.config)

    async def fetch_candidates(
        self,
        source_url: str,
        *,
        max_candidates: int = 8,
        timeout_seconds: int = 20,
        source_context: dict | None = None,
    ) -> list[CrawlCandidate]:
        # The structured provider is deliberately authoritative. Falling back to
        # generic page regexes can turn login/risk-control HTML into fake images.
        del max_candidates
        context = source_context if isinstance(source_context, dict) else {}
        note_id = str(context.get("note_id", "") or self.extract_source_uid(source_url, "")).strip()
        xsec_token = str(context.get("xsec_token", "") or self._token_from_url(source_url)).strip()
        if not note_id or not xsec_token:
            raise XhsProviderError(
                "小红书结构化详情缺少 note_id 或 xsec_token，不能回退到网页正则",
                category="configuration",
            )
        snapshot = context.get("detail_snapshot")
        if isinstance(snapshot, dict):
            detail = xhs_note_detail_from_snapshot(snapshot)
        else:
            detail = await asyncio.to_thread(
                self.provider_client.fetch_note_detail,
                note_id,
                xsec_token,
                timeout_seconds=timeout_seconds,
            )
        safety_limit = self._image_safety_limit()
        if len(detail.images) > safety_limit:
            raise XhsProviderError(
                f"小红书笔记返回 {len(detail.images)} 张图片，超过安全上限 {safety_limit}，已停止任务等待检查",
                category="response_too_large",
                pause_required=True,
            )
        if not detail.images:
            raise XhsProviderError(
                "小红书图文详情没有可下载图片",
                category="empty_note",
            )

        candidates: list[CrawlCandidate] = []
        page_count = len(detail.images)
        for image in detail.images:
            candidates.append(
                CrawlCandidate(
                    platform=self.platform,
                    post_url=canonical_xhs_post_url(detail.note_id),
                    normalized_post_url=canonical_xhs_post_url(detail.note_id),
                    source_uid=detail.note_id,
                    image_url=image.url,
                    raw_tags=list(detail.topics),
                    author=detail.author,
                    title=detail.title,
                    extra={
                        "adapter": "xiaohongshu",
                        "via": str(context.get('provider') or f"{self.config.get('xhs_provider_kind', 'xiaohongshu_mcp')}_rest"),
                        "source_id": detail.note_id,
                        "description": detail.description,
                        "published_at_ms": detail.published_at_ms,
                        "page_index": image.index,
                        "page_count": page_count,
                        "reported_width": image.width,
                        "reported_height": image.height,
                        "require_image_mime": True,
                        "request_headers": self.image_request_headers(detail.post_url, image.url),
                    },
                )
            )
        return candidates

    def _image_safety_limit(self) -> int:
        raw = self.config.get("xhs_max_images_per_note", 60)
        try:
            return min(max(int(raw or 60), 1), 100)
        except (TypeError, ValueError):
            return 60

    @staticmethod
    def _token_from_url(source_url: str) -> str:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(str(source_url or "")).query)
        values = query.get("xsec_token") or query.get("xsecToken") or []
        return str(values[0] if values else "").strip()

    def cookie_string(self) -> str:
        # Kept only for backward-compatible config introspection. Structured
        # collection never copies cookies into the plugin process.
        return ""

    def image_request_headers(self, source_url: str, image_url: str) -> dict[str, str]:
        del image_url
        return {"Referer": source_url or "https://www.xiaohongshu.com/"}

    def extract_source_uid(self, final_url: str, html: str) -> str:
        match = NOTE_ID_PATTERN.search(str(final_url or ""))
        if match:
            return match.group(1)
        legacy = re.search(r'"noteId"\s*:\s*"([^"]+)"', str(html or ""))
        return legacy.group(1) if legacy else ""
