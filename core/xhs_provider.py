from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import requests


XHS_POST_BASE_URL = "https://www.xiaohongshu.com/explore"
XHS_ALLOWED_IMAGE_HOST_SUFFIXES = (".xhscdn.com",)
XHS_ALLOWED_IMAGE_HOSTS = {"ci.xiaohongshu.com"}
XHS_TOPIC_PATTERN = re.compile(r"[#＃]\s*([^#＃\r\n]{1,60}?)\s*\[话题\]\s*[#＃]")
XHS_HASHTAG_PATTERN = re.compile(r"[#＃]([0-9A-Za-z_\-\u4e00-\u9fff\u3040-\u30ff]{1,60})")


class XhsProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: str = "unknown",
        retryable: bool = False,
        pause_required: bool = False,
        status_code: int | None = None,
        provider_code: str = "",
    ) -> None:
        super().__init__(message)
        self.category = str(category or "unknown")
        self.retryable = bool(retryable)
        self.pause_required = bool(pause_required)
        self.status_code = status_code
        self.provider_code = str(provider_code or "")


@dataclass(frozen=True, slots=True)
class XhsImageRef:
    url: str
    index: int
    width: int = 0
    height: int = 0


@dataclass(frozen=True, slots=True)
class XhsSearchHit:
    note_id: str
    xsec_token: str
    post_url: str
    title: str = ""
    author: str = ""
    note_type: str = "normal"
    position: int = 0


@dataclass(frozen=True, slots=True)
class XhsNoteDetail:
    note_id: str
    xsec_token: str
    post_url: str
    title: str = ""
    description: str = ""
    author: str = ""
    note_type: str = "normal"
    published_at_ms: int = 0
    topics: list[str] = field(default_factory=list)
    images: list[XhsImageRef] = field(default_factory=list)

    def match_texts(self) -> list[str]:
        return [self.title, self.description, *self.topics]


def canonical_xhs_post_url(note_id: str) -> str:
    value = str(note_id or "").strip()
    if not value:
        return ""
    return f"{XHS_POST_BASE_URL}/{value}"


def extract_xhs_topics(text: str) -> list[str]:
    source = str(text or "")
    result: list[str] = []
    seen: set[str] = set()

    def append(value: str) -> None:
        cleaned = re.sub(r"\s+", " ", str(value or "")).strip().strip("#＃")
        key = cleaned.casefold()
        if not cleaned or len(cleaned) > 60 or key in seen:
            return
        seen.add(key)
        result.append(cleaned)

    for match in XHS_TOPIC_PATTERN.finditer(source):
        append(match.group(1))
    for match in XHS_HASHTAG_PATTERN.finditer(source):
        append(match.group(1))
    return result[:40]


def normalize_xhs_image_url(raw_url: str) -> str:
    value = str(raw_url or "").strip()
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        raise XhsProviderError(
            "小红书详情返回了非 HTTP(S) 图片地址",
            category="unsafe_image_url",
            pause_required=True,
        )
    if parsed.username or parsed.password:
        raise XhsProviderError(
            "小红书详情返回了包含用户信息的图片地址",
            category="unsafe_image_url",
            pause_required=True,
        )
    allowed = host in XHS_ALLOWED_IMAGE_HOSTS or any(
        host.endswith(suffix) for suffix in XHS_ALLOWED_IMAGE_HOST_SUFFIXES
    )
    if not host or not allowed:
        raise XhsProviderError(
            f"小红书详情返回了未授权图片域名：{host or '-'}",
            category="unsafe_image_url",
            pause_required=True,
        )
    netloc = parsed.netloc
    if parsed.scheme == "http" and parsed.port == 80:
        netloc = host
    return urllib.parse.urlunsplit(("https", netloc, parsed.path, parsed.query, ""))


