from __future__ import annotations

from .matcher import normalize_tag_name


KNOWN_PIXIV_QUERY_TERMS: dict[str, list[str]] = {
    "初音未来": ["初音ミク"],
    "初音未來": ["初音ミク"],
    "hatsunemiku": ["初音ミク"],
    "hatsune miku": ["初音ミク"],
    "miku": ["初音ミク"],
    "镜音铃": ["鏡音リン"],
    "鏡音リン": ["鏡音リン"],
    "鏡音鈴": ["鏡音リン"],
    "kagamine rin": ["鏡音リン"],
    "kagaminerin": ["鏡音リン"],
    "rin": ["鏡音リン"],
    "镜音连": ["鏡音レン"],
    "鏡音レン": ["鏡音レン"],
    "鏡音連": ["鏡音レン"],
    "kagamine len": ["鏡音レン"],
    "kagaminelen": ["鏡音レン"],
    "len": ["鏡音レン"],
    "晓山瑞希": ["暁山瑞希"],
    "暁山瑞希": ["暁山瑞希"],
    "akiyama mizuki": ["暁山瑞希"],
    "mzk": ["暁山瑞希"],
}


def known_pixiv_query_terms(*values: str) -> list[str]:
    resolved: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_tag_name(value)
        lowered = str(value or "").strip().casefold()
        for key in (normalized, lowered):
            for term in KNOWN_PIXIV_QUERY_TERMS.get(key, []):
                term_key = normalize_tag_name(term)
                if term and term_key not in seen:
                    seen.add(term_key)
                    resolved.append(term)
    return resolved
