from __future__ import annotations

from typing import Any


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_int(source: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key not in source:
            continue
        value = _as_int(source.get(key))
        if value is not None:
            return value
    return None


def _append_unique(target: list[str], value: str) -> None:
    text = str(value or "").strip()
    if not text:
        return
    lowered = text.casefold()
    if any(str(item or "").casefold() == lowered for item in target):
        return
    target.append(text)


def append_pixiv_safety_tags(
    illust: dict[str, Any],
    raw_tags: list[str],
    translated_tags: list[str] | None = None,
) -> dict[str, Any]:
    """Expose Pixiv safety metadata as tags so existing include/exclude filters can catch it."""

    x_restrict = _first_int(illust, "x_restrict", "xRestrict")
    ai_type = _first_int(illust, "illust_ai_type", "ai_type", "aiType")
    added_tags: list[str] = []

    def add_raw(tag: str) -> None:
        before = len(raw_tags)
        _append_unique(raw_tags, tag)
        if len(raw_tags) != before:
            added_tags.append(tag)

    if x_restrict == 1:
        add_raw("R-18")
        add_raw("R18")
    elif x_restrict is not None and x_restrict >= 2:
        add_raw("R-18G")
        add_raw("R-18")
        add_raw("R18")

    if ai_type is not None and ai_type >= 2:
        add_raw("AI生成")
        add_raw("AI")
        if translated_tags is not None:
            _append_unique(translated_tags, "AI-generated")

    return {
        "x_restrict": x_restrict,
        "illust_ai_type": ai_type,
        "safety_tags": added_tags,
    }
