from __future__ import annotations

import re

from .matcher import normalize_tag_name


TAG_TYPES = {"character", "pairing", "theme", "other"}
TAG_STATUSES = {"active", "pending", "archived"}

TAG_TYPE_LABELS = {
    "character": "角色",
    "pairing": "CP",
    "theme": "主题",
    "other": "其他",
}
TAG_STATUS_LABELS = {
    "active": "启用",
    "pending": "待确认",
    "archived": "归档",
}

_TAG_TYPE_ALIASES = {
    "character": "character",
    "角色": "character",
    "人物": "character",
    "char": "character",
    "pairing": "pairing",
    "pair": "pairing",
    "cp": "pairing",
    "组合": "pairing",
    "配对": "pairing",
    "theme": "theme",
    "主题": "theme",
    "分类": "theme",
    "other": "other",
    "其他": "other",
    "普通": "other",
}
_TAG_STATUS_ALIASES = {
    "active": "active",
    "启用": "active",
    "正常": "active",
    "pending": "pending",
    "待确认": "pending",
    "待审核": "pending",
    "archived": "archived",
    "归档": "archived",
    "停用": "archived",
}


def normalize_tag_type(value: str | None, *, default: str | None = None) -> str | None:
    text = str(value or "").strip().casefold()
    resolved = _TAG_TYPE_ALIASES.get(text)
    if resolved:
        return resolved
    return default if default in TAG_TYPES else None


def normalize_tag_status(value: str | None, *, default: str | None = None) -> str | None:
    text = str(value or "").strip().casefold()
    resolved = _TAG_STATUS_ALIASES.get(text)
    if resolved:
        return resolved
    return default if default in TAG_STATUSES else None


def tag_type_label(value: str | None) -> str:
    normalized = normalize_tag_type(value, default="other") or "other"
    return TAG_TYPE_LABELS[normalized]


def tag_status_label(value: str | None) -> str:
    normalized = normalize_tag_status(value, default="active") or "active"
    return TAG_STATUS_LABELS[normalized]


def looks_like_broad_alias(value: str) -> bool:
    text = str(value or "").strip()
    normalized = normalize_tag_name(text)
    if not text or not normalized:
        return True
    meaningful = re.findall(r"[0-9a-zA-Z\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", text)
    if not meaningful:
        return True
    return len(normalized) <= 1
