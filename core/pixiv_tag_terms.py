from __future__ import annotations

from .matcher import normalize_tag_name


# Keep gallery names separate from the tags used by Pixiv creators.
# Both the Chinese name and the Japanese tag resolve to the same query.
PJSK_PIXIV_QUERY_TERMS: dict[str, str] = {
    "初音未来": "初音ミク",
    "镜音铃": "鏡音リン",
    "镜音连": "鏡音レン",
    "巡音流歌": "巡音ルカ",
    "MEIKO": "MEIKO",
    "KAITO": "KAITO",
    "星乃一歌": "星乃一歌",
    "天马咲希": "天馬咲希",
    "望月穗波": "望月穂波",
    "日野森志步": "日野森志歩",
    "花里实乃理": "花里みのり",
    "桐谷遥": "桐谷遥",
    "桃井爱莉": "桃井愛莉",
    "日野森雫": "日野森雫",
    "小豆泽心羽": "小豆沢こはね",
    "白石杏": "白石杏",
    "东云彰人": "東雲彰人",
    "青柳冬弥": "青柳冬弥",
    "天马司": "天馬司",
    "凤笑梦": "鳳えむ",
    "草薙宁宁": "草薙寧々",
    "神代类": "神代類",
    "宵崎奏": "宵崎奏",
    "朝比奈真冬": "朝比奈まふゆ",
    "东云绘名": "東雲絵名",
    "晓山瑞希": "暁山瑞希",
}


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

for _gallery_name, _pixiv_tag in PJSK_PIXIV_QUERY_TERMS.items():
    for _name in (_gallery_name, _pixiv_tag):
        KNOWN_PIXIV_QUERY_TERMS[normalize_tag_name(_name)] = [_pixiv_tag]


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
