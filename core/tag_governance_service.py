from __future__ import annotations

from typing import Any

from .pixiv_tag_terms import known_pixiv_query_terms
from .tag_policy import looks_like_broad_alias, tag_status_label, tag_type_label


class TagGovernanceService:
    def __init__(self, db) -> None:
        self.db = db

    def build_report(self, *, detail_limit: int = 12) -> dict[str, Any]:
        snapshot = self.db.get_tag_governance_snapshot()
        rows = list(snapshot.get("tags") or [])
        aliases = list(snapshot.get("aliases") or [])

        safe_cleanup = [row for row in rows if bool(row.get("safe_cleanup"))]
        protected_other = [row for row in rows if bool(row.get("protected_other"))]
        broad_aliases = [row for row in aliases if looks_like_broad_alias(str(row.get("alias") or ""))]
        missing_pixiv_terms: list[dict[str, Any]] = []
        for row in rows:
            if str(row.get("status") or "") != "active" or str(row.get("tag_type") or "") != "character":
                continue
            explicit_query_count = int(row.get("pixiv_query_term_count") or 0)
            builtin_terms = known_pixiv_query_terms(
                str(row.get("name") or ""),
                *[str(item) for item in (row.get("aliases") or [])],
            )
            if explicit_query_count <= 0 and not builtin_terms:
                missing_pixiv_terms.append(row)

        totals = dict(snapshot.get("totals") or {})
        totals.update(
            {
                "safe_cleanup_candidates": len(safe_cleanup),
                "protected_other_tags": len(protected_other),
                "broad_aliases": len(broad_aliases),
                "missing_pixiv_terms": len(missing_pixiv_terms),
            }
        )
        limit = max(1, int(detail_limit or 1))
        return {
            "totals": totals,
            "safe_cleanup": safe_cleanup[:limit],
            "protected_other": protected_other[:limit],
            "broad_aliases": broad_aliases[:limit],
            "missing_pixiv_terms": missing_pixiv_terms[:limit],
        }

    def format_report(self, *, detail_limit: int = 8) -> str:
        report = self.build_report(detail_limit=detail_limit)
        totals = report["totals"]
        type_counts = totals.get("type_counts") or {}
        status_counts = totals.get("status_counts") or {}
        lines = [
            "tag 规范报告（只读）：",
            (
                f"主 tag {int(totals.get('tags') or 0)}："
                f"角色 {int(type_counts.get('character') or 0)}，"
                f"CP {int(type_counts.get('pairing') or 0)}，"
                f"主题 {int(type_counts.get('theme') or 0)}，"
                f"其他 {int(type_counts.get('other') or 0)}。"
            ),
            (
                f"状态：启用 {int(status_counts.get('active') or 0)}，"
                f"待确认 {int(status_counts.get('pending') or 0)}，"
                f"归档 {int(status_counts.get('archived') or 0)}；"
                f"待处理提案 {int(totals.get('pending_proposals') or 0)}。"
            ),
            (
                f"安全清理候选 {int(totals.get('safe_cleanup_candidates') or 0)}，"
                f"受保护其他 tag {int(totals.get('protected_other_tags') or 0)}，"
                f"宽泛 alias {int(totals.get('broad_aliases') or 0)}，"
                f"缺可靠 Pixiv 搜索词的角色 {int(totals.get('missing_pixiv_terms') or 0)}。"
            ),
        ]

        def append_names(title: str, items: list[dict[str, Any]], formatter) -> None:
            if not items:
                return
            lines.append(title + "：" + "、".join(formatter(item) for item in items))

        append_names("安全清理示例", report["safe_cleanup"], lambda item: str(item.get("name") or ""))
        append_names(
            "受保护示例",
            report["protected_other"],
            lambda item: f"{item.get('name')}({int(item.get('approved_count') or 0)}张已通过)",
        )
        append_names(
            "宽泛 alias",
            report["broad_aliases"],
            lambda item: f"{item.get('alias')}→{item.get('tag_name')}",
        )
        append_names(
            "缺 Pixiv 词",
            report["missing_pixiv_terms"],
            lambda item: str(item.get("name") or ""),
        )
        lines.append("本报告不会删除、合并或改写任何 tag。")
        return "\n".join(lines)

    @staticmethod
    def format_proposal(row: dict[str, Any]) -> str:
        aliases = [str(item) for item in (row.get("aliases") or []) if str(item).strip()]
        submitter = str(row.get("submitter_name") or row.get("submitter_id") or "-")
        return (
            f"#{int(row.get('id') or 0)} {row.get('proposed_name')}"
            f"（出现 {int(row.get('occurrence_count') or 1)} 次，提交者 {submitter}"
            + (f"，建议 alias：{'、'.join(aliases[:5])}" if aliases else "")
            + "）"
        )

    @staticmethod
    def describe_tag(row: Any) -> str:
        return (
            f"{row['name']}（{tag_type_label(str(row['tag_type'] or 'other'))}，"
            f"{tag_status_label(str(row['status'] or 'active'))}）"
        )
