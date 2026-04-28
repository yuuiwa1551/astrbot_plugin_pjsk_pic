from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from astrbot.api import logger

from .db import ImageIndexDB
from .matcher import normalize_tag_name


GENERIC_PIXIV_TERMS = {
    "プロジェクトセカイ",
    "プロセカ",
    "pjsk",
    "prsk_fa",
    "projectsekai",
    "projectセカイ",
    "project sekai",
    "世界計画",
    "世界计划",
    "ボーカロイド",
    "vocaloid",
    "25時、ナイトコードで。",
    "25时，在夜之电台",
    "nightcord at 25:00",
    "ニーゴ",
    "leo/need",
    "レオニ",
    "more more jump!",
    "モモジャン",
    "vivid bad squad",
    "ビビバス",
    "ワンダーランズ×ショウタイム",
    "ワンダショ",
    "virtual singer",
    "バーチャル・シンガー",
    "fanart",
    "100users入り",
    "1000users入り",
}


@dataclass
class IdentityCandidateDraft:
    source_tag_id: int
    target_tag_id: int
    source_tag: str
    target_tag: str
    score: float
    reasons: list[str]
    evidence: dict[str, Any]


class TagIdentityService:
    def __init__(self, context, db: ImageIndexDB, config) -> None:
        self.context = context
        self.db = db
        self.config = config or {}

    async def scan(self, *, limit: int = 80, llm_limit: int | None = None) -> dict[str, Any]:
        entries = self.db.list_tag_identity_scan_inputs(platform="pixiv", limit=320)
        drafts: dict[tuple[int, int], IdentityCandidateDraft] = {}
        scanned_pairs = 0

        for left_index, left in enumerate(entries):
            for right in entries[left_index + 1 :]:
                scanned_pairs += 1
                draft = self._evaluate_pair(left, right)
                if not draft:
                    continue
                key = (draft.source_tag_id, draft.target_tag_id)
                current = drafts.get(key)
                if current is None or draft.score > current.score:
                    drafts[key] = draft

        ordered = sorted(
            drafts.values(),
            key=lambda item: (-item.score, item.source_tag, item.target_tag),
        )[: max(1, int(limit or 1))]
        stale_count = self.db.mark_stale_tag_identity_candidates(set(drafts.keys()))
        resolved_llm_limit = (
            max(0, int(llm_limit))
            if llm_limit is not None
            else max(0, int(self.config.get("tag_identity_llm_limit", 12) or 12))
        )

        items: list[dict[str, Any]] = []
        for index, draft in enumerate(ordered):
            llm_result = (
                await self._review_with_llm(draft)
                if index < resolved_llm_limit
                else {"status": "skipped", "reason": "超过本次 LLM 复核上限，保留 Pixiv / tag 证据候选"}
            )
            adjusted_score = self._score_with_llm(draft.score, llm_result)
            stored = self.db.upsert_tag_identity_candidate(
                source_tag_id=draft.source_tag_id,
                target_tag_id=draft.target_tag_id,
                score=adjusted_score,
                reasons=draft.reasons,
                evidence=draft.evidence,
                llm_result=llm_result,
            )
            if stored:
                items.append(stored)

        return {
            "ok": True,
            "message": f"已扫描 {scanned_pairs} 对角色 tag，刷新 {len(items)} 条待确认候选，隐藏 {stale_count} 条过期候选。",
            "scanned_pairs": scanned_pairs,
            "upserted": len(items),
            "stale": stale_count,
            "items": items,
        }

    def _evaluate_pair(self, left: dict[str, Any], right: dict[str, Any]) -> IdentityCandidateDraft | None:
        left_terms = self._collect_terms(left)
        right_terms = self._collect_terms(right)
        reasons: list[str] = []
        score = 0.0

        left_name_chars = self._cjk_chars(str(left.get("name", "")))
        right_name_chars = self._cjk_chars(str(right.get("name", "")))
        shared_chars = sorted(left_name_chars & right_name_chars)
        shared_ratio = (
            len(shared_chars) / max(len(left_name_chars), len(right_name_chars))
            if left_name_chars and right_name_chars
            else 0.0
        )
        if shared_chars and shared_ratio >= 0.4:
            score += 28 + min(35, int(shared_ratio * 50)) + min(10, len(shared_chars) * 3)
            reasons.append(f"角色名 CJK 重合约 {shared_ratio:.0%}：{''.join(shared_chars)}")

        matched_terms: list[dict[str, Any]] = []
        common_norms = set(left_terms) & set(right_terms)
        for normalized in sorted(common_norms):
            if self._is_generic_term(normalized):
                continue
            left_payload = left_terms[normalized]
            right_payload = right_terms[normalized]
            text = left_payload["text"] or right_payload["text"]
            matched_terms.append(
                {
                    "term": text,
                    "source_side": left_payload["source"],
                    "target_side": right_payload["source"],
                    "normalized": normalized,
                }
            )
            if left_payload["source"] == "name" or right_payload["source"] == "name":
                score += 40
            elif "platform" in {left_payload["source"], right_payload["source"]}:
                score += 28
            else:
                score += 18
            if len(matched_terms) >= 8:
                break
        if matched_terms:
            reasons.append("Pixiv 平台词 / 历史词存在交叉命中")

        cross_hits = self._cross_name_hits(left, right, left_terms, right_terms)
        if cross_hits:
            matched_terms.extend(cross_hits)
            score += min(50, 25 * len(cross_hits))
            reasons.append("一侧的 alias / Pixiv 词命中另一侧主 tag")

        if score < 40:
            return None

        source, target = self._orient_pair(left, right)
        if int(source["id"]) == int(target["id"]):
            return None

        evidence = {
            "shared_chars": shared_chars,
            "shared_ratio": round(shared_ratio, 3),
            "matched_terms": matched_terms[:12],
            "source": self._entry_evidence(source),
            "target": self._entry_evidence(target),
            "raw_pair": {
                "left": self._entry_evidence(left),
                "right": self._entry_evidence(right),
            },
        }
        return IdentityCandidateDraft(
            source_tag_id=int(source["id"]),
            target_tag_id=int(target["id"]),
            source_tag=str(source["name"]),
            target_tag=str(target["name"]),
            score=round(score, 2),
            reasons=reasons,
            evidence=evidence,
        )

    def _collect_terms(self, entry: dict[str, Any]) -> dict[str, dict[str, str]]:
        terms: dict[str, dict[str, str]] = {}
        anchor_norms: set[str] = set()

        def push(value: str, source: str, *, anchor: bool = False) -> None:
            text = str(value or "").strip()
            normalized = normalize_tag_name(text)
            if not text or not normalized or self._is_generic_term(normalized):
                return
            if anchor:
                anchor_norms.add(normalized)
            terms.setdefault(normalized, {"text": text, "source": source})

        push(str(entry.get("name", "")), "name", anchor=True)
        for alias in entry.get("aliases") or []:
            push(str(alias), "alias", anchor=True)
        for item in entry.get("platform_terms") or []:
            if isinstance(item, dict):
                push(str(item.get("term", "")), "platform", anchor=True)
        for item in entry.get("history_terms") or []:
            if isinstance(item, dict):
                history_term = str(item.get("term", ""))
                if self._history_term_resembles_entry(history_term, entry, anchor_norms):
                    push(history_term, "history")
        return terms

    def _history_term_resembles_entry(self, term: str, entry: dict[str, Any], anchor_norms: set[str]) -> bool:
        text = str(term or "").strip()
        normalized = normalize_tag_name(text)
        if not text or not normalized or self._is_generic_term(normalized):
            return False
        if normalized in anchor_norms:
            return True
        name_chars = self._cjk_chars(str(entry.get("name", "")))
        term_chars = self._cjk_chars(text)
        if len(name_chars & term_chars) >= 2:
            return True
        return False

    @staticmethod
    def _cjk_chars(text: str) -> set[str]:
        return set(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text or ""))

    @staticmethod
    def _is_generic_term(normalized: str) -> bool:
        text = str(normalized or "").strip().lower()
        if not text:
            return True
        generic = {normalize_tag_name(term).lower() for term in GENERIC_PIXIV_TERMS}
        if text in generic:
            return True
        if text.isdigit():
            return True
        return False

    def _cross_name_hits(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
        left_terms: dict[str, dict[str, str]],
        right_terms: dict[str, dict[str, str]],
    ) -> list[dict[str, Any]]:
        hits: list[dict[str, Any]] = []
        left_name = normalize_tag_name(str(left.get("name", "")))
        right_name = normalize_tag_name(str(right.get("name", "")))
        if right_name in left_terms and left_terms[right_name]["source"] != "name":
            hits.append(
                {
                    "term": left_terms[right_name]["text"],
                    "source_side": left_terms[right_name]["source"],
                    "target_side": "target_name",
                    "normalized": right_name,
                }
            )
        if left_name in right_terms and right_terms[left_name]["source"] != "name":
            hits.append(
                {
                    "term": right_terms[left_name]["text"],
                    "source_side": right_terms[left_name]["source"],
                    "target_side": "source_name",
                    "normalized": left_name,
                }
            )
        return hits

    @staticmethod
    def _orient_pair(left: dict[str, Any], right: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        left_score = int(left.get("image_count", 0) or 0) * 3 + len(left.get("aliases") or []) + len(left.get("platform_terms") or []) * 2
        right_score = int(right.get("image_count", 0) or 0) * 3 + len(right.get("aliases") or []) + len(right.get("platform_terms") or []) * 2
        if left_score > right_score:
            return right, left
        if right_score > left_score:
            return left, right
        return (right, left) if str(left.get("name", "")) < str(right.get("name", "")) else (left, right)

    @staticmethod
    def _entry_evidence(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "tag_id": int(entry.get("id", 0) or 0),
            "tag_name": str(entry.get("name", "")),
            "image_count": int(entry.get("image_count", 0) or 0),
            "aliases": [str(item) for item in (entry.get("aliases") or [])[:20]],
            "platform_terms": [
                {
                    "term": str(item.get("term", "")),
                    "term_type": str(item.get("term_type", "")),
                    "source": str(item.get("source", "")),
                    "confidence": float(item.get("confidence", 0) or 0),
                }
                for item in (entry.get("platform_terms") or [])[:20]
                if isinstance(item, dict)
            ],
            "history_terms": [
                {
                    "term": str(item.get("term", "")),
                    "count": int(item.get("count", 0) or 0),
                }
                for item in (entry.get("history_terms") or [])[:20]
                if isinstance(item, dict)
            ],
        }

    async def _review_with_llm(self, draft: IdentityCandidateDraft) -> dict[str, Any]:
        provider_id = str(
            self.config.get("tag_identity_provider_id", "")
            or self.config.get("review_provider_id", "")
            or ""
        ).strip()
        if not provider_id:
            return {"status": "unavailable", "reason": "未配置 tag_identity_provider_id / review_provider_id"}
        if self.context is None or not hasattr(self.context, "llm_generate"):
            return {"status": "unavailable", "reason": "当前 WebUI 未拿到 AstrBot LLM context"}

        prompt = self._build_llm_prompt(draft)
        try:
            response = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
            text = getattr(response, "completion_text", "") or getattr(response, "_completion_text", "") or ""
            parsed = self._parse_llm_response(text)
            parsed["status"] = "ok"
            parsed["raw_text"] = text[:800]
            return parsed
        except Exception as exc:
            logger.error(f"[PJSKPic] tag 身份 LLM 复核失败: {exc}", exc_info=True)
            return {"status": "error", "reason": f"LLM 调用失败：{exc}"}

    @staticmethod
    def _build_llm_prompt(draft: IdentityCandidateDraft) -> str:
        evidence = json.dumps(draft.evidence, ensure_ascii=False, indent=2)
        return (
            "你是二次元角色 tag 身份复核器。请判断 source_tag 和 target_tag 是否指同一个角色。\n"
            "只根据给定证据和常识判断；如果只是同作品、同组合、通用标签，不要判同一角色。\n"
            f"source_tag: {draft.source_tag}\n"
            f"target_tag: {draft.target_tag}\n"
            f"候选原因: {'；'.join(draft.reasons)}\n"
            f"证据:\n{evidence}\n"
            "只输出 JSON："
            '{"same_character": true/false/null, "confidence": 0.0-1.0, "reason": "中文理由", "canonical_name": "建议主 tag 或空"}'
        )

    @staticmethod
    def _parse_llm_response(text: str) -> dict[str, Any]:
        for raw in re.findall(r"\{[\s\S]*?\}", text or ""):
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if isinstance(data, dict):
                same = data.get("same_character")
                confidence = data.get("confidence", 0)
                try:
                    confidence_value = max(0.0, min(1.0, float(confidence or 0)))
                except (TypeError, ValueError):
                    confidence_value = 0.0
                return {
                    "same_character": same if same in {True, False, None} else None,
                    "confidence": confidence_value,
                    "reason": str(data.get("reason", "") or "").strip()[:300],
                    "canonical_name": str(data.get("canonical_name", "") or "").strip()[:80],
                }
        return {
            "same_character": None,
            "confidence": 0.0,
            "reason": (text or "").strip()[:300] or "模型未返回可解析 JSON",
            "canonical_name": "",
        }

    @staticmethod
    def _score_with_llm(score: float, llm_result: dict[str, Any]) -> float:
        if llm_result.get("status") != "ok":
            return round(float(score or 0), 2)
        confidence = float(llm_result.get("confidence", 0) or 0)
        same = llm_result.get("same_character")
        if same is True:
            score += 25 * confidence
        elif same is False:
            score -= 20 * confidence
        return round(max(0.0, score), 2)
