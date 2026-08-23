from __future__ import annotations

import hashlib
import json
import random
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import requests

# Adapted from the refresh-token App API flow used by upbit/pixivpy (Unlicense):
# https://github.com/upbit/pixivpy
PIXIV_APP_CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
PIXIV_APP_CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
PIXIV_APP_HASH_SECRET = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c"
PIXIV_APP_USER_AGENT = "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)"
PIXIV_APP_OS = "ios"
PIXIV_APP_OS_VERSION = "14.6"
PIXIV_AUTH_URL = "https://oauth.secure.pixiv.net/auth/token"
PIXIV_API_HOST = "https://app-api.pixiv.net"


class PixivAppAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        category: str = "unknown",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.category = category
        self.retryable = retryable


@dataclass
class PixivToken:
    access_token: str
    refresh_token: str
    user_id: str = ""
    expires_at: float = 0.0


def _retry_delay(
    response: requests.Response | None,
    attempt: int,
    random_func: Callable[[float, float], float],
) -> float:
    if response is not None:
        retry_after = str(response.headers.get("Retry-After", "") or "").strip()
        try:
            if retry_after:
                return min(max(float(retry_after), 0.0), 30.0)
        except ValueError:
            pass
    base = min(0.75 * (2 ** max(0, attempt - 1)), 6.0)
    return base + max(0.0, float(random_func(0.0, min(0.5, base * 0.25))))


def _request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: dict[str, Any] | None = None,
    timeout_seconds: int = 20,
    retry_times: int = 3,
    session: requests.Session | None = None,
    sleep_func: Callable[[float], None] = time.sleep,
    random_func: Callable[[float, float], float] = random.uniform,
) -> dict[str, Any]:
    request_headers = {
        "User-Agent": PIXIV_APP_USER_AGENT,
        "app-os": PIXIV_APP_OS,
        "app-os-version": PIXIV_APP_OS_VERSION,
    }
    if headers:
        request_headers.update({str(key): str(value) for key, value in headers.items() if value is not None})
    body = None
    if data is not None:
        body = {str(key): value for key, value in data.items()}
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    requester = session.request if session is not None else requests.request
    payload = ""
    max_attempts = max(1, int(retry_times or 1))
    for attempt in range(1, max_attempts + 1):
        try:
            response = requester(
                method.upper(),
                url,
                headers=request_headers,
                data=body,
                timeout=max(5, int(timeout_seconds or 20)),
            )
            response.raise_for_status()
            payload = response.text
            break
        except requests.HTTPError as exc:
            response = exc.response
            payload = response.text if response is not None else ""
            status_code = int(response.status_code) if response is not None else None
            retryable = status_code in {408, 409, 425, 429, 500, 502, 503, 504}
            if retryable and attempt < max_attempts:
                sleep_func(_retry_delay(response, attempt, random_func))
                continue
            status_text = str(status_code) if status_code is not None else "unknown"
            raise PixivAppAPIError(
                f"HTTP {status_text}: {(payload or str(exc))[:500]}",
                status_code=status_code,
                category="http",
                retryable=retryable,
            ) from exc
        except requests.RequestException as exc:
            if attempt < max_attempts:
                sleep_func(_retry_delay(None, attempt, random_func))
                continue
            raise PixivAppAPIError(
                f"请求失败：{exc}",
                category="transport",
                retryable=True,
            ) from exc
        except Exception as exc:
            if attempt < max_attempts:
                sleep_func(_retry_delay(None, attempt, random_func))
                continue
            raise PixivAppAPIError(
                f"请求失败：{exc}",
                category="unexpected",
                retryable=False,
            ) from exc

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PixivAppAPIError(
            f"Pixiv API 返回了非 JSON 内容：{payload[:200]}",
            category="decode",
        ) from exc
    if not isinstance(parsed, dict):
        raise PixivAppAPIError("Pixiv API 返回的 JSON 不是对象", category="decode")
    return parsed


