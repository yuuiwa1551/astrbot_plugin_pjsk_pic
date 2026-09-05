from __future__ import annotations

import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from xhs_cli import client_mixins, cookies as cookie_store
from xhs_cli.client import XhsClient
from xhs_cli.exceptions import (
    IpBlockedError,
    NeedVerifyError,
    SessionExpiredError,
    XhsApiError,
)


VERSION = "pjsk-xhs-cli-sidecar/0.1.0+xhs-cli-0.6.4"
SORT_MAP = {
    "综合": "general",
    "最新": "time_descending",
    "最多点赞": "popularity_descending",
}
NOTE_TYPE_MAP = {
    "不限": 0,
    "视频": 1,
    "图文": 2,
}


def _cookie_mapping(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_cookies = payload.get("cookies", payload) if isinstance(payload, dict) else payload
    if isinstance(raw_cookies, dict):
        return {
            str(name): str(value)
            for name, value in raw_cookies.items()
            if name and value and name != "saved_at"
        }
    return {
        str(item["name"]): str(item["value"])
        for item in raw_cookies
        if isinstance(item, dict) and item.get("name") and item.get("value")
    }


def _normalize_search(payload: dict[str, Any], *, page: int, page_size: int) -> dict[str, Any]:
    feeds: list[dict[str, Any]] = []
    for position, item in enumerate(payload["items"]):
        if not isinstance(item, dict):
            continue
        note_card = item.get("note_card")
        if not isinstance(note_card, dict):
            continue
        note_id = str(item.get("id", "") or note_card.get("note_id", "")).strip()
        xsec_token = str(item.get("xsec_token", "") or note_card.get("xsec_token", "")).strip()
        if not note_id or not xsec_token:
            continue
        user = note_card.get("user") if isinstance(note_card.get("user"), dict) else {}
        raw_type = str(note_card.get("type", "") or "").strip().lower()
        feeds.append(
            {
                "id": note_id,
                "xsecToken": xsec_token,
                "modelType": str(item.get("model_type", "note") or "note"),
                "index": position,
                "noteCard": {
                    "type": raw_type,
                    "displayTitle": str(
                        note_card.get("display_title", "") or note_card.get("title", "")
                    ),
                    "user": {
                        "nickname": str(user.get("nickname", "") or user.get("nick_name", "")),
                    },
                },
            }
        )
    return {
        "feeds": feeds,
        "page": int(page),
        "pageSize": int(page_size),
        "hasMore": payload["has_more"],
    }


def _image_url(item: dict[str, Any]) -> str:
    for key in ("url_default", "url_pre", "url"):
        value = str(item.get(key, "") or "").strip()
        if value:
            return value
    for info in item.get("info_list", []):
        if isinstance(info, dict) and str(info.get("url", "") or "").strip():
            return str(info["url"]).strip()
    return ""


def _normalize_detail(payload: dict[str, Any], *, note_id: str) -> dict[str, Any]:
    items = payload["items"]
    if not items:
        raise ValueError("note detail is empty")
    note_card = items[0]["note_card"]
    user = note_card.get("user") if isinstance(note_card.get("user"), dict) else {}
    images = [
        {
            "urlDefault": url,
            "width": int(image.get("width", 0) or 0),
            "height": int(image.get("height", 0) or 0),
        }
        for image in note_card["image_list"]
        if isinstance(image, dict) and (url := _image_url(image))
    ]
    tags = [
        {"name": str(item.get("name", "") or "").strip()}
        for item in note_card.get("tag_list", [])
        if isinstance(item, dict) and str(item.get("name", "") or "").strip()
    ]
    return {
        "noteId": str(note_card["note_id"]),
        "type": str(note_card['type']),
        "title": str(note_card.get("title", "") or ""),
        "desc": str(note_card.get("desc", "") or ""),
        "user": {
            "nickname": str(user.get("nickname", "") or user.get("nick_name", "")),
        },
        "time": int(note_card.get("time", 0) or 0),
        "imageList": images,
        "tagList": tags,
    }


class FilteredClient(XhsClient):
    """The pinned CLI omits publish_time and uses static filter values."""
    search_filters: dict[str, str]

    def _main_api_post(self, uri, data, header_overrides=None):
        if uri == '/api/sns/web/v1/search/notes':
            data = {**data, 'filters': [
                {'type': 'sort_type', 'tags': [data['sort']]},
                {'type': 'filter_note_type', 'tags': [self.search_filters['note_type']]},
                {'type': 'filter_note_time', 'tags': [self.search_filters['publish_time']]},
                {'type': 'filter_note_range', 'tags': ['不限']},
                {'type': 'filter_pos_distance', 'tags': ['不限']},
            ]}
        return super()._main_api_post(uri, data, header_overrides=header_overrides)


class XhsCliProvider:
    def __init__(self) -> None:
        cookie_path = Path(os.environ.get("XHS_COOKIE_FILE", "/provider-data/cookies.json"))
        state_dir = Path(os.environ.get("XHS_STATE_DIR", "/app/state"))
        state_dir.mkdir(parents=True, exist_ok=True)
        self._publish_time = '不限'
        client_mixins._search_session_path = lambda: state_dir / f"search-{self._publish_time}.json"
        cookie_store.get_config_dir = lambda: state_dir
        self._lock = threading.RLock()
        self._client = FilteredClient(
            _cookie_mapping(cookie_path),
            timeout=float(os.environ.get("XHS_REQUEST_TIMEOUT_SECONDS", "45")),
            request_delay=float(os.environ.get("XHS_REQUEST_DELAY_SECONDS", "2")),
            max_retries=1,
        )

    def close(self) -> None:
        self._client.close()

    def login_status(self) -> bool:
        with self._lock:
            data = self._client.get_self_info()
        return isinstance(data, dict) and bool(data)

    def search(self, request: dict[str, Any]) -> dict[str, Any]:
        keyword = str(request.get("keyword", "") or "").strip()
        filters = request.get("filters") if isinstance(request.get("filters"), dict) else {}
        page = max(1, int(request.get("page", 1) or 1))
        page_size = min(max(1, int(request.get("page_size", 20) or 20)), 50)
        with self._lock:
            self._publish_time = str(filters.get('publish_time', '不限'))
            self._client.search_filters = {
                'note_type': str(filters.get('note_type', '图文')),
                'publish_time': self._publish_time,
            }
            payload = self._client.search_notes(
                keyword,
                page=page,
                page_size=page_size,
                sort=SORT_MAP.get(str(filters.get("sort_by", "最新") or "最新"), "time_descending"),
                note_type=NOTE_TYPE_MAP.get(str(filters.get("note_type", "图文") or "图文"), 2),
            )
        return _normalize_search(payload, page=page, page_size=page_size)

    def detail(self, request: dict[str, Any]) -> dict[str, Any]:
        note_id = str(request.get("feed_id", "") or "").strip()
        xsec_token = str(request.get("xsec_token", "") or "").strip()
        with self._lock:
            payload = self._client.get_note_by_id(
                note_id,
                xsec_token=xsec_token,
                xsec_source="pc_search",
            )
        return _normalize_detail(payload, note_id=note_id)


class Handler(BaseHTTPRequestHandler):
    server_version = VERSION

    def _authorized(self) -> bool:
        expected = os.environ.get("AUTH_TOKEN", "")
        if not expected:
            return True
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied, f"Bearer {expected}")

    def _json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        return payload if isinstance(payload, dict) else {}

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _provider(self) -> XhsCliProvider:
        return self.server.provider  # type: ignore[attr-defined]

    def _dispatch(self) -> None:
        if self.path == "/health":
            self._send(
                200,
                {
                    "success": True,
                    "data": {
                        "status": "healthy",
                        "version": VERSION,
                        "capabilities": {"searchPagination": True},
                    },
                },
            )
            return
        if not self._authorized():
            self._send(401, {"success": False, "code": "UNAUTHORIZED", "message": "unauthorized"})
            return
        if self.command == "GET" and self.path == "/api/v1/login/status":
            self._send(200, {"success": True, "data": {"is_logged_in": self._provider().login_status()}})
            return
        if self.command == "POST" and self.path == "/api/v1/feeds/search":
            self._send(200, {"success": True, "data": self._provider().search(self._json_body())})
            return
        if self.command == "POST" and self.path == "/api/v1/feeds/detail":
            self._send(
                200,
                {"success": True, "data": {"data": {"note": self._provider().detail(self._json_body())}}},
            )
            return
        self._send(404, {"success": False, "code": "NOT_FOUND", "message": "not found"})

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def _handle(self) -> None:
        try:
            self._dispatch()
        except SessionExpiredError:
            self._send(401, {"success": False, "code": "SESSION_EXPIRED", "message": "login required"})
        except NeedVerifyError:
            self._send(403, {"success": False, "code": "NEED_VERIFY", "message": "verification required"})
        except IpBlockedError:
            self._send(429, {"success": False, "code": "300012", "message": "risk control"})
        except (KeyError, ValueError, TypeError):
            self._send(422, {"success": False, "code": "UPSTREAM_CONTRACT", "message": "invalid search or note response"})
        except XhsApiError as exc:
            self._send(502, {"success": False, "code": "XHS_API_ERROR", "message": f"upstream API error code={exc.code}"})
        except Exception as exc:
            self._send(500, {"success": False, "code": type(exc).__name__, "message": "provider request failed"})


class Server(ThreadingHTTPServer):
    provider: XhsCliProvider


def main() -> None:
    provider = XhsCliProvider()
    server = Server(("0.0.0.0", int(os.environ.get("PORT", "18060"))), Handler)
    server.provider = provider
    try:
        server.serve_forever()
    finally:
        provider.close()


if __name__ == "__main__":
    main()
