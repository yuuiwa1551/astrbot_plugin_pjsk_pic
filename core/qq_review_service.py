from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .db import ImageIndexDB


@dataclass(frozen=True, slots=True)
class QQReviewSession:
    session_key: str
    origin: str
    reviewer_id: str
    image_id: int
    filter_tag_id: int
    filter_tag_name: str
    claimed_at: float
    expires_at: float


class QQReviewSessionService:
    OPEN_STATUSES = ("pending", "uncertain")

    def __init__(
        self,
        db: ImageIndexDB,
        config: Mapping[str, Any],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = db
        self.config = config
        self._clock = clock
        self._lock = asyncio.Lock()
        self._sessions: dict[str, QQReviewSession] = {}
        self._claims: dict[int, str] = {}
        self._recent: dict[str, deque[int]] = {}

    @staticmethod
    def make_session_key(origin: str, reviewer_id: str) -> str:
        return f"{str(origin or 'default')}\x1f{str(reviewer_id or 'unknown')}"

    def _claim_ttl_seconds(self) -> int:
        value = int(self.config.get("qq_review_claim_ttl_seconds", 600) or 600)
        return min(max(value, 60), 3600)

    def _recent_count(self) -> int:
        value = int(self.config.get("qq_review_recent_count", 30) or 30)
        return min(max(value, 1), 200)

    def _recent_queue(self, session_key: str) -> deque[int]:
        wanted_size = self._recent_count()
        queue = self._recent.get(session_key)
        if queue is None or queue.maxlen != wanted_size:
            queue = deque(list(queue or []), maxlen=wanted_size)
            self._recent[session_key] = queue
        return queue

    def _release_locked(self, session_key: str, *, remember: bool) -> QQReviewSession | None:
        session = self._sessions.pop(session_key, None)
        if session is None:
            return None
        if self._claims.get(session.image_id) == session_key:
            self._claims.pop(session.image_id, None)
        if remember:
            self._recent_queue(session_key).append(session.image_id)
        return session

    def _cleanup_expired_locked(self) -> None:
        now = self._clock()
        expired = [
            session_key
            for session_key, session in self._sessions.items()
            if session.expires_at <= now
        ]
        for session_key in expired:
            self._release_locked(session_key, remember=False)

    async def claim_next(
        self,
        *,
        origin: str,
        reviewer_id: str,
        filter_tag_id: int = 0,
        filter_tag_name: str = "",
        replace_current: bool = True,
    ) -> tuple[QQReviewSession | None, int]:
        session_key = self.make_session_key(origin, reviewer_id)
        async with self._lock:
            self._cleanup_expired_locked()
            current = self._sessions.get(session_key)
            if current is not None and not replace_current:
                if self.db.is_open_pixiv_review_image(current.image_id, statuses=self.OPEN_STATUSES):
                    remaining = self.db.count_open_pixiv_review_images(
                        statuses=self.OPEN_STATUSES,
                        candidate_tag_id=current.filter_tag_id or None,
                    )
                    return current, remaining
                self._release_locked(session_key, remember=True)
            elif current is not None:
                self._release_locked(session_key, remember=True)

            claimed_ids = set(self._claims)
            recent_ids = set(self._recent_queue(session_key))
            excluded = claimed_ids | recent_ids
            row = self.db.get_random_pixiv_review_image(
                statuses=self.OPEN_STATUSES,
                candidate_tag_id=filter_tag_id or None,
                exclude_image_ids=excluded,
            )
            if row is None and recent_ids:
                row = self.db.get_random_pixiv_review_image(
                    statuses=self.OPEN_STATUSES,
                    candidate_tag_id=filter_tag_id or None,
                    exclude_image_ids=claimed_ids,
                )
            remaining = self.db.count_open_pixiv_review_images(
                statuses=self.OPEN_STATUSES,
                candidate_tag_id=filter_tag_id or None,
            )
            if row is None:
                return None, remaining

            now = self._clock()
            session = QQReviewSession(
                session_key=session_key,
                origin=str(origin or "default"),
                reviewer_id=str(reviewer_id or "unknown"),
                image_id=int(row["image_id"]),
                filter_tag_id=max(0, int(filter_tag_id or 0)),
                filter_tag_name=str(filter_tag_name or "").strip(),
                claimed_at=now,
                expires_at=now + self._claim_ttl_seconds(),
            )
            self._sessions[session_key] = session
            self._claims[session.image_id] = session_key
            return session, remaining

    async def get_current(self, *, origin: str, reviewer_id: str) -> QQReviewSession | None:
        session_key = self.make_session_key(origin, reviewer_id)
        async with self._lock:
            self._cleanup_expired_locked()
            session = self._sessions.get(session_key)
            if session is None:
                return None
            if not self.db.is_open_pixiv_review_image(session.image_id, statuses=self.OPEN_STATUSES):
                self._release_locked(session_key, remember=True)
                return None
            return session

    async def release_current(
        self,
        *,
        origin: str,
        reviewer_id: str,
        remember: bool = True,
    ) -> QQReviewSession | None:
        session_key = self.make_session_key(origin, reviewer_id)
        async with self._lock:
            self._cleanup_expired_locked()
            return self._release_locked(session_key, remember=remember)

    async def approve_current(
        self,
        *,
        origin: str,
        reviewer_id: str,
        tag_name: str,
    ) -> tuple[bool, dict[str, Any]]:
        session_key = self.make_session_key(origin, reviewer_id)
        async with self._lock:
            self._cleanup_expired_locked()
            session = self._sessions.get(session_key)
            if session is None:
                return False, {"message": "当前没有领取中的审核图片。", "code": "no_session"}
            if not self.db.is_open_pixiv_review_image(session.image_id, statuses=self.OPEN_STATUSES):
                self._release_locked(session_key, remember=True)
                return False, {
                    "message": f"图片 #{session.image_id} 已被其他人处理，请重新抽取。",
                    "code": "stale_session",
                    "image_id": session.image_id,
                }
            ok, result = self.db.apply_image_review(
                session.image_id,
                selected_tag_names=[tag_name],
                source_terms=[],
                platform="pixiv",
                reason=f"QQ 群友 {reviewer_id} 人工审核通过",
                reject_unselected=True,
                require_open_review=True,
            )
            payload = dict(result) if isinstance(result, dict) else {"message": str(result)}
            if ok or payload.get("code") == "stale_review":
                self._release_locked(session_key, remember=True)
            if payload.get("code") == "stale_review":
                payload["code"] = "stale_session"
                payload["message"] = f"图片 #{session.image_id} 已被其他人处理，请重新抽取。"
            payload.setdefault("image_id", session.image_id)
            payload["filter_tag_id"] = session.filter_tag_id
            payload["filter_tag_name"] = session.filter_tag_name
            return ok, payload

    async def reject_current(
        self,
        *,
        origin: str,
        reviewer_id: str,
        reason: str = "",
    ) -> tuple[bool, dict[str, Any]]:
        session_key = self.make_session_key(origin, reviewer_id)
        async with self._lock:
            self._cleanup_expired_locked()
            session = self._sessions.get(session_key)
            if session is None:
                return False, {"message": "当前没有领取中的审核图片。", "code": "no_session"}
            if not self.db.is_open_pixiv_review_image(session.image_id, statuses=self.OPEN_STATUSES):
                self._release_locked(session_key, remember=True)
                return False, {
                    "message": f"图片 #{session.image_id} 已被其他人处理，请重新抽取。",
                    "code": "stale_session",
                    "image_id": session.image_id,
                }
            reason_text = str(reason or "").strip()[:200]
            audit_reason = f"QQ 群友 {reviewer_id} 人工拒绝"
            if reason_text:
                audit_reason += f"：{reason_text}"
            ok, result = self.db.reject_image_source(
                session.image_id,
                platform="pixiv",
                reason=audit_reason,
                require_open_review=True,
            )
            payload = dict(result) if isinstance(result, dict) else {"message": str(result)}
            if ok or payload.get("code") == "stale_review":
                self._release_locked(session_key, remember=True)
            if payload.get("code") == "stale_review":
                payload["code"] = "stale_session"
                payload["message"] = f"图片 #{session.image_id} 已被其他人处理，请重新抽取。"
            payload.setdefault("image_id", session.image_id)
            payload["filter_tag_id"] = session.filter_tag_id
            payload["filter_tag_name"] = session.filter_tag_name
            return ok, payload

    async def clear(self) -> None:
        async with self._lock:
            self._sessions.clear()
            self._claims.clear()
            self._recent.clear()