class XhsProviderClient:
    """Low-rate, serialized REST client for the pinned xiaohongshu-mcp provider."""

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        session: requests.Session | None = None,
        sleep_func: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or {}
        self.session = session or requests.Session()
        self.sleep_func = sleep_func
        self.clock = clock
        self._request_lock = threading.RLock()
        self._last_request_at = 0.0

    def base_url(self) -> str:
        value = str(
            self.config.get("xhs_provider_base_url", "http://pjsk-xhs-provider:18060")
            or "http://pjsk-xhs-provider:18060"
        ).strip()
        parsed = urllib.parse.urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            raise XhsProviderError(
                "小红书提供者地址无效",
                category="configuration",
            )
        return value.rstrip("/")

    def access_token(self) -> str:
        return str(self.config.get("xhs_provider_access_token", "") or "").strip()

    def min_interval_seconds(self) -> float:
        raw = self.config.get("xhs_provider_min_interval_seconds", 2.0)
        try:
            return min(max(float(raw or 0.0), 0.0), 60.0)
        except (TypeError, ValueError):
            return 2.0

    def close(self) -> None:
        self.session.close()

    def health(self, *, timeout_seconds: int = 10) -> dict[str, Any]:
        payload = self._request_json("GET", "/health", timeout_seconds=timeout_seconds)
        data = payload.get("data")
        if not isinstance(data, dict) or str(data.get("status", "")).lower() != "healthy":
            raise XhsProviderError(
                "小红书提供者健康响应格式异常",
                category="contract",
                pause_required=True,
            )
        return data

    def login_status(self, *, timeout_seconds: int = 20) -> bool:
        payload = self._request_json("GET", "/api/v1/login/status", timeout_seconds=timeout_seconds)
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("is_logged_in"), bool):
            raise XhsProviderError(
                "小红书登录状态响应格式异常",
                category="contract",
                pause_required=True,
            )
        return bool(data["is_logged_in"])

    def search_notes(
        self,
        keyword: str,
        *,
        max_results: int = 20,
        timeout_seconds: int = 45,
    ) -> list[XhsSearchHit]:
        term = str(keyword or "").strip()
        if not term:
            raise XhsProviderError("小红书搜索词不能为空", category="configuration")
        payload = self._request_json(
            "POST",
            "/api/v1/feeds/search",
            json_body={
                "keyword": term,
                "filters": {
                    "sort_by": "最新",
                    "note_type": "图文",
                    "publish_time": "一周内",
                },
            },
            timeout_seconds=timeout_seconds,
        )
        data = payload.get("data")
        feeds = data.get("feeds") if isinstance(data, dict) else None
        if not isinstance(feeds, list):
            raise XhsProviderError(
                "小红书搜索响应缺少 feeds 数组",
                category="contract",
                pause_required=True,
            )

        hits: list[XhsSearchHit] = []
        seen: set[str] = set()
        for item in feeds:
            if not isinstance(item, dict):
                continue
            note_card = item.get("noteCard")
            if not isinstance(note_card, dict):
                continue
            if str(item.get("modelType", "") or "").strip().lower() != "note":
                continue
            note_type = str(note_card.get("type", "") or "").strip().lower()
            if note_type != "normal":
                continue
            note_id = str(item.get("id", "") or "").strip()
            xsec_token = str(item.get("xsecToken", "") or "").strip()
            if not note_id or not xsec_token or note_id in seen:
                continue
            seen.add(note_id)
            user = note_card.get("user")
            author = ""
            if isinstance(user, dict):
                author = str(user.get("nickname", "") or user.get("nickName", "") or "").strip()
            try:
                position = int(item.get("index", len(hits)) or 0)
            except (TypeError, ValueError):
                position = len(hits)
            hits.append(
                XhsSearchHit(
                    note_id=note_id,
                    xsec_token=xsec_token,
                    post_url=canonical_xhs_post_url(note_id),
                    title=str(note_card.get("displayTitle", "") or "").strip(),
                    author=author,
                    note_type=note_type,
                    position=position,
                )
            )
            if len(hits) >= max(1, int(max_results or 1)):
                break
        return hits

    def fetch_note_detail(
        self,
        note_id: str,
        xsec_token: str,
        *,
        timeout_seconds: int = 45,
    ) -> XhsNoteDetail:
        resolved_note_id = str(note_id or "").strip()
        resolved_token = str(xsec_token or "").strip()
        if not resolved_note_id or not resolved_token:
            raise XhsProviderError(
                "小红书详情请求缺少 note_id 或 xsec_token",
                category="configuration",
            )
        payload = self._request_json(
            "POST",
            "/api/v1/feeds/detail",
            json_body={
                "feed_id": resolved_note_id,
                "xsec_token": resolved_token,
                "load_all_comments": False,
            },
            timeout_seconds=timeout_seconds,
        )
        outer = payload.get("data")
        inner = outer.get("data") if isinstance(outer, dict) else None
        note = inner.get("note") if isinstance(inner, dict) else None
        if not isinstance(note, dict):
            raise XhsProviderError(
                "小红书详情响应缺少 note 对象",
                category="contract",
                pause_required=True,
            )
        response_note_id = str(note.get("noteId", "") or resolved_note_id).strip()
        if response_note_id != resolved_note_id:
            raise XhsProviderError(
                "小红书详情响应的 note_id 不一致",
                category="contract",
                pause_required=True,
            )
        note_type = str(note.get("type", "") or "").strip().lower()
        if note_type and note_type != "normal":
            raise XhsProviderError(
                f"小红书笔记不是图文类型：{note_type}",
                category="unsupported_note",
            )

        raw_images = note.get("imageList")
        if not isinstance(raw_images, list):
            raise XhsProviderError(
                "小红书详情响应缺少 imageList 数组",
                category="contract",
                pause_required=True,
            )
        images: list[XhsImageRef] = []
        seen_urls: set[str] = set()
        for index, item in enumerate(raw_images, start=1):
            if not isinstance(item, dict):
                continue
            raw_url = str(item.get("urlDefault", "") or item.get("urlPre", "") or "").strip()
            if not raw_url:
                continue
            url = normalize_xhs_image_url(raw_url)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            try:
                width = max(0, int(item.get("width", 0) or 0))
                height = max(0, int(item.get("height", 0) or 0))
            except (TypeError, ValueError):
                width = 0
                height = 0
            images.append(XhsImageRef(url=url, index=index, width=width, height=height))

        user = note.get("user")
        author = ""
        if isinstance(user, dict):
            author = str(user.get("nickname", "") or user.get("nickName", "") or "").strip()
        try:
            published_at_ms = max(0, int(note.get("time", 0) or 0))
        except (TypeError, ValueError):
            published_at_ms = 0
        description = str(note.get("desc", "") or "").strip()
        return XhsNoteDetail(
            note_id=response_note_id,
            xsec_token=str(note.get("xsecToken", "") or resolved_token).strip(),
            post_url=canonical_xhs_post_url(response_note_id),
            title=str(note.get("title", "") or "").strip(),
            description=description,
            author=author,
            note_type=note_type or "normal",
            published_at_ms=published_at_ms,
            topics=extract_xhs_topics(description),
            images=images,
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        url = f"{self.base_url()}/{str(path or '').lstrip('/')}"
        headers = {"Accept": "application/json"}
        token = self.access_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        with self._request_lock:
            interval = self.min_interval_seconds()
            elapsed = self.clock() - self._last_request_at
            if self._last_request_at > 0 and elapsed < interval:
                self.sleep_func(interval - elapsed)
            try:
                response = self.session.request(
                    str(method or "GET").upper(),
                    url,
                    headers=headers,
                    json=json_body,
                    timeout=max(5, int(timeout_seconds or 30)),
                )
            except requests.Timeout as exc:
                raise XhsProviderError(
                    f"小红书提供者请求超时：{exc}",
                    category="timeout",
                    retryable=True,
                ) from exc
            except requests.RequestException as exc:
                raise XhsProviderError(
                    f"小红书提供者连接失败：{exc}",
                    category="transport",
                    retryable=True,
                ) from exc
            finally:
                self._last_request_at = self.clock()

        payload: Any
        try:
            payload = response.json()
        except Exception:
            try:
                payload = json.loads(str(response.text or ""))
            except (TypeError, json.JSONDecodeError) as exc:
                raise XhsProviderError(
                    f"小红书提供者返回了非 JSON 内容（HTTP {response.status_code}）",
                    category="contract",
                    pause_required=True,
                    status_code=int(response.status_code),
                ) from exc
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                pass
        if not isinstance(payload, dict):
            raise XhsProviderError(
                "小红书提供者 JSON 顶层不是对象",
                category="contract",
                pause_required=True,
                status_code=int(response.status_code),
            )
        if int(response.status_code) >= 400 or payload.get("success") is False or payload.get("error"):
            secrets = [token]
            if isinstance(json_body, dict):
                secrets.extend(
                    str(json_body.get(key, "") or "").strip()
                    for key in ("xsec_token", "xsecToken")
                )
            raise self._build_provider_error(
                payload,
                status_code=int(response.status_code),
                secrets=secrets,
            )
        if payload.get("success") is not True:
            raise XhsProviderError(
                "小红书提供者响应缺少 success=true",
                category="contract",
                pause_required=True,
                status_code=int(response.status_code),
            )
        return payload

    @staticmethod
    def _build_provider_error(
        payload: dict[str, Any],
        *,
        status_code: int,
        secrets: list[str] | None = None,
    ) -> XhsProviderError:
        provider_code = str(payload.get("code", "") or "").strip()
        message_parts = [
            str(payload.get("error", "") or "").strip(),
            str(payload.get("message", "") or "").strip(),
            str(payload.get("details", "") or "").strip(),
        ]
        detail = "；".join(item for item in message_parts if item) or f"HTTP {status_code}"
        for secret in secrets or []:
            if secret:
                detail = detail.replace(secret, "<redacted>")
        lower = f"{provider_code} {detail}".casefold()

        if status_code in {401, 403} or any(word in lower for word in ("未登录", "登录失效", "login required", "not logged")):
            category = "authentication"
            retryable = False
            pause_required = True
        elif any(word in lower for word in ("验证码", "需要验证", "verification", "captcha")):
            category = "verification"
            retryable = False
            pause_required = True
        elif "300012" in lower or any(word in lower for word in ("风控", "异常访问", "账号异常", "网络环境异常")):
            category = "risk_control"
            retryable = False
            pause_required = True
        elif status_code == 429 or any(word in lower for word in ("限流", "请求频繁", "too many requests")):
            category = "rate_limit"
            retryable = False
            pause_required = True
        elif status_code in {408, 425, 500, 502, 503, 504}:
            category = "upstream_http"
            retryable = True
            pause_required = False
        elif provider_code in {"SEARCH_FEEDS_FAILED", "GET_FEED_DETAIL_FAILED"}:
            category = "upstream_contract"
            retryable = False
            pause_required = True
        else:
            category = "provider_error"
            retryable = False
            pause_required = status_code >= 400
        return XhsProviderError(
            f"小红书提供者错误[{provider_code or status_code}]：{detail[:500]}",
            category=category,
            retryable=retryable,
            pause_required=pause_required,
            status_code=status_code,
            provider_code=provider_code,
        )