def _authenticate_with_refresh_token(
    refresh_token: str,
    *,
    timeout_seconds: int,
    retry_times: int,
    session: requests.Session,
    sleep_func: Callable[[float], None],
    random_func: Callable[[float, float], float],
    clock: Callable[[], float],
) -> PixivToken:
    token = str(refresh_token or "").strip()
    if not token:
        raise PixivAppAPIError("未配置 Pixiv refresh token", category="configuration")

    local_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    headers = {
        "x-client-time": local_time,
        "x-client-hash": hashlib.md5((local_time + PIXIV_APP_HASH_SECRET).encode("utf-8")).hexdigest(),
    }
    payload = _request_json(
        PIXIV_AUTH_URL,
        method="POST",
        headers=headers,
        data={
            "get_secure_url": 1,
            "client_id": PIXIV_APP_CLIENT_ID,
            "client_secret": PIXIV_APP_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": token,
        },
        timeout_seconds=timeout_seconds,
        retry_times=retry_times,
        session=session,
        sleep_func=sleep_func,
        random_func=random_func,
    )
    response = payload.get("response")
    if not isinstance(response, dict):
        raise PixivAppAPIError("refresh token 鉴权失败：响应格式异常", category="authentication")

    access_token = str(response.get("access_token", "") or "").strip()
    next_refresh_token = str(response.get("refresh_token", "") or "").strip()
    if not access_token:
        raise PixivAppAPIError("Pixiv 鉴权响应缺少 access_token", category="authentication")

    try:
        expires_in = max(60, int(response.get("expires_in", 3600) or 3600))
    except (TypeError, ValueError):
        expires_in = 3600
    user = response.get("user")
    user_id = ""
    if isinstance(user, dict):
        user_id = str(user.get("id", "") or "").strip()
    return PixivToken(
        access_token=access_token,
        refresh_token=next_refresh_token or token,
        user_id=user_id,
        expires_at=clock() + expires_in,
    )


class PixivAppClient:
    """Thread-safe Pixiv App API client with connection and access-token reuse."""

    def __init__(
        self,
        config,
        *,
        session: requests.Session | None = None,
        sleep_func: Callable[[float], None] = time.sleep,
        random_func: Callable[[float, float], float] = random.uniform,
        clock: Callable[[], float] = time.time,
        auth_failure_threshold: int = 1,
        auth_cooldown_seconds: int = 120,
    ) -> None:
        self.config = config or {}
        self.session = session or requests.Session()
        self.sleep_func = sleep_func
        self.random_func = random_func
        self.clock = clock
        self.auth_failure_threshold = max(1, int(auth_failure_threshold or 1))
        self.auth_cooldown_seconds = max(1, int(auth_cooldown_seconds or 1))
        self._token_lock = threading.RLock()
        self._token: PixivToken | None = None
        self._configured_refresh_token = ""
        self._active_refresh_token = ""
        self._auth_failures = 0
        self._auth_blocked_until = 0.0

    def refresh_token(self) -> str:
        return str(self.config.get("pixiv_refresh_token", "") or "").strip()

    def retry_times(self) -> int:
        try:
            configured = int(self.config.get("platform_retry_times", 2) or 2)
        except (TypeError, ValueError):
            configured = 2
        return min(5, max(1, configured))

    def close(self) -> None:
        self.session.close()

    def invalidate_token(self, *, expected_access_token: str | None = None) -> None:
        with self._token_lock:
            if (
                expected_access_token
                and self._token is not None
                and self._token.access_token != expected_access_token
            ):
                return
            self._token = None

    def _get_token(self, *, timeout_seconds: int) -> PixivToken:
        configured_token = self.refresh_token()
        if not configured_token:
            raise PixivAppAPIError("未配置 Pixiv refresh token", category="configuration")

        with self._token_lock:
            if configured_token != self._configured_refresh_token:
                self._configured_refresh_token = configured_token
                self._active_refresh_token = configured_token
                self._token = None
                self._auth_failures = 0
                self._auth_blocked_until = 0.0

            now = self.clock()
            if self._token is not None and self._token.expires_at - now > 60:
                return self._token
            if self._auth_blocked_until > now:
                remaining = max(1, int(self._auth_blocked_until - now))
                raise PixivAppAPIError(
                    f"Pixiv OAuth 连续失败，客户端熔断中，约 {remaining} 秒后重试",
                    category="circuit_open",
                    retryable=True,
                )

            source_refresh_token = self._active_refresh_token or configured_token
            try:
                refreshed = _authenticate_with_refresh_token(
                    source_refresh_token,
                    timeout_seconds=timeout_seconds,
                    retry_times=self.retry_times(),
                    session=self.session,
                    sleep_func=self.sleep_func,
                    random_func=self.random_func,
                    clock=self.clock,
                )
            except PixivAppAPIError:
                self._auth_failures += 1
                if self._auth_failures >= self.auth_failure_threshold:
                    self._auth_blocked_until = self.clock() + self.auth_cooldown_seconds
                raise

            self._token = refreshed
            self._active_refresh_token = refreshed.refresh_token or source_refresh_token
            self._auth_failures = 0
            self._auth_blocked_until = 0.0
            return refreshed

    def _authorized_request_json(
        self,
        url: str,
        *,
        timeout_seconds: int,
        accept_language: str = "zh-cn",
    ) -> dict[str, Any]:
        token = self._get_token(timeout_seconds=timeout_seconds)
        headers = {"Authorization": f"Bearer {token.access_token}"}
        if accept_language:
            headers["Accept-Language"] = accept_language
        try:
            return _request_json(
                url,
                headers=headers,
                timeout_seconds=timeout_seconds,
                retry_times=self.retry_times(),
                session=self.session,
                sleep_func=self.sleep_func,
                random_func=self.random_func,
            )
        except PixivAppAPIError as exc:
            if exc.status_code != 401:
                raise

        self.invalidate_token(expected_access_token=token.access_token)
        token = self._get_token(timeout_seconds=timeout_seconds)
        headers["Authorization"] = f"Bearer {token.access_token}"
        return _request_json(
            url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            retry_times=self.retry_times(),
            session=self.session,
            sleep_func=self.sleep_func,
            random_func=self.random_func,
        )

    def fetch_illust_detail(
        self,
        illust_id: str | int,
        *,
        timeout_seconds: int = 20,
        accept_language: str = "zh-cn",
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode({"illust_id": str(illust_id)})
        payload = self._authorized_request_json(
            f"{PIXIV_API_HOST}/v1/illust/detail?{query}",
            timeout_seconds=timeout_seconds,
            accept_language=accept_language,
        )
        illust = payload.get("illust")
        if not isinstance(illust, dict):
            raise PixivAppAPIError(f"Pixiv 作品详情格式异常：{payload}", category="decode")
        return illust

    def search_illusts(
        self,
        word: str,
        *,
        search_target: str = "partial_match_for_tags",
        sort: str = "date_desc",
        search_ai_type: int | None = 0,
        offset: int | str | None = None,
        timeout_seconds: int = 20,
        accept_language: str = "zh-cn",
    ) -> dict[str, Any]:
        query: dict[str, Any] = {
            "word": str(word or "").strip(),
            "search_target": search_target or "partial_match_for_tags",
            "sort": sort or "date_desc",
            "filter": "for_ios",
        }
        if not query["word"]:
            raise PixivAppAPIError("Pixiv 搜索词不能为空", category="configuration")
        if search_ai_type is not None:
            query["search_ai_type"] = int(search_ai_type)
        if offset not in (None, ""):
            query["offset"] = offset

        payload = self._authorized_request_json(
            f"{PIXIV_API_HOST}/v1/search/illust?{urllib.parse.urlencode(query)}",
            timeout_seconds=timeout_seconds,
            accept_language=accept_language,
        )
        illusts = payload.get("illusts")
        if not isinstance(illusts, list):
            raise PixivAppAPIError(f"Pixiv 搜索结果格式异常：{payload}", category="decode")
        return payload


def authenticate_with_refresh_token(refresh_token: str, *, timeout_seconds: int = 20) -> PixivToken:
    session = requests.Session()
    try:
        return _authenticate_with_refresh_token(
            refresh_token,
            timeout_seconds=timeout_seconds,
            retry_times=3,
            session=session,
            sleep_func=time.sleep,
            random_func=random.uniform,
            clock=time.time,
        )
    finally:
        session.close()


def fetch_illust_detail(
    illust_id: str | int,
    *,
    refresh_token: str,
    timeout_seconds: int = 20,
    accept_language: str = "zh-cn",
) -> dict[str, Any]:
    client = PixivAppClient({"pixiv_refresh_token": refresh_token})
    try:
        return client.fetch_illust_detail(
            illust_id,
            timeout_seconds=timeout_seconds,
            accept_language=accept_language,
        )
    finally:
        client.close()


def search_illusts(
    word: str,
    *,
    refresh_token: str,
    search_target: str = "partial_match_for_tags",
    sort: str = "date_desc",
    search_ai_type: int | None = 0,
    offset: int | str | None = None,
    timeout_seconds: int = 20,
    accept_language: str = "zh-cn",
) -> dict[str, Any]:
    client = PixivAppClient({"pixiv_refresh_token": refresh_token})
    try:
        return client.search_illusts(
            word,
            search_target=search_target,
            sort=sort,
            search_ai_type=search_ai_type,
            offset=offset,
            timeout_seconds=timeout_seconds,
            accept_language=accept_language,
        )
    finally:
        client.close()


def extract_offset_from_next_url(next_url: str | None) -> int | None:
    raw_url = str(next_url or "").strip()
    if not raw_url:
        return None
    parsed = urllib.parse.urlparse(raw_url)
    values = urllib.parse.parse_qs(parsed.query).get("offset") or []
    if not values:
        return None
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return None
