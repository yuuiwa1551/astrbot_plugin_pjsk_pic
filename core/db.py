from __future__ import annotations

from collections import Counter
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .matcher import normalize_tag_name
from .models import APPROVED_STATUSES, MatchResult
from .phash import hamming_distance

IMAGE_TAG_STATUS_PRIORITY = {
    "manual_approved": 5,
    "approved": 4,
    "pending": 3,
    "uncertain": 2,
    "manual_rejected": 1,
    "rejected": 0,
}


def split_status_filter(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = re.split(r"[,，\s]+", value)
    else:
        raw_items = [str(item) for item in value]
    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        text = str(raw or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def utcnow_str() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ImageIndexDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.execute("PRAGMA temp_store=MEMORY;")
        except sqlite3.OperationalError:
            pass
        return conn

    def _table_columns(self, conn: sqlite3.Connection, table: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row['name']) for row in rows}

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column_name: str, ddl_suffix: str) -> None:
        if column_name not in self._table_columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_name} {ddl_suffix}")

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL UNIQUE,
                    file_name TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    phash TEXT DEFAULT '',
                    width INTEGER DEFAULT 0,
                    height INTEGER DEFAULT 0,
                    format TEXT DEFAULT '',
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    normalized_name TEXT NOT NULL UNIQUE,
                    is_character INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tag_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tag_id INTEGER NOT NULL,
                    alias TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(tag_id) REFERENCES tags(id)
                );

                CREATE TABLE IF NOT EXISTS image_tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_id INTEGER NOT NULL,
                    tag_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    score REAL DEFAULT 1.0,
                    review_status TEXT DEFAULT 'approved',
                    review_reason TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(image_id, tag_id, source_type),
                    FOREIGN KEY(image_id) REFERENCES images(id),
                    FOREIGN KEY(tag_id) REFERENCES tags(id)
                );

                CREATE TABLE IF NOT EXISTS image_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_id INTEGER NOT NULL,
                    file_path TEXT NOT NULL UNIQUE,
                    file_name TEXT NOT NULL,
                    storage_type TEXT NOT NULL DEFAULT 'library',
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(image_id) REFERENCES images(id)
                );

                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_id INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    post_url TEXT NOT NULL,
                    image_url TEXT NOT NULL,
                    author TEXT DEFAULT '',
                    raw_tags TEXT DEFAULT '[]',
                    extra_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(image_id, image_url),
                    FOREIGN KEY(image_id) REFERENCES images(id)
                );

                CREATE TABLE IF NOT EXISTS crawl_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    tags_text TEXT DEFAULT '',
                    include_tags_text TEXT DEFAULT '',
                    exclude_tags_text TEXT DEFAULT '',
                    tag_match_mode TEXT DEFAULT 'exact',
                    status TEXT NOT NULL DEFAULT 'pending',
                    progress INTEGER DEFAULT 0,
                    error_log TEXT DEFAULT '',
                    result_summary TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS crawl_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    tag_id INTEGER DEFAULT 0,
                    tag_name TEXT NOT NULL,
                    normalized_tag TEXT NOT NULL,
                    query_text TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 1,
                    last_seen_source_uid TEXT DEFAULT '',
                    last_checked_at TEXT DEFAULT '',
                    last_success_at TEXT DEFAULT '',
                    last_error TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform, normalized_tag),
                    FOREIGN KEY(tag_id) REFERENCES tags(id)
                );

                CREATE TABLE IF NOT EXISTS review_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_id INTEGER NOT NULL,
                    tag_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    model_result TEXT DEFAULT '',
                    manual_result TEXT DEFAULT '',
                    reason TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(image_id) REFERENCES images(id),
                    FOREIGN KEY(tag_id) REFERENCES tags(id)
                );

                CREATE TABLE IF NOT EXISTS send_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    image_id INTEGER NOT NULL,
                    matched_tag TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    FOREIGN KEY(image_id) REFERENCES images(id)
                );

                CREATE TABLE IF NOT EXISTS platform_tag_terms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tag_id INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    term TEXT NOT NULL,
                    normalized_term TEXT NOT NULL,
                    term_type TEXT NOT NULL DEFAULT 'both',
                    source TEXT NOT NULL DEFAULT 'manual_review',
                    confidence REAL DEFAULT 1.0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform, normalized_term),
                    FOREIGN KEY(tag_id) REFERENCES tags(id)
                );

                CREATE TABLE IF NOT EXISTS rejected_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    post_url TEXT NOT NULL,
                    normalized_post_url TEXT NOT NULL,
                    source_uid TEXT DEFAULT '',
                    image_id INTEGER DEFAULT 0,
                    reason TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform, normalized_post_url)
                );

                CREATE TABLE IF NOT EXISTS tag_merge_identity_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_tag_id INTEGER NOT NULL,
                    target_tag_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    score REAL DEFAULT 0,
                    reasons_json TEXT DEFAULT '[]',
                    evidence_json TEXT DEFAULT '{}',
                    llm_result_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_tag_id, target_tag_id),
                    FOREIGN KEY(source_tag_id) REFERENCES tags(id),
                    FOREIGN KEY(target_tag_id) REFERENCES tags(id)
                );

                CREATE INDEX IF NOT EXISTS idx_images_active ON images(is_active);
                CREATE INDEX IF NOT EXISTS idx_images_sha256 ON images(sha256);
                CREATE INDEX IF NOT EXISTS idx_image_files_image_id ON image_files(image_id);
                CREATE INDEX IF NOT EXISTS idx_image_files_active ON image_files(is_active);
                CREATE INDEX IF NOT EXISTS idx_image_tags_tag_id ON image_tags(tag_id);
                CREATE INDEX IF NOT EXISTS idx_image_tags_review_status ON image_tags(review_status);
                CREATE INDEX IF NOT EXISTS idx_sources_platform ON sources(platform);
                CREATE INDEX IF NOT EXISTS idx_crawl_jobs_status ON crawl_jobs(status);
                CREATE INDEX IF NOT EXISTS idx_crawl_subscriptions_platform ON crawl_subscriptions(platform, enabled);
                CREATE INDEX IF NOT EXISTS idx_review_tasks_status ON review_tasks(status);
                CREATE INDEX IF NOT EXISTS idx_send_logs_session_id ON send_logs(session_id);
                CREATE INDEX IF NOT EXISTS idx_platform_tag_terms_tag_id ON platform_tag_terms(tag_id, platform);
                CREATE INDEX IF NOT EXISTS idx_rejected_sources_platform ON rejected_sources(platform, normalized_post_url);
                CREATE INDEX IF NOT EXISTS idx_tag_merge_identity_candidates_status ON tag_merge_identity_candidates(status, updated_at);
                """
            )
            try:
                conn.execute("PRAGMA journal_mode=DELETE;")
            except sqlite3.OperationalError:
                pass

            self._ensure_column(conn, 'images', 'phash', "TEXT DEFAULT ''")
            self._ensure_column(conn, 'tags', 'is_character', 'INTEGER DEFAULT 0')
            self._ensure_column(conn, 'image_tags', 'score', 'REAL DEFAULT 1.0')
            self._ensure_column(conn, 'image_tags', 'review_status', "TEXT DEFAULT 'approved'")
            self._ensure_column(conn, 'image_tags', 'review_reason', "TEXT DEFAULT ''")
            self._ensure_column(conn, 'image_tags', 'updated_at', "TEXT DEFAULT ''")
            self._ensure_column(conn, 'crawl_jobs', 'attempt_count', 'INTEGER DEFAULT 0')
            self._ensure_column(conn, 'crawl_jobs', 'include_tags_text', "TEXT DEFAULT ''")
            self._ensure_column(conn, 'crawl_jobs', 'exclude_tags_text', "TEXT DEFAULT ''")
            self._ensure_column(conn, 'crawl_jobs', 'tag_match_mode', "TEXT DEFAULT 'exact'")
            self._ensure_column(conn, 'crawl_subscriptions', 'tag_id', 'INTEGER DEFAULT 0')
            self._ensure_column(conn, 'crawl_subscriptions', 'query_text', "TEXT DEFAULT ''")
            self._ensure_column(conn, 'crawl_subscriptions', 'enabled', 'INTEGER DEFAULT 1')
            self._ensure_column(conn, 'crawl_subscriptions', 'last_seen_source_uid', "TEXT DEFAULT ''")
            self._ensure_column(conn, 'crawl_subscriptions', 'last_checked_at', "TEXT DEFAULT ''")
            self._ensure_column(conn, 'crawl_subscriptions', 'last_success_at', "TEXT DEFAULT ''")
            self._ensure_column(conn, 'crawl_subscriptions', 'last_error', "TEXT DEFAULT ''")
            self._ensure_file_locations_initialized(conn)

    @staticmethod
    def _infer_storage_type(file_path: str) -> str:
        normalized = str(file_path or "").replace("\\", "/").lower()
        if "/trash/" in normalized:
            return "trash"
        if "/images/restored/" in normalized:
            return "restored"
        if "/images/imported/" in normalized:
            return "imported"
        return "library"

    def _ensure_file_locations_initialized(self, conn: sqlite3.Connection) -> None:
        location_count = int(conn.execute("SELECT COUNT(*) AS c FROM image_files").fetchone()["c"])
        if location_count > 0:
            return
        rows = conn.execute(
            "SELECT id, file_path, file_name, is_active, created_at, updated_at FROM images",
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT OR IGNORE INTO image_files(image_id, file_path, file_name, storage_type, is_active, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(row["id"]),
                    str(row["file_path"]),
                    str(row["file_name"]),
                    self._infer_storage_type(str(row["file_path"])),
                    int(row["is_active"] or 0),
                    str(row["created_at"]),
                    str(row["updated_at"]),
                ),
            )

    def _upsert_file_location(
        self,
        conn: sqlite3.Connection,
        *,
        image_id: int,
        file_path: str,
        file_name: str,
        storage_type: str,
        now: str,
    ) -> None:
        row = conn.execute(
            "SELECT id FROM image_files WHERE file_path = ? LIMIT 1",
            (file_path,),
        ).fetchone()
        if row:
            conn.execute(
                """
                UPDATE image_files
                SET image_id = ?, file_name = ?, storage_type = ?, is_active = 1, updated_at = ?
                WHERE id = ?
                """,
                (image_id, file_name, storage_type, now, row["id"]),
            )
            return
        conn.execute(
            """
            INSERT INTO image_files(image_id, file_path, file_name, storage_type, is_active, created_at, updated_at)
            VALUES(?, ?, ?, ?, 1, ?, ?)
            """,
            (image_id, file_path, file_name, storage_type, now, now),
        )

    def _sync_image_file_state(
        self,
        conn: sqlite3.Connection,
        image_id: int,
        *,
        preferred_path: str | None = None,
        now: str | None = None,
    ) -> None:
        now = now or utcnow_str()
        image_row = conn.execute(
            "SELECT file_path FROM images WHERE id = ? LIMIT 1",
            (image_id,),
        ).fetchone()
        current_path = str(image_row["file_path"]) if image_row and image_row["file_path"] else ""
        locations = conn.execute(
            """
            SELECT file_path, file_name
            FROM image_files
            WHERE image_id = ? AND is_active = 1
            ORDER BY updated_at DESC, id DESC
            """,
            (image_id,),
        ).fetchall()

        chosen: sqlite3.Row | None = None
        if current_path:
            for row in locations:
                if str(row["file_path"]) == current_path and Path(str(row["file_path"])).exists():
                    chosen = row
                    break
        if chosen is None and preferred_path:
            for row in locations:
                if str(row["file_path"]) == preferred_path and Path(str(row["file_path"])).exists():
                    chosen = row
                    break
        if chosen is None:
            for row in locations:
                try:
                    if Path(str(row["file_path"])).exists():
                        chosen = row
                        break
                except OSError:
                    continue
        if chosen is None and locations:
            chosen = locations[0]

        if chosen is not None:
            conn.execute(
                """
                UPDATE images
                SET file_path = ?, file_name = ?, is_active = 1, updated_at = ?
                WHERE id = ?
                """,
                (str(chosen["file_path"]), str(chosen["file_name"]), now, image_id),
            )
            return

        conn.execute(
            "UPDATE images SET is_active = 0, updated_at = ? WHERE id = ?",
            (now, image_id),
        )

    def _set_preferred_image_variant(
        self,
        conn: sqlite3.Connection,
        *,
        image_id: int,
        file_path: str,
        file_name: str,
        sha256: str,
        phash: str,
        width: int,
        height: int,
        format_: str,
        now: str,
    ) -> None:
        conn.execute(
            "UPDATE image_files SET is_active = CASE WHEN file_path = ? THEN 1 ELSE 0 END, updated_at = ? WHERE image_id = ?",
            (file_path, now, image_id),
        )
        conn.execute(
            """
            UPDATE images
            SET file_path = ?, file_name = ?, sha256 = ?, phash = ?, width = ?, height = ?, format = ?, is_active = 1, updated_at = ?
            WHERE id = ?
            """,
            (file_path, file_name, sha256, phash, width, height, format_, now, image_id),
        )

    def upsert_image(
        self,
        *,
        file_path: str,
        file_name: str,
        sha256: str,
        width: int,
        height: int,
        format_: str,
        phash: str = '',
        storage_type: str = "library",
    ) -> int:
        now = utcnow_str()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT image_id FROM image_files WHERE file_path = ? LIMIT 1",
                (file_path,),
            ).fetchone()
            if row:
                image_id = int(row["image_id"])
                existing = conn.execute(
                    "SELECT phash, width, height, format FROM images WHERE id = ? LIMIT 1",
                    (image_id,),
                ).fetchone()
                next_phash = phash or str(existing["phash"] or "") if existing else phash
                next_width = int(width or (existing["width"] if existing else 0) or 0)
                next_height = int(height or (existing["height"] if existing else 0) or 0)
                next_format = format_ or str(existing["format"] or "") if existing else format_
                conn.execute(
                    """
                    UPDATE images
                    SET file_name = ?, sha256 = ?, phash = ?, width = ?, height = ?, format = ?, is_active = 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (file_name, sha256, next_phash, next_width, next_height, next_format, now, image_id),
                )
                self._upsert_file_location(
                    conn,
                    image_id=image_id,
                    file_path=file_path,
                    file_name=file_name,
                    storage_type=storage_type,
                    now=now,
                )
                self._sync_image_file_state(conn, image_id, preferred_path=file_path, now=now)
                return image_id

            existing = conn.execute(
                """
                SELECT id, phash, width, height, format
                FROM images
                WHERE sha256 = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (sha256,),
            ).fetchone()
            if existing:
                image_id = int(existing["id"])
                next_phash = phash or str(existing["phash"] or "")
                next_width = int(width or existing["width"] or 0)
                next_height = int(height or existing["height"] or 0)
                next_format = format_ or str(existing["format"] or "")
                conn.execute(
                    """
                    UPDATE images
                    SET phash = ?, width = ?, height = ?, format = ?, is_active = 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (next_phash, next_width, next_height, next_format, now, image_id),
                )
                self._upsert_file_location(
                    conn,
                    image_id=image_id,
                    file_path=file_path,
                    file_name=file_name,
                    storage_type=storage_type,
                    now=now,
                )
                self._sync_image_file_state(conn, image_id, preferred_path=file_path, now=now)
                return image_id

            cursor = conn.execute(
                """
                INSERT INTO images(file_path, file_name, sha256, phash, width, height, format, is_active, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (file_path, file_name, sha256, phash, width, height, format_, now, now),
            )
            image_id = int(cursor.lastrowid)
            self._upsert_file_location(
                conn,
                image_id=image_id,
                file_path=file_path,
                file_name=file_name,
                storage_type=storage_type,
                now=now,
            )
            return image_id

    def find_similar_images_by_phash(self, phash: str, *, max_distance: int = 8, limit: int = 10) -> list[sqlite3.Row]:
        if not phash:
            return []
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, file_path, file_name, sha256, phash, width, height, format, updated_at
                FROM images
                WHERE is_active = 1 AND phash != ''
                ORDER BY id DESC
                LIMIT 500
                """
            ).fetchall()
        matches: list[tuple[int, sqlite3.Row]] = []
        for row in rows:
            distance = hamming_distance(phash, str(row["phash"] or ""))
            if distance <= max_distance:
                matches.append((distance, row))
        matches.sort(key=lambda item: (item[0], -int(item[1]["id"])))
        return [row for _, row in matches[:limit]]

    def get_image_row(self, image_id: int) -> sqlite3.Row | None:
        with self._lock, self._connect() as conn:
            return conn.execute("SELECT * FROM images WHERE id = ? LIMIT 1", (image_id,)).fetchone()

    def attach_image_variant(
        self,
        image_id: int,
        *,
        file_path: str,
        file_name: str,
        sha256: str,
        phash: str,
        width: int,
        height: int,
        format_: str,
        storage_type: str = "imported",
        make_primary: bool = False,
    ) -> int:
        now = utcnow_str()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT id FROM images WHERE id = ? LIMIT 1", (image_id,)).fetchone()
            if not row:
                raise ValueError(f"image not found: {image_id}")
            self._upsert_file_location(
                conn,
                image_id=image_id,
                file_path=file_path,
                file_name=file_name,
                storage_type=storage_type,
                now=now,
            )
            if make_primary:
                self._set_preferred_image_variant(
                    conn,
                    image_id=image_id,
                    file_path=file_path,
                    file_name=file_name,
                    sha256=sha256,
                    phash=phash,
                    width=width,
                    height=height,
                    format_=format_,
                    now=now,
                )
            else:
                conn.execute(
                    "UPDATE image_files SET is_active = 0, updated_at = ? WHERE image_id = ? AND file_path = ?",
                    (now, image_id, file_path),
                )
                self._sync_image_file_state(conn, image_id, now=now)
        return image_id

    @staticmethod
    def _preferred_image_tag_status(*statuses: str) -> str:
        valid = [str(item or "").strip() for item in statuses if str(item or "").strip()]
        if not valid:
            return "approved"
        return max(valid, key=lambda item: (IMAGE_TAG_STATUS_PRIORITY.get(item, -1), item))

    def merge_images(
        self,
        primary_image_id: int,
        duplicate_image_id: int,
        *,
        preferred_file_path: str | None = None,
        preferred_file_name: str | None = None,
        preferred_sha256: str | None = None,
        preferred_phash: str | None = None,
        preferred_width: int | None = None,
        preferred_height: int | None = None,
        preferred_format: str | None = None,
    ) -> tuple[bool, str]:
        if primary_image_id == duplicate_image_id:
            return False, "cannot merge the same image"

        now = utcnow_str()
        with self._lock, self._connect() as conn:
            primary = conn.execute("SELECT * FROM images WHERE id = ? LIMIT 1", (primary_image_id,)).fetchone()
            duplicate = conn.execute("SELECT * FROM images WHERE id = ? LIMIT 1", (duplicate_image_id,)).fetchone()
            if not primary or not duplicate:
                return False, "image_not_found"

            conn.execute(
                "UPDATE image_files SET image_id = ?, updated_at = ? WHERE image_id = ?",
                (primary_image_id, now, duplicate_image_id),
            )

            duplicate_tags = conn.execute(
                """
                SELECT tag_id, source_type, score, review_status, review_reason, created_at
                FROM image_tags
                WHERE image_id = ?
                """,
                (duplicate_image_id,),
            ).fetchall()
            for row in duplicate_tags:
                existing = conn.execute(
                    """
                    SELECT id, score, review_status, review_reason, created_at
                    FROM image_tags
                    WHERE image_id = ? AND tag_id = ? AND source_type = ?
                    LIMIT 1
                    """,
                    (primary_image_id, int(row["tag_id"]), str(row["source_type"])),
                ).fetchone()
                merged_status = self._preferred_image_tag_status(
                    str(row["review_status"] or ""),
                    str(existing["review_status"] or "") if existing else "",
                )
                merged_score = max(
                    float(row["score"] or 0.0),
                    float(existing["score"] or 0.0) if existing else 0.0,
                )
                merged_reason = (
                    str(existing["review_reason"] or "") if existing and str(existing["review_reason"] or "").strip()
                    else str(row["review_reason"] or "")
                )
                if existing:
                    conn.execute(
                        """
                        UPDATE image_tags
                        SET score = ?, review_status = ?, review_reason = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (merged_score, merged_status, merged_reason, now, int(existing["id"])),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO image_tags(image_id, tag_id, source_type, score, review_status, review_reason, created_at, updated_at)
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            primary_image_id,
                            int(row["tag_id"]),
                            str(row["source_type"]),
                            merged_score,
                            merged_status,
                            merged_reason,
                            str(row["created_at"] or now),
                            now,
                        ),
                    )
            conn.execute("DELETE FROM image_tags WHERE image_id = ?", (duplicate_image_id,))

            duplicate_sources = conn.execute("SELECT * FROM sources WHERE image_id = ?", (duplicate_image_id,)).fetchall()
            for row in duplicate_sources:
                existing = conn.execute(
                    "SELECT id FROM sources WHERE image_id = ? AND image_url = ? LIMIT 1",
                    (primary_image_id, str(row["image_url"])),
                ).fetchone()
                if existing:
                    continue
                conn.execute(
                    """
                    INSERT INTO sources(image_id, platform, post_url, image_url, author, raw_tags, extra_json, created_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        primary_image_id,
                        str(row["platform"]),
                        str(row["post_url"]),
                        str(row["image_url"]),
                        str(row["author"] or ""),
                        str(row["raw_tags"] or "[]"),
                        str(row["extra_json"] or "{}"),
                        str(row["created_at"] or now),
                    ),
                )
            conn.execute("DELETE FROM sources WHERE image_id = ?", (duplicate_image_id,))

            duplicate_reviews = conn.execute("SELECT * FROM review_tasks WHERE image_id = ?", (duplicate_image_id,)).fetchall()
            for row in duplicate_reviews:
                existing = conn.execute(
                    "SELECT id FROM review_tasks WHERE image_id = ? AND tag_id = ? LIMIT 1",
                    (primary_image_id, int(row["tag_id"])),
                ).fetchone()
                if existing:
                    conn.execute("DELETE FROM review_tasks WHERE id = ?", (int(row["id"]),))
                else:
                    conn.execute(
                        "UPDATE review_tasks SET image_id = ?, updated_at = ? WHERE id = ?",
                        (primary_image_id, now, int(row["id"])),
                    )

            conn.execute("UPDATE send_logs SET image_id = ? WHERE image_id = ?", (primary_image_id, duplicate_image_id))

            preferred_path = str(preferred_file_path or "").strip() or str(primary["file_path"])
            preferred_name = str(preferred_file_name or "").strip() or str(primary["file_name"])
            preferred_sha = str(preferred_sha256 or "").strip() or str(primary["sha256"])
            preferred_ph = str(preferred_phash or "").strip() or str(primary["phash"] or "")
            width = int(preferred_width if preferred_width is not None else int(primary["width"] or 0))
            height = int(preferred_height if preferred_height is not None else int(primary["height"] or 0))
            format_name = str(preferred_format or "").strip() or str(primary["format"] or "")

            duplicate_current_path = str(duplicate["file_path"] or "").strip()
            if preferred_path and preferred_path == duplicate_current_path and preferred_path != str(primary["file_path"] or ""):
                placeholder_path = f"{preferred_path}#merged-{duplicate_image_id}"
                conn.execute(
                    "UPDATE images SET file_path = ?, updated_at = ? WHERE id = ?",
                    (placeholder_path, now, duplicate_image_id),
                )

            self._set_preferred_image_variant(
                conn,
                image_id=primary_image_id,
                file_path=preferred_path,
                file_name=preferred_name,
                sha256=preferred_sha,
                phash=preferred_ph,
                width=width,
                height=height,
                format_=format_name,
                now=now,
            )
            conn.execute(
                "UPDATE image_files SET is_active = 0, updated_at = ? WHERE image_id = ? AND file_path != ?",
                (now, primary_image_id, preferred_path),
            )
            conn.execute(
                """
                UPDATE images
                SET is_active = 0, updated_at = ?
                WHERE id = ?
                """,
                (now, duplicate_image_id),
            )
        return True, f"merged {duplicate_image_id} -> {primary_image_id}"

    def mark_missing_files_inactive(self, library_root: str, seen_paths: set[str]) -> int:
        root = str(Path(library_root).resolve())
        count = 0
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, image_id, file_path
                FROM image_files
                WHERE storage_type = 'library' AND file_path LIKE ?
                """,
                (f'{root}%',),
            ).fetchall()
            affected_image_ids: set[int] = set()
            for row in rows:
                if row['file_path'] not in seen_paths:
                    conn.execute(
                        "UPDATE image_files SET is_active = 0, updated_at = ? WHERE id = ?",
                        (utcnow_str(), row['id']),
                    )
                    affected_image_ids.add(int(row["image_id"]))
                    count += 1
            for image_id in affected_image_ids:
                self._sync_image_file_state(conn, image_id)
        return count

    def get_or_create_tag(self, tag_name: str, is_character: bool | None = None) -> int:
        normalized = normalize_tag_name(tag_name)
        if not normalized:
            raise ValueError('tag 不能为空')
        now = utcnow_str()
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT id, is_character FROM tags WHERE normalized_name = ?', (normalized,)).fetchone()
            if row:
                if is_character is True and int(row['is_character']) != 1:
                    conn.execute('UPDATE tags SET is_character = 1 WHERE id = ?', (row['id'],))
                return int(row['id'])
            cursor = conn.execute(
                'INSERT INTO tags(name, normalized_name, is_character, created_at) VALUES(?, ?, ?, ?)',
                (tag_name.strip(), normalized, 1 if is_character else 0, now),
            )
            return int(cursor.lastrowid)

    def create_or_get_tag(self, tag_name: str, is_character: bool | None = None) -> tuple[bool, dict[str, Any]]:
        tag_text = str(tag_name or '').strip()
        normalized = normalize_tag_name(tag_text)
        if not tag_text or not normalized:
            return False, {'message': 'tag 不能为空'}
        now = utcnow_str()
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM tags WHERE normalized_name = ? LIMIT 1', (normalized,)).fetchone()
            created = False
            if row:
                if is_character is True and int(row['is_character'] or 0) != 1:
                    conn.execute('UPDATE tags SET is_character = 1 WHERE id = ?', (int(row['id']),))
                    row = conn.execute('SELECT * FROM tags WHERE id = ? LIMIT 1', (int(row['id']),)).fetchone()
            else:
                cursor = conn.execute(
                    'INSERT INTO tags(name, normalized_name, is_character, created_at) VALUES(?, ?, ?, ?)',
                    (tag_text, normalized, 1 if is_character else 0, now),
                )
                row = conn.execute('SELECT * FROM tags WHERE id = ? LIMIT 1', (int(cursor.lastrowid),)).fetchone()
                created = True
        if not row:
            return False, {'message': 'tag 创建失败'}
        return True, {
            'message': ('已新增主 tag' if created else '已复用已有主 tag'),
            'tag': {
                'id': int(row['id']),
                'name': str(row['name']),
                'is_character': bool(int(row['is_character'] or 0)),
            },
            'created': created,
        }

    def set_tag_character(self, tag_name: str, is_character: bool) -> tuple[bool, str]:
        tag_id = self.get_tag_id(tag_name)
        if tag_id is None:
            return False, f'tag 不存在：{tag_name}'
        with self._lock, self._connect() as conn:
            conn.execute('UPDATE tags SET is_character = ? WHERE id = ?', (1 if is_character else 0, tag_id))
        state = '角色 tag' if is_character else '普通 tag'
        return True, f'已将 {tag_name} 标记为：{state}'

    def get_tag_row(self, tag_name: str) -> sqlite3.Row | None:
        normalized = normalize_tag_name(tag_name)
        with self._lock, self._connect() as conn:
            return conn.execute('SELECT * FROM tags WHERE normalized_name = ?', (normalized,)).fetchone()

    def get_tag_row_by_id(self, tag_id: int) -> sqlite3.Row | None:
        with self._lock, self._connect() as conn:
            return conn.execute('SELECT * FROM tags WHERE id = ?', (tag_id,)).fetchone()

    @staticmethod
    def _resolve_tag_exact_conn(conn: sqlite3.Connection, query: str) -> tuple[sqlite3.Row | None, str]:
        normalized = normalize_tag_name(query)
        if not normalized:
            return None, ''
        exact_tag = conn.execute('SELECT * FROM tags WHERE normalized_name = ? LIMIT 1', (normalized,)).fetchone()
        if exact_tag:
            return exact_tag, 'exact_tag'
        exact_alias = conn.execute(
            """
            SELECT t.*
            FROM tag_aliases a
            JOIN tags t ON t.id = a.tag_id
            WHERE a.normalized_alias = ?
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()
        if exact_alias:
            return exact_alias, 'exact_alias'
        return None, ''

    @staticmethod
    def _get_alias_usage_conn(conn: sqlite3.Connection, normalized_alias: str) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT a.id, a.tag_id, a.alias, t.name AS tag_name
            FROM tag_aliases a
            JOIN tags t ON t.id = a.tag_id
            WHERE a.normalized_alias = ?
            LIMIT 1
            """,
            (normalized_alias,),
        ).fetchone()

    def _insert_alias_conn(
        self,
        conn: sqlite3.Connection,
        *,
        tag_id: int,
        alias: str,
        now: str | None = None,
    ) -> tuple[bool, str]:
        alias_text = str(alias or '').strip()
        if not alias_text:
            return False, 'alias 不能为空'
        target = conn.execute('SELECT id, name, normalized_name FROM tags WHERE id = ? LIMIT 1', (tag_id,)).fetchone()
        if not target:
            return False, f'tag 不存在：{tag_id}'

        normalized_alias = normalize_tag_name(alias_text)
        if not normalized_alias:
            return False, 'alias 不能为空'
        if normalized_alias == str(target['normalized_name'] or ''):
            return False, f'alias 不能与主 tag 相同：{alias_text}'

        conflict_tag = conn.execute(
            'SELECT id, name FROM tags WHERE normalized_name = ? LIMIT 1',
            (normalized_alias,),
        ).fetchone()
        if conflict_tag:
            if int(conflict_tag['id']) == int(tag_id):
                return False, f'alias 不能与主 tag 相同：{alias_text}'
            return False, f'alias 与现有 tag 冲突：{conflict_tag["name"]}'

        alias_usage = self._get_alias_usage_conn(conn, normalized_alias)
        if alias_usage:
            if int(alias_usage['tag_id']) == int(tag_id):
                return False, f'alias 已存在：{alias_text}'
            return False, f'alias 已被 {alias_usage["tag_name"]} 使用：{alias_text}'

        conn.execute(
            'INSERT INTO tag_aliases(tag_id, alias, normalized_alias, created_at) VALUES(?, ?, ?, ?)',
            (tag_id, alias_text, normalized_alias, now or utcnow_str()),
        )
        return True, f'已添加别名：{target["name"]} -> {alias_text}'

    @staticmethod
    def _preferred_review_task_status(*statuses: str) -> str:
        valid = [str(item or '').strip() for item in statuses if str(item or '').strip()]
        if not valid:
            return 'pending'
        return max(valid, key=lambda item: (IMAGE_TAG_STATUS_PRIORITY.get(item, -1), item))

    def _merge_tag_into_conn(
        self,
        conn: sqlite3.Connection,
        *,
        target_id: int,
        target_name: str,
        source_id: int,
        now: str,
    ) -> dict[str, Any]:
        target = conn.execute('SELECT * FROM tags WHERE id = ? LIMIT 1', (target_id,)).fetchone()
        source = conn.execute('SELECT * FROM tags WHERE id = ? LIMIT 1', (source_id,)).fetchone()
        if not target or not source:
            raise ValueError('tag_not_found')
        if int(target['id']) == int(source['id']):
            return {
                'source_name': str(source['name']),
                'image_links_migrated': 0,
                'review_tasks_migrated': 0,
                'review_tasks_merged': 0,
                'aliases_migrated': 0,
                'aliases_skipped': [],
                'source_name_alias_added': False,
                'subscriptions_migrated': 0,
                'subscriptions_merged': 0,
                'subscriptions_removed': 0,
            }

        image_links_migrated = 0
        source_image_tags = conn.execute(
            """
            SELECT id, image_id, source_type, score, review_status, review_reason, created_at
            FROM image_tags
            WHERE tag_id = ?
            """,
            (source_id,),
        ).fetchall()
        for row in source_image_tags:
            existing = conn.execute(
                """
                SELECT id, score, review_status, review_reason
                FROM image_tags
                WHERE image_id = ? AND tag_id = ? AND source_type = ?
                LIMIT 1
                """,
                (int(row['image_id']), target_id, str(row['source_type'])),
            ).fetchone()
            merged_status = self._preferred_image_tag_status(
                str(existing['review_status'] or '') if existing else '',
                str(row['review_status'] or ''),
            )
            merged_score = max(
                float(existing['score'] or 0.0) if existing else 0.0,
                float(row['score'] or 0.0),
            )
            merged_reason = (
                str(existing['review_reason'] or '') if existing and str(existing['review_reason'] or '').strip()
                else str(row['review_reason'] or '')
            )
            if existing:
                conn.execute(
                    """
                    UPDATE image_tags
                    SET score = ?, review_status = ?, review_reason = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (merged_score, merged_status, merged_reason, now, int(existing['id'])),
                )
                conn.execute('DELETE FROM image_tags WHERE id = ?', (int(row['id']),))
            else:
                conn.execute(
                    """
                    UPDATE image_tags
                    SET tag_id = ?, review_status = ?, review_reason = ?, score = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (target_id, merged_status, merged_reason, merged_score, now, int(row['id'])),
                )
            image_links_migrated += 1

        review_tasks_migrated = 0
        review_tasks_merged = 0
        source_reviews = conn.execute(
            """
            SELECT id, image_id, status, model_result, manual_result, reason, created_at
            FROM review_tasks
            WHERE tag_id = ?
            """,
            (source_id,),
        ).fetchall()
        for row in source_reviews:
            existing = conn.execute(
                """
                SELECT id, status, model_result, manual_result, reason
                FROM review_tasks
                WHERE image_id = ? AND tag_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(row['image_id']), target_id),
            ).fetchone()
            merged_status = self._preferred_review_task_status(
                str(existing['status'] or '') if existing else '',
                str(row['status'] or ''),
            )
            merged_model_result = (
                str(existing['model_result'] or '') if existing and str(existing['model_result'] or '').strip()
                else str(row['model_result'] or '')
            )
            merged_manual_result = (
                str(existing['manual_result'] or '') if existing and str(existing['manual_result'] or '').strip()
                else str(row['manual_result'] or '')
            )
            merged_reason = (
                str(existing['reason'] or '') if existing and str(existing['reason'] or '').strip()
                else str(row['reason'] or '')
            )
            if existing:
                conn.execute(
                    """
                    UPDATE review_tasks
                    SET status = ?, model_result = ?, manual_result = ?, reason = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (merged_status, merged_model_result, merged_manual_result, merged_reason, now, int(existing['id'])),
                )
                conn.execute('DELETE FROM review_tasks WHERE id = ?', (int(row['id']),))
                review_tasks_merged += 1
            else:
                conn.execute(
                    """
                    UPDATE review_tasks
                    SET tag_id = ?, status = ?, model_result = ?, manual_result = ?, reason = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (target_id, merged_status, merged_model_result, merged_manual_result, merged_reason, now, int(row['id'])),
                )
                review_tasks_migrated += 1

        source_alias_rows = conn.execute(
            'SELECT alias FROM tag_aliases WHERE tag_id = ? ORDER BY id ASC',
            (source_id,),
        ).fetchall()
        conn.execute('DELETE FROM tag_aliases WHERE tag_id = ?', (source_id,))
        aliases_migrated = 0
        aliases_skipped: list[str] = []
        for row in source_alias_rows:
            alias = str(row['alias'] or '').strip()
            if not alias:
                continue
            ok, message = self._insert_alias_conn(conn, tag_id=target_id, alias=alias, now=now)
            if ok:
                aliases_migrated += 1
            else:
                aliases_skipped.append(f'{alias}（{message}）')

        source_platform_terms = conn.execute(
            'SELECT platform, term, term_type, source, confidence FROM platform_tag_terms WHERE tag_id = ? ORDER BY id ASC',
            (source_id,),
        ).fetchall()
        conn.execute('DELETE FROM platform_tag_terms WHERE tag_id = ?', (source_id,))
        for row in source_platform_terms:
            self._upsert_platform_term_conn(
                conn,
                tag_id=target_id,
                platform=str(row['platform'] or ''),
                term=str(row['term'] or ''),
                term_type=str(row['term_type'] or 'both'),
                source=str(row['source'] or 'manual_review'),
                confidence=float(row['confidence'] or 0.0),
                now=now,
            )

        source_name_alias_added = False
        subscriptions_migrated = 0
        subscriptions_merged = 0
        subscriptions_removed = 0
        source_subscription_rows = conn.execute(
            'SELECT * FROM crawl_subscriptions WHERE tag_id = ? ORDER BY id ASC',
            (source_id,),
        ).fetchall()
        target_normalized = str(target['normalized_name'] or normalize_tag_name(str(target['name'] or '')))
        for row in source_subscription_rows:
            platform_text = str(row['platform'] or '').strip()
            target_subscription = conn.execute(
                """
                SELECT *
                FROM crawl_subscriptions
                WHERE platform = ? AND normalized_tag = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (platform_text, target_normalized),
            ).fetchone()
            if target_subscription and int(target_subscription['id']) != int(row['id']):
                merged_query_text = str(target_subscription['query_text'] or '').strip() or str(row['query_text'] or '').strip()
                merged_enabled = bool(int(target_subscription['enabled'] or 0)) or bool(int(row['enabled'] or 0))
                merged_last_seen_source_uid = str(target_subscription['last_seen_source_uid'] or '').strip() or str(row['last_seen_source_uid'] or '').strip()
                merged_last_checked_at = max(
                    str(target_subscription['last_checked_at'] or '').strip(),
                    str(row['last_checked_at'] or '').strip(),
                )
                merged_last_success_at = max(
                    str(target_subscription['last_success_at'] or '').strip(),
                    str(row['last_success_at'] or '').strip(),
                )
                merged_last_error = str(target_subscription['last_error'] or '').strip() or str(row['last_error'] or '').strip()
                conn.execute(
                    """
                    UPDATE crawl_subscriptions
                    SET tag_id = ?, tag_name = ?, normalized_tag = ?, query_text = ?, enabled = ?,
                        last_seen_source_uid = ?, last_checked_at = ?, last_success_at = ?, last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        target_id,
                        target_name,
                        target_normalized,
                        merged_query_text,
                        1 if merged_enabled else 0,
                        merged_last_seen_source_uid,
                        merged_last_checked_at,
                        merged_last_success_at,
                        merged_last_error,
                        now,
                        int(target_subscription['id']),
                    ),
                )
                conn.execute('DELETE FROM crawl_subscriptions WHERE id = ?', (int(row['id']),))
                subscriptions_removed += 1
                subscriptions_merged += 1
            else:
                conn.execute(
                    """
                    UPDATE crawl_subscriptions
                    SET tag_id = ?, tag_name = ?, normalized_tag = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (target_id, target_name, target_normalized, now, int(row['id'])),
                )
                subscriptions_migrated += 1
        conn.execute('DELETE FROM tags WHERE id = ?', (source_id,))

        ok, _ = self._insert_alias_conn(conn, tag_id=target_id, alias=str(source['name']), now=now)
        source_name_alias_added = ok

        if int(source['is_character'] or 0) == 1 and int(target['is_character'] or 0) != 1:
            conn.execute('UPDATE tags SET is_character = 1 WHERE id = ?', (target_id,))

        return {
            'source_name': str(source['name']),
            'image_links_migrated': image_links_migrated,
            'review_tasks_migrated': review_tasks_migrated,
            'review_tasks_merged': review_tasks_merged,
            'aliases_migrated': aliases_migrated,
            'aliases_skipped': aliases_skipped,
            'source_name_alias_added': source_name_alias_added,
            'subscriptions_migrated': subscriptions_migrated,
            'subscriptions_merged': subscriptions_merged,
            'subscriptions_removed': subscriptions_removed,
        }

    def link_image_tag(self, image_id: int, tag_id: int, source_type: str = 'directory', review_status: str = 'approved', score: float = 1.0, review_reason: str = '') -> None:
        now = utcnow_str()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO image_tags(image_id, tag_id, source_type, score, review_status, review_reason, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(image_id, tag_id, source_type)
                DO UPDATE SET
                    score = excluded.score,
                    review_status = excluded.review_status,
                    review_reason = excluded.review_reason,
                    updated_at = excluded.updated_at
                """,
                (image_id, tag_id, source_type, score, review_status, review_reason, now, now),
            )

    def update_image_tag_review(self, image_id: int, tag_id: int, status: str, reason: str = '', source_type_prefix: str | None = None) -> None:
        sql = 'UPDATE image_tags SET review_status = ?, review_reason = ?, updated_at = ? WHERE image_id = ? AND tag_id = ?'
        params: list[Any] = [status, reason, utcnow_str(), image_id, tag_id]
        if source_type_prefix:
            sql += ' AND source_type LIKE ?'
            params.append(f'{source_type_prefix}%')
        with self._lock, self._connect() as conn:
            conn.execute(sql, params)

    def add_alias(self, tag_name: str, alias: str) -> tuple[bool, str]:
        tag_name = tag_name.strip()
        alias = alias.strip()
        if not tag_name or not alias:
            return False, 'tag \u548c alias \u90FD\u4E0D\u80FD\u4E3A\u7A7A\u3002'
        tag_id = self.get_tag_id(tag_name)
        if tag_id is None:
            return False, f'tag \u4E0D\u5B58\u5728\uFF1A{tag_name}'
        with self._lock, self._connect() as conn:
            return self._insert_alias_conn(conn, tag_id=tag_id, alias=alias, now=utcnow_str())

    def remove_alias(self, tag_name: str, alias: str) -> tuple[bool, str]:
        tag_id = self.get_tag_id(tag_name.strip())
        if tag_id is None:
            return False, f'tag 不存在：{tag_name}'
        normalized = normalize_tag_name(alias)
        with self._lock, self._connect() as conn:
            cursor = conn.execute('DELETE FROM tag_aliases WHERE tag_id = ? AND normalized_alias = ?', (tag_id, normalized))
            if cursor.rowcount <= 0:
                return False, f'别名不存在：{alias}'
        return True, f'已删除别名：{tag_name} -> {alias}'

    def list_aliases(self, tag_name: str) -> list[str]:
        tag_id = self.get_tag_id(tag_name.strip())
        if tag_id is None:
            return []
        with self._lock, self._connect() as conn:
            rows = conn.execute('SELECT alias FROM tag_aliases WHERE tag_id = ? ORDER BY alias ASC', (tag_id,)).fetchall()
            return [str(row['alias']) for row in rows]


    @staticmethod
    def _normalize_platform_term_type(term_type: str) -> str:
        text = str(term_type or 'both').strip().lower()
        if text not in {'query', 'match', 'both'}:
            return 'both'
        return text

    @classmethod
    def _platform_term_type_union(cls, left: str, right: str) -> str:
        left_normalized = cls._normalize_platform_term_type(left)
        right_normalized = cls._normalize_platform_term_type(right)
        if left_normalized == right_normalized:
            return left_normalized
        return 'both'

    @classmethod
    def _platform_term_type_matches(cls, stored_term_type: str, expected_types: set[str]) -> bool:
        normalized = cls._normalize_platform_term_type(stored_term_type)
        if normalized == 'both':
            return bool({'query', 'match'} & expected_types)
        return normalized in expected_types

    @staticmethod
    def _looks_like_platform_term(term: str) -> bool:
        text = str(term or '').strip()
        normalized = normalize_tag_name(text)
        if not text or not normalized or len(text) > 60 or len(normalized) <= 1:
            return False
        lowered = text.lower()
        blacklist = {
            'pixiv', 'illustration', 'fanart', 'art', 'image', 'images',
            '插画', '图片', '图', '壁纸', '漫画', '约稿', '头像',
            '女の子', '女孩子', '世界计划', 'プロセカ', 'projectsekai', 'prsk_fa',
            'プロジェクトセカイ', 'プロジェクトセカイカラフルステージ', 'pjsk',
            'ニーゴ', '25時、ナイトコードで。', '25时，在夜之电台',
        }
        if lowered in blacklist:
            return False
        if lowered.isdigit() or 'http' in lowered:
            return False
        if 'users入り' in lowered or 'bookmarks' in lowered:
            return False
        if any(ch in text for ch in '/|,，'):
            return False
        return True

    @staticmethod
    def _resolve_platform_term_exact_conn(conn: sqlite3.Connection, platform: str, query: str) -> sqlite3.Row | None:
        normalized = normalize_tag_name(query)
        if not normalized:
            return None
        return conn.execute(
            """
            SELECT p.id, p.tag_id, p.term, p.term_type, p.source, p.confidence,
                   t.name AS tag_name
            FROM platform_tag_terms p
            JOIN tags t ON t.id = p.tag_id
            WHERE p.platform = ? AND p.normalized_term = ?
            LIMIT 1
            """,
            (str(platform or '').strip().lower(), normalized),
        ).fetchone()

    def _upsert_platform_term_conn(
        self,
        conn: sqlite3.Connection,
        *,
        tag_id: int,
        platform: str,
        term: str,
        term_type: str = 'both',
        source: str = 'manual_review',
        confidence: float = 1.0,
        now: str | None = None,
    ) -> tuple[bool, str]:
        platform_text = str(platform or '').strip().lower()
        term_text = str(term or '').strip()
        normalized_term = normalize_tag_name(term_text)
        normalized_term_type = self._normalize_platform_term_type(term_type)
        if not platform_text:
            return False, 'platform 不能为空'
        if not term_text or not normalized_term:
            return False, 'platform term 不能为空'

        target = conn.execute(
            'SELECT id, name, normalized_name FROM tags WHERE id = ? LIMIT 1',
            (tag_id,),
        ).fetchone()
        if not target:
            return False, f'tag 不存在：{tag_id}'
        if normalized_term == str(target['normalized_name'] or ''):
            return False, f'{platform_text} term 与主 tag 相同，无需单独保存'
        alias_exists = conn.execute(
            'SELECT 1 FROM tag_aliases WHERE tag_id = ? AND normalized_alias = ? LIMIT 1',
            (tag_id, normalized_term),
        ).fetchone()
        if alias_exists:
            return False, f'{platform_text} term 已被 alias 覆盖：{term_text}'

        existing = conn.execute(
            'SELECT id, tag_id, term_type FROM platform_tag_terms WHERE platform = ? AND normalized_term = ? LIMIT 1',
            (platform_text, normalized_term),
        ).fetchone()
        current_time = now or utcnow_str()
        if existing:
            if int(existing['tag_id']) != int(tag_id):
                conflict = conn.execute('SELECT name FROM tags WHERE id = ? LIMIT 1', (int(existing['tag_id']),)).fetchone()
                conflict_name = str(conflict['name']) if conflict else str(existing['tag_id'])
                return False, f'{platform_text} term 已映射到其他 tag：{conflict_name}'
            merged_type = self._platform_term_type_union(str(existing['term_type'] or ''), normalized_term_type)
            conn.execute(
                """
                UPDATE platform_tag_terms
                SET term = ?, term_type = ?, source = ?, confidence = ?, updated_at = ?
                WHERE id = ?
                """,
                (term_text, merged_type, str(source or 'manual_review'), float(confidence or 0.0), current_time, int(existing['id'])),
            )
            return True, f'已更新 {platform_text} term：{target["name"]} -> {term_text}'

        conn.execute(
            """
            INSERT INTO platform_tag_terms(
                tag_id, platform, term, normalized_term, term_type, source, confidence, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(tag_id),
                platform_text,
                term_text,
                normalized_term,
                normalized_term_type,
                str(source or 'manual_review'),
                float(confidence or 0.0),
                current_time,
                current_time,
            ),
        )
        return True, f'已添加 {platform_text} term：{target["name"]} -> {term_text}'

    def add_platform_term(
        self,
        tag_name: str,
        term: str,
        *,
        platform: str = 'pixiv',
        term_type: str = 'both',
        source: str = 'manual_review',
        confidence: float = 1.0,
    ) -> tuple[bool, str]:
        tag_row = self.get_tag_row(tag_name)
        if not tag_row:
            return False, f'tag 不存在：{tag_name}'
        with self._lock, self._connect() as conn:
            return self._upsert_platform_term_conn(
                conn,
                tag_id=int(tag_row['id']),
                platform=platform,
                term=term,
                term_type=term_type,
                source=source,
                confidence=confidence,
                now=utcnow_str(),
            )

    def resolve_platform_term(self, platform: str, query: str) -> MatchResult:
        normalized = normalize_tag_name(query)
        if not normalized:
            return MatchResult(matched=False)
        with self._lock, self._connect() as conn:
            row = self._resolve_platform_term_exact_conn(conn, platform, query)
        if not row:
            return MatchResult(matched=False)
        return MatchResult(
            matched=True,
            tag_id=int(row['tag_id']),
            tag_name=str(row['tag_name']),
            match_type=f'platform:{str(platform or '').strip().lower()}',
        )

    def list_platform_terms(
        self,
        *,
        tag_name: str = '',
        tag_id: int | None = None,
        platform: str = '',
        term_types: Iterable[str] | None = None,
        limit: int = 100,
        offset: int = 0,
        keyword: str = '',
    ) -> list[sqlite3.Row]:
        resolved_tag_id = tag_id
        if resolved_tag_id is None and tag_name:
            resolved_tag_id = self.get_tag_id(tag_name)
            if resolved_tag_id is None:
                return []
        sql = """
            SELECT p.id, p.tag_id, p.platform, p.term, p.normalized_term, p.term_type, p.source, p.confidence,
                   p.created_at, p.updated_at, t.name AS tag_name
            FROM platform_tag_terms p
            JOIN tags t ON t.id = p.tag_id
        """
        clauses: list[str] = []
        params: list[Any] = []
        if resolved_tag_id is not None:
            clauses.append('p.tag_id = ?')
            params.append(int(resolved_tag_id))
        if platform:
            clauses.append('p.platform = ?')
            params.append(str(platform).strip().lower())
        keyword_text = str(keyword or '').strip()
        normalized_keyword = normalize_tag_name(keyword_text)
        if keyword_text:
            keyword_clauses: list[str] = []
            keyword_params: list[Any] = []
            if normalized_keyword:
                keyword_clauses.extend(['p.normalized_term LIKE ?', 't.normalized_name LIKE ?'])
                keyword_params.extend([f'%{normalized_keyword}%', f'%{normalized_keyword}%'])
            keyword_clauses.extend(['p.term LIKE ?', 't.name LIKE ?'])
            keyword_params.extend([f'%{keyword_text}%', f'%{keyword_text}%'])
            clauses.append('(' + ' OR '.join(keyword_clauses) + ')')
            params.extend(keyword_params)
        normalized_term_types = {
            self._normalize_platform_term_type(item)
            for item in (term_types or [])
            if str(item or '').strip()
        }
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY p.confidence DESC, p.term ASC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        if normalized_term_types:
            rows = [row for row in rows if self._platform_term_type_matches(str(row['term_type'] or ''), normalized_term_types)]
        resolved_offset = max(0, int(offset or 0))
        resolved_limit = max(1, int(limit or 100))
        return rows[resolved_offset : resolved_offset + resolved_limit]

    def update_platform_term(
        self,
        term_id: int,
        *,
        tag_name: str = '',
        term: str = '',
        term_type: str = '',
        source: str = '',
        confidence: float | None = None,
    ) -> tuple[bool, str]:
        resolved_term_id = int(term_id or 0)
        if resolved_term_id <= 0:
            return False, 'term_id 无效'
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT p.id, p.tag_id, p.platform, p.term, p.normalized_term, p.term_type, p.source, p.confidence,
                       t.name AS tag_name
                FROM platform_tag_terms p
                JOIN tags t ON t.id = p.tag_id
                WHERE p.id = ?
                LIMIT 1
                """,
                (resolved_term_id,),
            ).fetchone()
            if not row:
                return False, 'platform_term_not_found'

            if tag_name:
                target_row, _ = self._resolve_tag_exact_conn(conn, tag_name)
                if not target_row:
                    return False, f'tag 不存在：{tag_name}'
                if int(target_row['id']) != int(row['tag_id']):
                    return False, '暂不支持直接修改主 tag，请删除后重建'

            term_text = str(term or row['term'] or '').strip()
            normalized_term = normalize_tag_name(term_text)
            if not term_text or not normalized_term:
                return False, 'Pixiv 词不能为空'
            if normalized_term != str(row['normalized_term'] or ''):
                return False, '暂不支持直接修改词本身，请删除后重建'

            normalized_term_type = self._normalize_platform_term_type(term_type or str(row['term_type'] or 'both'))
            source_text = str(source or row['source'] or 'manual_review').strip() or 'manual_review'
            if confidence is None:
                confidence_value = float(row['confidence'] or 0.0)
            else:
                confidence_value = float(confidence or 0.0)
            current_time = utcnow_str()
            conn.execute(
                """
                UPDATE platform_tag_terms
                SET term = ?, term_type = ?, source = ?, confidence = ?, updated_at = ?
                WHERE id = ?
                """,
                (term_text, normalized_term_type, source_text, confidence_value, current_time, resolved_term_id),
            )
            return True, f'已更新 {str(row["platform"] or "")} term：{str(row["tag_name"])} -> {term_text}'

    def remove_platform_term(self, term_id: int) -> tuple[bool, str]:
        resolved_term_id = int(term_id or 0)
        if resolved_term_id <= 0:
            return False, 'term_id 无效'
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT p.id, p.platform, p.term, t.name AS tag_name
                FROM platform_tag_terms p
                JOIN tags t ON t.id = p.tag_id
                WHERE p.id = ?
                LIMIT 1
                """,
                (resolved_term_id,),
            ).fetchone()
            if not row:
                return False, 'platform_term_not_found'
            conn.execute('DELETE FROM platform_tag_terms WHERE id = ?', (resolved_term_id,))
            return True, f'已删除 {str(row["platform"] or "")} term：{str(row["tag_name"])} -> {str(row["term"] or "")}'

    def get_platform_terms_for_tag(
        self,
        *,
        tag_name: str,
        platform: str = 'pixiv',
        purpose: str = 'query',
        include_aliases: bool = True,
        include_primary: bool = True,
    ) -> list[str]:
        row = self.get_tag_row(tag_name)
        if not row:
            return []
        expected_types = {'query'} if str(purpose or 'query').strip().lower() == 'query' else {'match'}
        resolved: list[str] = []
        seen: set[str] = set()

        def append_term(value: str) -> None:
            text = str(value or '').strip()
            normalized = normalize_tag_name(text)
            if not text or not normalized or normalized in seen:
                return
            seen.add(normalized)
            resolved.append(text)

        for term_row in self.list_platform_terms(
            tag_id=int(row['id']),
            platform=platform,
            term_types=expected_types,
            limit=100,
        ):
            append_term(str(term_row['term']))
        if include_aliases:
            for alias in self.list_aliases(str(row['name'])):
                append_term(alias)
        if include_primary:
            append_term(str(row['name']))
        return resolved

    def suggest_platform_terms_for_tag(
        self,
        *,
        tag_name: str,
        platform: str = 'pixiv',
        limit: int = 15,
    ) -> list[dict[str, Any]]:
        tag_row = self.get_tag_row(tag_name)
        if not tag_row:
            return []
        existing_terms = {
            normalize_tag_name(str(tag_row['name'])),
            *(normalize_tag_name(alias) for alias in self.list_aliases(str(tag_row['name']))),
            *(normalize_tag_name(str(row['term'])) for row in self.list_platform_terms(tag_id=int(tag_row['id']), platform=platform, limit=200)),
        }
        counter: Counter[str] = Counter()
        display_terms: dict[str, str] = {}
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.raw_tags, s.extra_json
                FROM sources s
                JOIN image_tags it ON it.image_id = s.image_id
                JOIN images i ON i.id = s.image_id
                WHERE s.platform = ?
                  AND it.tag_id = ?
                  AND it.review_status IN ('approved', 'manual_approved')
                  AND i.is_active = 1
                """,
                (str(platform or '').strip().lower(), int(tag_row['id'])),
            ).fetchall()
        for row in rows:
            raw_terms = json.loads(row['raw_tags'] or '[]') if row['raw_tags'] else []
            extra = json.loads(row['extra_json'] or '{}') if row['extra_json'] else {}
            translated_terms = extra.get('translated_tags') if isinstance(extra, dict) else []
            if not isinstance(translated_terms, list):
                translated_terms = []
            for value in [*raw_terms, *translated_terms]:
                text = str(value or '').strip()
                normalized = normalize_tag_name(text)
                if (
                    not text
                    or not normalized
                    or normalized in existing_terms
                    or not self._looks_like_platform_term(text)
                ):
                    continue
                counter[normalized] += 1
                display_terms.setdefault(normalized, text)
        suggestions = sorted(counter.items(), key=lambda item: (-item[1], display_terms.get(item[0], item[0])))
        return [
            {
                'term': display_terms.get(normalized, normalized),
                'count': int(count),
                'normalized_term': normalized,
            }
            for normalized, count in suggestions[: max(1, int(limit or 1))]
        ]

    def list_unresolved_platform_terms(
        self,
        *,
        platform: str = 'pixiv',
        keyword: str = '',
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        platform_text = str(platform or 'pixiv').strip().lower() or 'pixiv'
        keyword_text = str(keyword or '').strip()
        normalized_keyword = normalize_tag_name(keyword_text)
        with self._lock, self._connect() as conn:
            existing_terms = {
                str(row['normalized_term'] or '')
                for row in conn.execute(
                    'SELECT normalized_term FROM platform_tag_terms WHERE platform = ?',
                    (platform_text,),
                ).fetchall()
                if str(row['normalized_term'] or '').strip()
            }
            existing_terms.update(
                str(row['normalized_name'] or '')
                for row in conn.execute('SELECT normalized_name FROM tags').fetchall()
                if str(row['normalized_name'] or '').strip()
            )
            existing_terms.update(
                str(row['normalized_alias'] or '')
                for row in conn.execute('SELECT normalized_alias FROM tag_aliases').fetchall()
                if str(row['normalized_alias'] or '').strip()
            )
            rows = conn.execute(
                """
                SELECT s.post_url, s.author, s.raw_tags, s.extra_json
                FROM sources s
                JOIN images i ON i.id = s.image_id
                WHERE s.platform = ?
                  AND i.is_active = 1
                ORDER BY s.id DESC
                """,
                (platform_text,),
            ).fetchall()

        counter: Counter[str] = Counter()
        display_terms: dict[str, str] = {}
        sample_post_urls: dict[str, list[str]] = {}
        sample_authors: dict[str, list[str]] = {}

        for row in rows:
            try:
                raw_terms = json.loads(row['raw_tags'] or '[]') if row['raw_tags'] else []
            except Exception:
                raw_terms = []
            try:
                extra = json.loads(row['extra_json'] or '{}') if row['extra_json'] else {}
            except Exception:
                extra = {}
            translated_terms = extra.get('translated_tags') if isinstance(extra, dict) else []
            if not isinstance(translated_terms, list):
                translated_terms = []

            row_seen: set[str] = set()
            for value in [*raw_terms, *translated_terms]:
                text = str(value or '').strip()
                normalized = normalize_tag_name(text)
                if (
                    not text
                    or not normalized
                    or normalized in row_seen
                    or normalized in existing_terms
                    or not self._looks_like_platform_term(text)
                ):
                    continue
                if keyword_text and keyword_text.lower() not in text.lower():
                    if not normalized_keyword or normalized_keyword not in normalized:
                        continue
                row_seen.add(normalized)
                counter[normalized] += 1
                display_terms.setdefault(normalized, text)

                post_url = str(row['post_url'] or '').strip()
                if post_url:
                    bucket = sample_post_urls.setdefault(normalized, [])
                    if post_url not in bucket and len(bucket) < 3:
                        bucket.append(post_url)
                author = str(row['author'] or '').strip()
                if author:
                    bucket = sample_authors.setdefault(normalized, [])
                    if author not in bucket and len(bucket) < 3:
                        bucket.append(author)

        suggestions = sorted(counter.items(), key=lambda item: (-item[1], display_terms.get(item[0], item[0])))
        return [
            {
                'term': display_terms.get(normalized, normalized),
                'normalized_term': normalized,
                'count': int(count),
                'sample_post_urls': sample_post_urls.get(normalized, []),
                'sample_authors': sample_authors.get(normalized, []),
            }
            for normalized, count in suggestions[: max(1, int(limit or 1))]
        ]

    @staticmethod
    def _dedupe_terms(values: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for raw in values:
            text = str(raw or '').strip()
            normalized = normalize_tag_name(text)
            if not text or not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(text)
        return result

    def _collect_platform_term_usage_conn(
        self,
        conn: sqlite3.Connection,
        *,
        platform: str,
        terms: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        normalized_terms = {
            normalize_tag_name(item)
            for item in terms
            if normalize_tag_name(item)
        }
        if not normalized_terms:
            return {}
        rows = conn.execute(
            """
            SELECT s.post_url, s.author, s.raw_tags, s.extra_json
            FROM sources s
            JOIN images i ON i.id = s.image_id
            WHERE s.platform = ?
              AND i.is_active = 1
            ORDER BY s.id DESC
            """,
            (str(platform or 'pixiv').strip().lower() or 'pixiv',),
        ).fetchall()
        usage: dict[str, dict[str, Any]] = {
            normalized: {
                'count': 0,
                'sample_post_urls': [],
                'sample_authors': [],
            }
            for normalized in normalized_terms
        }
        for row in rows:
            try:
                raw_terms = json.loads(row['raw_tags'] or '[]') if row['raw_tags'] else []
            except Exception:
                raw_terms = []
            try:
                extra = json.loads(row['extra_json'] or '{}') if row['extra_json'] else {}
            except Exception:
                extra = {}
            translated_terms = extra.get('translated_tags') if isinstance(extra, dict) else []
            if not isinstance(translated_terms, list):
                translated_terms = []
            row_seen: set[str] = set()
            for value in [*raw_terms, *translated_terms]:
                normalized = normalize_tag_name(str(value or '').strip())
                if not normalized or normalized in row_seen or normalized not in normalized_terms:
                    continue
                row_seen.add(normalized)
                payload = usage[normalized]
                payload['count'] = int(payload.get('count', 0) or 0) + 1
                post_url = str(row['post_url'] or '').strip()
                if post_url and post_url not in payload['sample_post_urls'] and len(payload['sample_post_urls']) < 3:
                    payload['sample_post_urls'].append(post_url)
                author = str(row['author'] or '').strip()
                if author and author not in payload['sample_authors'] and len(payload['sample_authors']) < 3:
                    payload['sample_authors'].append(author)
        return usage

    def _build_review_alias_term_plan_conn(
        self,
        conn: sqlite3.Connection,
        *,
        platform: str,
        target_row: sqlite3.Row,
        term: str,
    ) -> dict[str, Any]:
        platform_text = str(platform or 'pixiv').strip().lower() or 'pixiv'
        term_text = str(term or '').strip()
        target_id = int(target_row['id'])
        target_name = str(target_row['name'])
        target_normalized = str(target_row['normalized_name'] or normalize_tag_name(target_name))
        normalized_term = normalize_tag_name(term_text)
        if not term_text or not normalized_term:
            return {'status': 'skipped', 'term': term_text, 'message': '空词'}
        if not self._looks_like_platform_term(term_text):
            return {'status': 'skipped', 'term': term_text, 'message': '看起来不是适合沉淀的平台角色词'}
        if normalized_term == target_normalized:
            return {'status': 'mapped', 'term': term_text, 'tag_name': target_name, 'action': 'already'}

        source_row, source_match_type = self._resolve_tag_exact_conn(conn, term_text)
        if source_row and int(source_row['id']) != target_id and source_match_type == 'exact_alias':
            return {
                'status': 'skipped',
                'term': term_text,
                'message': f'alias 已被 {str(source_row["name"])} 使用',
            }

        platform_match = self._resolve_platform_term_exact_conn(conn, platform_text, term_text)
        if platform_match and int(platform_match['tag_id']) != target_id:
            conflict = conn.execute('SELECT name FROM tags WHERE id = ? LIMIT 1', (int(platform_match['tag_id']),)).fetchone()
            conflict_name = str(conflict['name']) if conflict else str(platform_match['tag_id'])
            return {
                'status': 'skipped',
                'term': term_text,
                'message': f'{platform_text} term 已映射到其他 tag：{conflict_name}',
            }

        source_tag_id = int(source_row['id']) if source_row else 0
        action = 'add'
        if source_row and source_tag_id == target_id:
            action = 'already'
        elif source_row and source_match_type == 'exact_tag':
            action = 'merge_tag'
        elif platform_match:
            action = 'already'
        return {
            'status': 'mapped',
            'term': term_text,
            'tag_name': target_name,
            'action': action,
            'source_tag_id': source_tag_id,
            'source_match_type': source_match_type,
            'platform_already': bool(platform_match),
        }

    def _apply_review_alias_term_conn(
        self,
        conn: sqlite3.Connection,
        *,
        platform: str,
        target_row: sqlite3.Row,
        term: str,
        now: str,
    ) -> dict[str, Any]:
        plan = self._build_review_alias_term_plan_conn(conn, platform=platform, target_row=target_row, term=term)
        if plan.get('status') != 'mapped':
            return plan

        platform_text = str(platform or 'pixiv').strip().lower() or 'pixiv'
        target_id = int(target_row['id'])
        target_name = str(target_row['name'])
        action = str(plan.get('action') or '')

        if action == 'merge_tag':
            if not bool(plan.get('platform_already')):
                ok, message = self._upsert_platform_term_conn(
                    conn,
                    tag_id=target_id,
                    platform=platform_text,
                    term=str(plan['term']),
                    term_type='both',
                    source='manual_review',
                    confidence=1.0,
                    now=now,
                )
                if not ok and '已被 alias 覆盖' not in message and '与主 tag 相同' not in message:
                    return {'status': 'skipped', 'term': str(plan['term']), 'message': message}
            merge_result = self._merge_tag_into_conn(
                conn,
                target_id=target_id,
                target_name=target_name,
                source_id=int(plan.get('source_tag_id') or 0),
                now=now,
            )
            plan['action'] = 'merge_tag'
            plan['merged_tag'] = str(merge_result.get('source_name') or '')
            plan['merge_result'] = merge_result
            return plan

        if action == 'add':
            if not bool(plan.get('platform_already')):
                ok, message = self._upsert_platform_term_conn(
                    conn,
                    tag_id=target_id,
                    platform=platform_text,
                    term=str(plan['term']),
                    term_type='both',
                    source='manual_review',
                    confidence=1.0,
                    now=now,
                )
                if ok:
                    plan['platform_term_added'] = True
                elif '已被 alias 覆盖' not in message and '与主 tag 相同' not in message:
                    return {'status': 'skipped', 'term': str(plan['term']), 'message': message}
            ok, message = self._insert_alias_conn(conn, tag_id=target_id, alias=str(plan['term']), now=now)
            if ok:
                plan['action'] = 'alias_added'
                plan['alias_added'] = True
            elif '已存在' in message or '不能与主 tag 相同' in message:
                plan['action'] = 'already'
            else:
                return {'status': 'skipped', 'term': str(plan['term']), 'message': message}
        return plan

    def _preview_review_term_mappings_conn(
        self,
        conn: sqlite3.Connection,
        *,
        platform: str,
        requested_terms: Iterable[str],
        target_row: sqlite3.Row,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        mapped_terms: list[dict[str, Any]] = []
        skipped_terms: list[str] = []
        for term in requested_terms:
            plan = self._build_review_alias_term_plan_conn(conn, platform=platform, target_row=target_row, term=term)
            if plan.get('status') != 'mapped':
                skipped_terms.append(f'{term}（{str(plan.get("message") or "无法沉淀")}）')
                continue
            mapped_terms.append(
                {
                    'term': str(plan['term']),
                    'tag_name': str(plan['tag_name']),
                    'action': str(plan.get('action') or 'add'),
                    'source_tag_id': int(plan.get('source_tag_id') or 0),
                }
            )
        return mapped_terms, skipped_terms

    def preview_batch_image_review(
        self,
        items: Iterable[dict[str, Any]],
        *,
        platform: str = 'pixiv',
        reject_unselected: bool = True,
    ) -> tuple[bool, dict[str, Any]]:
        platform_text = str(platform or 'pixiv').strip().lower() or 'pixiv'
        normalized_items: list[dict[str, Any]] = []
        seen_image_ids: set[int] = set()
        for raw in items:
            if not isinstance(raw, dict):
                continue
            image_id = int(raw.get('image_id', 0) or 0)
            if image_id <= 0 or image_id in seen_image_ids:
                continue
            seen_image_ids.add(image_id)
            normalized_items.append(
                {
                    'image_id': image_id,
                    'selected_tag_names': self._dedupe_terms(raw.get('selected_tag_names', []) or []),
                    'source_terms': self._dedupe_terms(raw.get('source_terms', []) or []),
                }
            )
        if not normalized_items:
            return False, {'message': '请至少选择一张图片。'}

        preview_items: list[dict[str, Any]] = []
        totals = {
            'images': 0,
            'approved_tag_links': 0,
            'rejected_tag_links': 0,
            'mapped_terms': 0,
            'skipped_terms': 0,
            'errors': 0,
        }
        with self._lock, self._connect() as conn:
            for item in normalized_items:
                image_id = int(item['image_id'])
                requested_tags = list(item['selected_tag_names'])
                requested_terms = list(item['source_terms'])
                if not requested_tags:
                    preview_items.append({'image_id': image_id, 'status': 'error', 'message': '请至少选择一个归入主 tag。'})
                    totals['errors'] += 1
                    continue
                image = conn.execute('SELECT id FROM images WHERE id = ? AND is_active = 1 LIMIT 1', (image_id,)).fetchone()
                if not image:
                    preview_items.append({'image_id': image_id, 'status': 'error', 'message': f'图片不存在：{image_id}'})
                    totals['errors'] += 1
                    continue
                source_exists = conn.execute(
                    'SELECT 1 FROM sources WHERE image_id = ? AND platform = ? LIMIT 1',
                    (image_id, platform_text),
                ).fetchone()
                if not source_exists:
                    preview_items.append({'image_id': image_id, 'status': 'error', 'message': f'图片 #{image_id} 没有 {platform_text} 来源记录。'})
                    totals['errors'] += 1
                    continue
                canonical_tag_name = requested_tags[0]
                alias_terms = self._dedupe_terms([*requested_tags[1:], *requested_terms])
                target_row, _ = self._resolve_tag_exact_conn(conn, canonical_tag_name)
                if not target_row:
                    preview_items.append({'image_id': image_id, 'status': 'error', 'message': f'归入主 tag 不存在：{canonical_tag_name}'})
                    totals['errors'] += 1
                    continue
                selected_ids = {int(target_row['id'])}
                selected_names = [str(target_row['name'])]
                mapped_terms, skipped_terms = self._preview_review_term_mappings_conn(
                    conn,
                    platform=platform_text,
                    requested_terms=alias_terms,
                    target_row=target_row,
                )
                merged_source_ids = {
                    int(term.get('source_tag_id') or 0)
                    for term in mapped_terms
                    if str(term.get('action') or '') == 'merge_tag' and int(term.get('source_tag_id') or 0) > 0
                }
                tasks = conn.execute(
                    """
                    SELECT rt.tag_id, t.name AS tag_name
                    FROM review_tasks rt
                    JOIN tags t ON t.id = rt.tag_id
                    WHERE rt.image_id = ?
                    ORDER BY rt.id DESC
                    """,
                    (image_id,),
                ).fetchall()
                rejected_names = [
                    str(row['tag_name'])
                    for row in tasks
                    if reject_unselected and int(row['tag_id']) not in selected_ids and int(row['tag_id']) not in merged_source_ids
                ]
                preview_items.append(
                    {
                        'image_id': image_id,
                        'status': 'ok',
                        'approved_tags': selected_names,
                        'rejected_tags': rejected_names,
                        'mapped_terms': mapped_terms,
                        'skipped_terms': skipped_terms,
                        'selected_source_terms': alias_terms,
                    }
                )
                totals['images'] += 1
                totals['approved_tag_links'] += len(selected_names)
                totals['rejected_tag_links'] += len(rejected_names)
                totals['mapped_terms'] += len(mapped_terms)
                totals['skipped_terms'] += len(skipped_terms)
        return True, {
            'message': f'已生成 {totals["images"]} 张图片的批量审核预览',
            'items': preview_items,
            'totals': totals,
        }

    def apply_batch_image_review(
        self,
        items: Iterable[dict[str, Any]],
        *,
        platform: str = 'pixiv',
        reject_unselected: bool = True,
    ) -> tuple[bool, dict[str, Any]]:
        normalized_items: list[dict[str, Any]] = []
        seen_image_ids: set[int] = set()
        for raw in items:
            if not isinstance(raw, dict):
                continue
            image_id = int(raw.get('image_id', 0) or 0)
            if image_id <= 0 or image_id in seen_image_ids:
                continue
            seen_image_ids.add(image_id)
            normalized_items.append(
                {
                    'image_id': image_id,
                    'selected_tag_names': self._dedupe_terms(raw.get('selected_tag_names', []) or []),
                    'source_terms': self._dedupe_terms(raw.get('source_terms', []) or []),
                    'reason': str(raw.get('reason', '') or '').strip(),
                }
            )
        if not normalized_items:
            return False, {'message': '请至少选择一张图片。'}

        results: list[dict[str, Any]] = []
        success_count = 0
        failure_count = 0
        mapped_terms_count = 0
        for item in normalized_items:
            ok, result = self.apply_image_review(
                int(item['image_id']),
                selected_tag_names=item['selected_tag_names'],
                source_terms=item['source_terms'],
                platform=platform,
                reason=item['reason'],
                reject_unselected=reject_unselected,
            )
            payload = {'ok': ok, 'image_id': int(item['image_id'])}
            if isinstance(result, dict):
                payload.update(result)
            else:
                payload['message'] = str(result)
            results.append(payload)
            if ok:
                success_count += 1
                mapped_terms_count += len(payload.get('mapped_terms', []) or [])
            else:
                failure_count += 1
        return success_count > 0, {
            'message': f'批量审核完成：成功 {success_count} 张，失败 {failure_count} 张',
            'items': results,
            'success_count': success_count,
            'failure_count': failure_count,
            'mapped_terms_count': mapped_terms_count,
        }

    def preview_batch_platform_terms(
        self,
        *,
        tag_name: str,
        terms: Iterable[str],
        platform: str = 'pixiv',
        term_type: str = 'both',
    ) -> tuple[bool, dict[str, Any]]:
        requested_terms = self._dedupe_terms(terms)
        if not requested_terms:
            return False, {'message': '请至少提供一个 Pixiv 词。'}
        platform_text = str(platform or 'pixiv').strip().lower() or 'pixiv'
        normalized_term_type = self._normalize_platform_term_type(term_type)
        target_row = self.get_tag_row(tag_name)
        if not target_row:
            return False, {'message': f'tag 不存在：{tag_name}'}

        with self._lock, self._connect() as conn:
            usage = self._collect_platform_term_usage_conn(conn, platform=platform_text, terms=requested_terms)
            items: list[dict[str, Any]] = []
            summary = {'add': 0, 'update': 0, 'already': 0, 'conflict': 0, 'invalid': 0}
            for term in requested_terms:
                normalized = normalize_tag_name(term)
                payload = usage.get(normalized, {'count': 0, 'sample_post_urls': [], 'sample_authors': []})
                item = {
                    'term': term,
                    'count': int(payload.get('count', 0) or 0),
                    'sample_post_urls': list(payload.get('sample_post_urls', []) or []),
                    'sample_authors': list(payload.get('sample_authors', []) or []),
                    'target_tag': str(target_row['name']),
                    'status': '',
                    'message': '',
                }
                if not self._looks_like_platform_term(term):
                    item['status'] = 'invalid'
                    item['message'] = '看起来不是适合沉淀的平台角色词'
                    summary['invalid'] += 1
                    items.append(item)
                    continue
                existing = conn.execute(
                    """
                    SELECT p.id, p.tag_id, p.term_type, p.source, p.confidence, t.name AS tag_name
                    FROM platform_tag_terms p
                    JOIN tags t ON t.id = p.tag_id
                    WHERE p.platform = ? AND p.normalized_term = ?
                    LIMIT 1
                    """,
                    (platform_text, normalized),
                ).fetchone()
                if existing and int(existing['tag_id']) != int(target_row['id']):
                    item['status'] = 'conflict'
                    item['message'] = f'已映射到其他 tag：{str(existing["tag_name"])}'
                    summary['conflict'] += 1
                elif existing:
                    merged_type = self._platform_term_type_union(str(existing['term_type'] or ''), normalized_term_type)
                    if merged_type != str(existing['term_type'] or 'both'):
                        item['status'] = 'update'
                        item['message'] = f'将扩展 term_type：{str(existing["term_type"])} -> {merged_type}'
                        summary['update'] += 1
                    else:
                        item['status'] = 'already'
                        item['message'] = '当前目标 tag 已存在该平台词'
                        summary['already'] += 1
                else:
                    item['status'] = 'add'
                    item['message'] = '将新增到目标 tag'
                    summary['add'] += 1
                items.append(item)

        return True, {
            'message': f'已生成 {len(items)} 个 Pixiv 词的批量映射预览',
            'target_tag': str(target_row['name']),
            'items': items,
            'summary': summary,
        }

    def apply_batch_platform_terms(
        self,
        *,
        tag_name: str,
        terms: Iterable[str],
        platform: str = 'pixiv',
        term_type: str = 'both',
        source: str = 'manual_review',
        confidence: float = 1.0,
    ) -> tuple[bool, dict[str, Any]]:
        requested_terms = self._dedupe_terms(terms)
        if not requested_terms:
            return False, {'message': '请至少提供一个 Pixiv 词。'}
        target_row = self.get_tag_row(tag_name)
        if not target_row:
            return False, {'message': f'tag 不存在：{tag_name}'}
        normalized_term_type = self._normalize_platform_term_type(term_type)
        results: list[dict[str, Any]] = []
        summary = {'added': 0, 'updated': 0, 'failed': 0}
        with self._lock, self._connect() as conn:
            for term in requested_terms:
                if not self._looks_like_platform_term(term):
                    results.append({'term': term, 'ok': False, 'message': '看起来不是适合沉淀的平台角色词'})
                    summary['failed'] += 1
                    continue
                existing = conn.execute(
                    """
                    SELECT id, tag_id, term_type
                    FROM platform_tag_terms
                    WHERE platform = ? AND normalized_term = ?
                    LIMIT 1
                    """,
                    (str(platform or 'pixiv').strip().lower() or 'pixiv', normalize_tag_name(term)),
                ).fetchone()
                ok, message = self._upsert_platform_term_conn(
                    conn,
                    tag_id=int(target_row['id']),
                    platform=platform,
                    term=term,
                    term_type=normalized_term_type,
                    source=source,
                    confidence=confidence,
                    now=utcnow_str(),
                )
                results.append({'term': term, 'ok': ok, 'message': message})
                if ok:
                    if existing and int(existing['tag_id']) == int(target_row['id']):
                        summary['updated'] += 1
                    else:
                        summary['added'] += 1
                else:
                    summary['failed'] += 1
        return summary['added'] + summary['updated'] > 0, {
            'message': f'批量平台词提交完成：新增 {summary["added"]}，更新 {summary["updated"]}，失败 {summary["failed"]}',
            'target_tag': str(target_row['name']),
            'items': results,
            'summary': summary,
        }

    def list_tag_merge_candidates(
        self,
        *,
        keyword: str = '',
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        keyword_text = str(keyword or '').strip()
        with self._lock, self._connect() as conn:
            tag_rows = conn.execute(
                """
                SELECT t.id, t.name, t.normalized_name, t.is_character,
                       COUNT(DISTINCT CASE WHEN it.review_status IN ('approved', 'manual_approved') AND i.is_active = 1 THEN i.id END) AS image_count
                FROM tags t
                LEFT JOIN image_tags it ON it.tag_id = t.id
                LEFT JOIN images i ON i.id = it.image_id
                WHERE t.is_character = 1
                GROUP BY t.id, t.name, t.normalized_name, t.is_character
                ORDER BY image_count DESC, t.name ASC
                LIMIT 240
                """
            ).fetchall()
            candidates: dict[tuple[int, int], dict[str, Any]] = {}
            for target_row in tag_rows:
                target_id = int(target_row['id'])
                target_name = str(target_row['name'])
                terms: list[tuple[str, str, int]] = []
                for platform_row in self.list_platform_terms(tag_id=target_id, platform='pixiv', limit=80):
                    terms.append((str(platform_row['term'] or ''), 'platform_term', 8))
                for suggestion in self.suggest_platform_terms_for_tag(tag_name=target_name, platform='pixiv', limit=15):
                    terms.append((str(suggestion.get('term', '') or ''), 'history_term', min(7, int(suggestion.get('count', 0) or 0))))
                seen_term_keys: set[str] = set()
                for term, reason, weight in terms:
                    normalized_term = normalize_tag_name(term)
                    if not normalized_term or normalized_term in seen_term_keys:
                        continue
                    seen_term_keys.add(normalized_term)
                    matched_row, match_type = self._resolve_tag_exact_conn(conn, term)
                    if not matched_row or int(matched_row['id']) == target_id:
                        continue
                    source_id = int(matched_row['id'])
                    source_name = str(matched_row['name'])
                    key = (source_id, target_id)
                    entry = candidates.setdefault(
                        key,
                        {
                            'source_tag': source_name,
                            'target_tag': target_name,
                            'source_tag_id': source_id,
                            'target_tag_id': target_id,
                            'source_is_character': bool(matched_row['is_character']),
                            'target_is_character': bool(target_row['is_character']),
                            'source_image_count': int(conn.execute(
                                """
                                SELECT COUNT(DISTINCT i.id) AS c
                                FROM image_tags it
                                JOIN images i ON i.id = it.image_id
                                WHERE it.tag_id = ? AND i.is_active = 1 AND it.review_status IN ('approved', 'manual_approved')
                                """,
                                (source_id,),
                            ).fetchone()['c']),
                            'target_image_count': int(target_row['image_count'] or 0),
                            'score': 0,
                            'reasons': [],
                            'example_terms': [],
                        },
                    )
                    entry['score'] += int(weight)
                    reason_text = f'{reason}:{match_type or "exact"}'
                    if reason_text not in entry['reasons']:
                        entry['reasons'].append(reason_text)
                    if term not in entry['example_terms']:
                        entry['example_terms'].append(term)
            items = list(candidates.values())
        if keyword_text:
            keyword_normalized = normalize_tag_name(keyword_text)
            items = [
                item
                for item in items
                if keyword_text in str(item['source_tag'])
                or keyword_text in str(item['target_tag'])
                or any(keyword_text in str(term) for term in item.get('example_terms', []))
                or keyword_normalized in normalize_tag_name(str(item['source_tag']))
                or keyword_normalized in normalize_tag_name(str(item['target_tag']))
            ]
        items.sort(
            key=lambda item: (
                -int(item.get('score', 0) or 0),
                -int(item.get('target_is_character', False)),
                -int(item.get('target_image_count', 0) or 0),
                int(item.get('source_image_count', 0) or 0),
                str(item.get('source_tag', '')),
            )
        )
        return items[: max(1, int(limit or 1))]

    def list_tag_identity_scan_inputs(
        self,
        *,
        platform: str = 'pixiv',
        limit: int = 260,
    ) -> list[dict[str, Any]]:
        platform_text = str(platform or 'pixiv').strip().lower() or 'pixiv'
        resolved_limit = max(1, int(limit or 1))
        with self._lock, self._connect() as conn:
            tag_rows = conn.execute(
                """
                SELECT t.id, t.name, t.normalized_name, t.is_character,
                       COUNT(DISTINCT CASE WHEN it.review_status IN ('approved', 'manual_approved') AND i.is_active = 1 THEN i.id END) AS image_count
                FROM tags t
                LEFT JOIN image_tags it ON it.tag_id = t.id
                LEFT JOIN images i ON i.id = it.image_id
                WHERE t.is_character = 1
                GROUP BY t.id, t.name, t.normalized_name, t.is_character
                ORDER BY image_count DESC, t.name ASC
                LIMIT ?
                """,
                (resolved_limit,),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for tag_row in tag_rows:
                tag_id = int(tag_row['id'])
                alias_rows = conn.execute(
                    'SELECT alias, normalized_alias FROM tag_aliases WHERE tag_id = ? ORDER BY alias ASC',
                    (tag_id,),
                ).fetchall()
                platform_rows = conn.execute(
                    """
                    SELECT term, normalized_term, term_type, source, confidence
                    FROM platform_tag_terms
                    WHERE tag_id = ? AND platform = ?
                    ORDER BY confidence DESC, term ASC
                    LIMIT 120
                    """,
                    (tag_id, platform_text),
                ).fetchall()
                source_rows = conn.execute(
                    """
                    SELECT s.raw_tags, s.extra_json
                    FROM sources s
                    JOIN image_tags it ON it.image_id = s.image_id
                    JOIN images i ON i.id = s.image_id
                    WHERE s.platform = ?
                      AND it.tag_id = ?
                      AND i.is_active = 1
                      AND it.review_status IN ('approved', 'manual_approved')
                    ORDER BY s.id DESC
                    LIMIT 260
                    """,
                    (platform_text, tag_id),
                ).fetchall()
                history_counter: Counter[str] = Counter()
                history_display: dict[str, str] = {}
                for source_row in source_rows:
                    try:
                        raw_terms = json.loads(source_row['raw_tags'] or '[]') if source_row['raw_tags'] else []
                    except Exception:
                        raw_terms = []
                    try:
                        extra = json.loads(source_row['extra_json'] or '{}') if source_row['extra_json'] else {}
                    except Exception:
                        extra = {}
                    translated_terms = extra.get('translated_tags') if isinstance(extra, dict) else []
                    if not isinstance(translated_terms, list):
                        translated_terms = []
                    for value in [*raw_terms, *translated_terms]:
                        text = str(value or '').strip()
                        normalized = normalize_tag_name(text)
                        if not text or not normalized or not self._looks_like_platform_term(text):
                            continue
                        history_counter[normalized] += 1
                        history_display.setdefault(normalized, text)
                history_terms = [
                    {
                        'term': history_display.get(normalized, normalized),
                        'normalized_term': normalized,
                        'count': int(count),
                    }
                    for normalized, count in sorted(
                        history_counter.items(),
                        key=lambda item: (-item[1], history_display.get(item[0], item[0])),
                    )[:40]
                ]
                result.append(
                    {
                        'id': tag_id,
                        'name': str(tag_row['name']),
                        'normalized_name': str(tag_row['normalized_name'] or ''),
                        'is_character': bool(tag_row['is_character']),
                        'image_count': int(tag_row['image_count'] or 0),
                        'aliases': [str(row['alias']) for row in alias_rows],
                        'platform_terms': [
                            {
                                'term': str(row['term'] or ''),
                                'normalized_term': str(row['normalized_term'] or ''),
                                'term_type': str(row['term_type'] or 'both'),
                                'source': str(row['source'] or ''),
                                'confidence': float(row['confidence'] or 0.0),
                            }
                            for row in platform_rows
                        ],
                        'history_terms': history_terms,
                    }
                )
        return result

    @staticmethod
    def _parse_candidate_json(value: str, fallback: Any) -> Any:
        try:
            parsed = json.loads(value or '')
        except Exception:
            return fallback
        return parsed if parsed is not None else fallback

    def upsert_tag_identity_candidate(
        self,
        *,
        source_tag_id: int,
        target_tag_id: int,
        score: float,
        reasons: list[str],
        evidence: dict[str, Any],
        llm_result: dict[str, Any] | None = None,
        status: str = 'pending',
    ) -> dict[str, Any]:
        source_id = int(source_tag_id or 0)
        target_id = int(target_tag_id or 0)
        if source_id <= 0 or target_id <= 0 or source_id == target_id:
            raise ValueError('source_tag_id / target_tag_id 无效')
        normalized_status = str(status or 'pending').strip().lower() or 'pending'
        if normalized_status not in {'pending', 'ignored'}:
            normalized_status = 'pending'
        now = utcnow_str()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tag_merge_identity_candidates(
                    source_tag_id, target_tag_id, status, score, reasons_json,
                    evidence_json, llm_result_json, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_tag_id, target_tag_id) DO UPDATE SET
                    status = CASE
                        WHEN tag_merge_identity_candidates.status = 'ignored' AND excluded.status = 'pending'
                        THEN tag_merge_identity_candidates.status
                        ELSE excluded.status
                    END,
                    score = excluded.score,
                    reasons_json = excluded.reasons_json,
                    evidence_json = excluded.evidence_json,
                    llm_result_json = excluded.llm_result_json,
                    updated_at = excluded.updated_at
                """,
                (
                    source_id,
                    target_id,
                    normalized_status,
                    float(score or 0),
                    json.dumps(reasons or [], ensure_ascii=False),
                    json.dumps(evidence or {}, ensure_ascii=False),
                    json.dumps(llm_result or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT c.*, s.name AS source_tag, t.name AS target_tag
                FROM tag_merge_identity_candidates c
                JOIN tags s ON s.id = c.source_tag_id
                JOIN tags t ON t.id = c.target_tag_id
                WHERE c.source_tag_id = ? AND c.target_tag_id = ?
                LIMIT 1
                """,
                (source_id, target_id),
            ).fetchone()
        return self._build_tag_identity_candidate_payload(row) if row else {}

    def list_tag_identity_candidates(
        self,
        *,
        status: str = 'pending',
        keyword: str = '',
        limit: int = 80,
    ) -> list[dict[str, Any]]:
        status_text = str(status or 'pending').strip().lower()
        keyword_text = str(keyword or '').strip()
        normalized_keyword = normalize_tag_name(keyword_text)
        sql = """
            SELECT c.*, s.name AS source_tag, t.name AS target_tag,
                   s.is_character AS source_is_character, t.is_character AS target_is_character
            FROM tag_merge_identity_candidates c
            JOIN tags s ON s.id = c.source_tag_id
            JOIN tags t ON t.id = c.target_tag_id
        """
        clauses: list[str] = []
        params: list[Any] = []
        if status_text:
            clauses.append('c.status = ?')
            params.append(status_text)
        if keyword_text:
            like_text = f'%{keyword_text}%'
            like_normalized = f'%{normalized_keyword}%'
            clauses.append(
                """
                (
                    s.name LIKE ?
                    OR t.name LIKE ?
                    OR s.normalized_name LIKE ?
                    OR t.normalized_name LIKE ?
                    OR c.reasons_json LIKE ?
                    OR c.evidence_json LIKE ?
                )
                """
            )
            params.extend([like_text, like_text, like_normalized, like_normalized, like_text, like_text])
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY c.score DESC, c.updated_at DESC, c.id DESC LIMIT ?'
        params.append(max(1, int(limit or 1)))
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._build_tag_identity_candidate_payload(row) for row in rows]

    def ignore_tag_identity_candidate(self, candidate_id: int) -> tuple[bool, str]:
        resolved_id = int(candidate_id or 0)
        if resolved_id <= 0:
            return False, 'candidate_id 无效'
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT id FROM tag_merge_identity_candidates WHERE id = ? LIMIT 1',
                (resolved_id,),
            ).fetchone()
            if not row:
                return False, '候选不存在'
            conn.execute(
                "UPDATE tag_merge_identity_candidates SET status = 'ignored', updated_at = ? WHERE id = ?",
                (utcnow_str(), resolved_id),
            )
        return True, '已忽略该归并候选'

    def mark_stale_tag_identity_candidates(self, active_pairs: set[tuple[int, int]]) -> int:
        normalized_pairs = {(int(source), int(target)) for source, target in active_pairs if int(source) > 0 and int(target) > 0}
        now = utcnow_str()
        changed = 0
        with self._lock, self._connect() as conn:
            pending_rows = conn.execute(
                """
                SELECT id, source_tag_id, target_tag_id
                FROM tag_merge_identity_candidates
                WHERE status = 'pending'
                """
            ).fetchall()
            for row in pending_rows:
                pair = (int(row['source_tag_id']), int(row['target_tag_id']))
                if pair in normalized_pairs:
                    continue
                conn.execute(
                    "UPDATE tag_merge_identity_candidates SET status = 'stale', updated_at = ? WHERE id = ?",
                    (now, int(row['id'])),
                )
                changed += 1
        return changed

    def get_pixiv_query_terms_for_tag(self, tag_name: str) -> list[str]:
        row = self.get_tag_row(tag_name)
        if not row:
            return []
        tag_id = int(row['id'])
        terms: list[str] = []
        seen: set[str] = set()

        def append_term(value: str) -> None:
            text = str(value or '').strip()
            normalized = normalize_tag_name(text)
            if not text or not normalized or normalized in seen:
                return
            seen.add(normalized)
            terms.append(text)

        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT term, term_type, confidence
                FROM platform_tag_terms
                WHERE tag_id = ? AND platform = ?
                ORDER BY
                    CASE term_type WHEN 'query' THEN 0 WHEN 'both' THEN 1 ELSE 2 END,
                    confidence DESC,
                    term ASC
                LIMIT 80
                """,
                (tag_id, 'pixiv'),
            ).fetchall()
        for term_row in rows:
            term_type = self._normalize_platform_term_type(str(term_row['term_type'] or 'both'))
            if self._platform_term_type_matches(term_type, {'query'}):
                append_term(str(term_row['term'] or ''))
        if not terms:
            primary_cjk = set(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", str(row['name'] or '')))
            for suggestion in self.suggest_platform_terms_for_tag(
                tag_name=str(row['name']),
                platform='pixiv',
                limit=20,
            ):
                term = str(suggestion.get('term', '') or '').strip()
                term_cjk = set(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", term))
                if len(primary_cjk & term_cjk) >= 2:
                    append_term(term)
                if len(terms) >= 5:
                    break
        if not terms:
            append_term(str(row['name']))
        return terms

    def _build_tag_identity_candidate_payload(self, row: Any) -> dict[str, Any]:
        if not row:
            return {}
        reasons = self._parse_candidate_json(str(row['reasons_json'] or '[]'), [])
        evidence = self._parse_candidate_json(str(row['evidence_json'] or '{}'), {})
        llm_result = self._parse_candidate_json(str(row['llm_result_json'] or '{}'), {})
        return {
            'id': int(row['id']),
            'source_tag_id': int(row['source_tag_id']),
            'target_tag_id': int(row['target_tag_id']),
            'source_tag': str(row['source_tag'] or ''),
            'target_tag': str(row['target_tag'] or ''),
            'source_is_character': bool(row['source_is_character']) if 'source_is_character' in row.keys() else True,
            'target_is_character': bool(row['target_is_character']) if 'target_is_character' in row.keys() else True,
            'status': str(row['status'] or 'pending'),
            'score': float(row['score'] or 0.0),
            'reasons': reasons if isinstance(reasons, list) else [],
            'evidence': evidence if isinstance(evidence, dict) else {},
            'llm_result': llm_result if isinstance(llm_result, dict) else {},
            'created_at': str(row['created_at'] or ''),
            'updated_at': str(row['updated_at'] or ''),
        }

    def preview_merge_tags(
        self,
        *,
        target_tag_name: str,
        source_tag_names: Iterable[str],
    ) -> tuple[bool, dict[str, Any]]:
        requested_sources = self._dedupe_terms(source_tag_names)
        if not requested_sources:
            return False, {'message': '请至少提供一个来源 tag。'}
        with self._lock, self._connect() as conn:
            target_row, target_match_type = self._resolve_tag_exact_conn(conn, target_tag_name)
            if not target_row:
                return False, {'message': f'目标 tag 不存在：{target_tag_name}'}
            target_id = int(target_row['id'])
            target_name = str(target_row['name'])
            target_aliases = [str(row['alias']) for row in conn.execute('SELECT alias FROM tag_aliases WHERE tag_id = ? ORDER BY alias ASC', (target_id,)).fetchall()]
            items: list[dict[str, Any]] = []
            totals = {
                'image_links': 0,
                'image_link_collisions': 0,
                'review_tasks': 0,
                'review_task_collisions': 0,
                'aliases': 0,
                'platform_terms': 0,
                'subscriptions': 0,
            }
            for source_text in requested_sources:
                source_row, source_match_type = self._resolve_tag_exact_conn(conn, source_text)
                if not source_row:
                    items.append({'source_tag': source_text, 'status': 'error', 'message': f'来源 tag 不存在：{source_text}'})
                    continue
                source_id = int(source_row['id'])
                source_name = str(source_row['name'])
                if source_id == target_id:
                    items.append({'source_tag': source_name, 'status': 'skip', 'message': '来源 tag 与目标 tag 相同'})
                    continue
                image_links = int(conn.execute('SELECT COUNT(*) AS c FROM image_tags WHERE tag_id = ?', (source_id,)).fetchone()['c'])
                image_link_collisions = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) AS c
                        FROM image_tags s
                        JOIN image_tags t
                          ON t.image_id = s.image_id
                         AND t.tag_id = ?
                         AND t.source_type = s.source_type
                        WHERE s.tag_id = ?
                        """,
                        (target_id, source_id),
                    ).fetchone()['c']
                )
                review_tasks = int(conn.execute('SELECT COUNT(*) AS c FROM review_tasks WHERE tag_id = ?', (source_id,)).fetchone()['c'])
                review_task_collisions = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) AS c
                        FROM review_tasks s
                        JOIN review_tasks t
                          ON t.image_id = s.image_id
                         AND t.tag_id = ?
                        WHERE s.tag_id = ?
                        """,
                        (target_id, source_id),
                    ).fetchone()['c']
                )
                alias_rows = conn.execute('SELECT alias FROM tag_aliases WHERE tag_id = ? ORDER BY alias ASC', (source_id,)).fetchall()
                platform_rows = conn.execute('SELECT term FROM platform_tag_terms WHERE tag_id = ? ORDER BY id ASC', (source_id,)).fetchall()
                subscription_rows = conn.execute(
                    'SELECT platform, query_text, enabled FROM crawl_subscriptions WHERE tag_id = ? ORDER BY platform ASC',
                    (source_id,),
                ).fetchall()
                item = {
                    'source_tag': source_name,
                    'source_match_type': source_match_type,
                    'target_tag': target_name,
                    'target_match_type': target_match_type,
                    'source_is_character': bool(source_row['is_character']),
                    'target_is_character': bool(target_row['is_character']),
                    'image_links': image_links,
                    'image_link_collisions': image_link_collisions,
                    'review_tasks': review_tasks,
                    'review_task_collisions': review_task_collisions,
                    'aliases': [str(row['alias']) for row in alias_rows[:10]],
                    'alias_count': len(alias_rows),
                    'platform_terms': [str(row['term']) for row in platform_rows[:10]],
                    'platform_term_count': len(platform_rows),
                    'subscriptions': [
                        {
                            'platform': str(row['platform'] or ''),
                            'query_text': str(row['query_text'] or ''),
                            'enabled': bool(row['enabled']),
                        }
                        for row in subscription_rows
                    ],
                    'subscription_count': len(subscription_rows),
                    'status': 'ok',
                    'message': '可执行归并',
                }
                totals['image_links'] += image_links
                totals['image_link_collisions'] += image_link_collisions
                totals['review_tasks'] += review_tasks
                totals['review_task_collisions'] += review_task_collisions
                totals['aliases'] += len(alias_rows)
                totals['platform_terms'] += len(platform_rows)
                totals['subscriptions'] += len(subscription_rows)
                items.append(item)
        return True, {
            'message': f'已生成 {len(items)} 个来源 tag 的归并预览',
            'target_tag': target_name,
            'target_aliases': target_aliases,
            'items': items,
            'totals': totals,
        }

    def get_tag_id(self, tag_name: str) -> int | None:
        normalized = normalize_tag_name(tag_name)
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT id FROM tags WHERE normalized_name = ?', (normalized,)).fetchone()
            return int(row['id']) if row else None

    def get_stats(self) -> dict[str, int]:
        with self._lock, self._connect() as conn:
            images_count = conn.execute('SELECT COUNT(*) AS c FROM images WHERE is_active = 1').fetchone()['c']
            tags_count = conn.execute(
                """
                SELECT COUNT(*) AS c FROM tags t
                WHERE EXISTS (
                    SELECT 1 FROM image_tags it
                    JOIN images i ON i.id = it.image_id
                    WHERE it.tag_id = t.id
                      AND i.is_active = 1
                      AND it.review_status IN ('approved', 'manual_approved')
                )
                """
            ).fetchone()['c']
            alias_count = conn.execute('SELECT COUNT(*) AS c FROM tag_aliases').fetchone()['c']
            job_count = conn.execute('SELECT COUNT(*) AS c FROM crawl_jobs').fetchone()['c']
            subscription_count = conn.execute("SELECT COUNT(*) AS c FROM crawl_subscriptions WHERE enabled = 1").fetchone()['c']
            review_count = conn.execute("SELECT COUNT(*) AS c FROM review_tasks WHERE status IN ('pending', 'uncertain')").fetchone()['c']
        return {
            'images': int(images_count),
            'tags': int(tags_count),
            'aliases': int(alias_count),
            'crawl_jobs': int(job_count),
            'crawl_subscriptions': int(subscription_count),
            'pending_reviews': int(review_count),
        }

    def count_images_for_tag(self, tag_name: str, include_unapproved: bool = False) -> int:
        tag_id = self.get_tag_id(tag_name)
        if tag_id is None:
            return 0
        status_sql = '' if include_unapproved else " AND it.review_status IN ('approved', 'manual_approved')"
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(DISTINCT i.id) AS c
                FROM image_tags it
                JOIN images i ON i.id = it.image_id
                WHERE it.tag_id = ? AND i.is_active = 1 {status_sql}
                """,
                (tag_id,),
            ).fetchone()
            return int(row['c'])

    def resolve_tag(self, query: str, allow_fuzzy: bool = True, candidate_limit: int = 5) -> MatchResult:
        normalized = normalize_tag_name(query)
        if not normalized:
            return MatchResult(matched=False)
        with self._lock, self._connect() as conn:
            exact_tag = conn.execute('SELECT t.id, t.name FROM tags t WHERE t.normalized_name = ? LIMIT 1', (normalized,)).fetchone()
            if exact_tag:
                return MatchResult(matched=True, tag_id=int(exact_tag['id']), tag_name=str(exact_tag['name']), match_type='exact_tag')
            exact_alias = conn.execute(
                'SELECT t.id, t.name FROM tag_aliases a JOIN tags t ON t.id = a.tag_id WHERE a.normalized_alias = ? LIMIT 1',
                (normalized,),
            ).fetchone()
            if exact_alias:
                return MatchResult(matched=True, tag_id=int(exact_alias['id']), tag_name=str(exact_alias['name']), match_type='exact_alias')
            if not allow_fuzzy:
                return MatchResult(matched=False)
            candidates = conn.execute(
                """
                SELECT DISTINCT t.id, t.name, COUNT(DISTINCT i.id) AS image_count
                FROM tags t
                LEFT JOIN tag_aliases a ON a.tag_id = t.id
                LEFT JOIN image_tags it ON it.tag_id = t.id AND it.review_status IN ('approved', 'manual_approved')
                LEFT JOIN images i ON i.id = it.image_id AND i.is_active = 1
                WHERE t.normalized_name LIKE ? OR a.normalized_alias LIKE ?
                GROUP BY t.id, t.name
                ORDER BY image_count DESC, t.name ASC
                LIMIT ?
                """,
                (f'%{normalized}%', f'%{normalized}%', candidate_limit + 1),
            ).fetchall()
        if not candidates:
            return MatchResult(matched=False)
        if len(candidates) == 1:
            row = candidates[0]
            return MatchResult(matched=True, tag_id=int(row['id']), tag_name=str(row['name']), match_type='fuzzy')
        return MatchResult(matched=False, candidates=[str(row['name']) for row in candidates[:candidate_limit]])

    def merge_tags(self, target_tag_name: str, source_tag_names: Iterable[str]) -> tuple[bool, dict[str, Any]]:
        requested_sources: list[str] = []
        seen_sources: set[str] = set()
        for raw in source_tag_names:
            text = str(raw or '').strip()
            normalized = normalize_tag_name(text)
            if not text or not normalized or normalized in seen_sources:
                continue
            seen_sources.add(normalized)
            requested_sources.append(text)
        if not requested_sources:
            return False, {'message': '请至少提供一个来源 tag。'}

        now = utcnow_str()
        with self._lock, self._connect() as conn:
            target_row, target_match_type = self._resolve_tag_exact_conn(conn, target_tag_name)
            if not target_row:
                return False, {'message': f'目标 tag 不存在：{target_tag_name}'}
            target_id = int(target_row['id'])
            target_name = str(target_row['name'])
            target_normalized = str(target_row['normalized_name'] or '')
            summary: dict[str, Any] = {
                'message': '',
                'target_tag': target_name,
                'target_match_type': target_match_type,
                'merged_tags': [],
                'aliases_added': [],
                'skipped': [],
                'image_links_migrated': 0,
                'review_tasks_migrated': 0,
                'review_tasks_merged': 0,
                'aliases_migrated': 0,
                'subscriptions_migrated': 0,
                'subscriptions_merged': 0,
                'subscriptions_removed': 0,
                'aliases_skipped': [],
            }

            for source_text in requested_sources:
                source_row, _ = self._resolve_tag_exact_conn(conn, source_text)
                if not source_row:
                    ok, message = self._insert_alias_conn(conn, tag_id=target_id, alias=source_text, now=now)
                    if ok:
                        summary['aliases_added'].append(source_text)
                    else:
                        summary['skipped'].append(f'{source_text}（{message}）')
                    continue

                source_id = int(source_row['id'])
                source_name = str(source_row['name'])
                source_normalized = str(source_row['normalized_name'] or '')
                if source_id == target_id or source_normalized == target_normalized:
                    summary['skipped'].append(f'{source_text}（已归并到 {target_name}）')
                    continue

                result = self._merge_tag_into_conn(
                    conn,
                    target_id=target_id,
                    target_name=target_name,
                    source_id=source_id,
                    now=now,
                )
                summary['merged_tags'].append(result['source_name'])
                summary['image_links_migrated'] += int(result['image_links_migrated'])
                summary['review_tasks_migrated'] += int(result['review_tasks_migrated'])
                summary['review_tasks_merged'] += int(result['review_tasks_merged'])
                summary['aliases_migrated'] += int(result['aliases_migrated'])
                summary['subscriptions_migrated'] += int(result.get('subscriptions_migrated') or 0)
                summary['subscriptions_merged'] += int(result.get('subscriptions_merged') or 0)
                summary['subscriptions_removed'] += int(result['subscriptions_removed'])
                summary['aliases_skipped'].extend(list(result.get('aliases_skipped') or []))

            merged_count = len(summary['merged_tags'])
            alias_count = len(summary['aliases_added'])
            if merged_count == 0 and alias_count == 0:
                summary['message'] = '没有发生可执行的 tag 变更。'
                return False, summary
            summary['message'] = f'已归并到主 tag：{target_name}'
            return True, summary

    def switch_primary_tag(self, tag_name_or_alias: str, new_primary_name: str) -> tuple[bool, dict[str, Any]]:
        new_name = str(new_primary_name or '').strip()
        if not new_name:
            return False, {'message': '新主 tag 不能为空。'}
        now = utcnow_str()
        with self._lock, self._connect() as conn:
            current_row, match_type = self._resolve_tag_exact_conn(conn, tag_name_or_alias)
            if not current_row:
                return False, {'message': f'没有找到 tag 或 alias：{tag_name_or_alias}'}
            current_id = int(current_row['id'])
            current_name = str(current_row['name'])
            current_normalized = str(current_row['normalized_name'] or '')
            new_normalized = normalize_tag_name(new_name)
            if not new_normalized:
                return False, {'message': '新主 tag 不能为空。'}
            if new_normalized == current_normalized and new_name == current_name:
                return False, {'message': '新主 tag 与当前主 tag 相同。'}

            conflict_tag = conn.execute(
                'SELECT id, name FROM tags WHERE normalized_name = ? LIMIT 1',
                (new_normalized,),
            ).fetchone()
            if conflict_tag and int(conflict_tag['id']) != current_id:
                return False, {'message': f'新主 tag 与现有 tag 冲突：{conflict_tag["name"]}'}

            alias_usage = self._get_alias_usage_conn(conn, new_normalized)
            if alias_usage and int(alias_usage['tag_id']) != current_id:
                return False, {'message': f'新主 tag 已被 {alias_usage["tag_name"]} 的 alias 使用：{new_name}'}

            conn.execute(
                'DELETE FROM tag_aliases WHERE tag_id = ? AND normalized_alias = ?',
                (current_id, new_normalized),
            )
            conn.execute(
                'UPDATE tags SET name = ?, normalized_name = ? WHERE id = ?',
                (new_name, new_normalized, current_id),
            )

            old_name_promoted_to_alias = False
            if current_normalized != new_normalized:
                ok, _ = self._insert_alias_conn(conn, tag_id=current_id, alias=current_name, now=now)
                old_name_promoted_to_alias = ok

            return True, {
                'message': f'已切换主 tag：{current_name} -> {new_name}',
                'tag_id': current_id,
                'old_name': current_name,
                'new_name': new_name,
                'match_type': match_type,
                'old_name_promoted_to_alias': old_name_promoted_to_alias,
            }

    def get_random_image_for_tag(self, tag_id: int, excluded_image_ids: list[int] | None = None) -> sqlite3.Row | None:
        excluded_image_ids = excluded_image_ids or []
        approved_placeholder = ','.join('?' for _ in APPROVED_STATUSES)
        approved_params: tuple[Any, ...] = tuple(APPROVED_STATUSES)
        with self._lock, self._connect() as conn:
            if excluded_image_ids:
                placeholders = ','.join('?' for _ in excluded_image_ids)
                row = conn.execute(
                    f"""
                    SELECT DISTINCT i.id, i.file_path, i.file_name
                    FROM image_tags it
                    JOIN images i ON i.id = it.image_id
                    WHERE it.tag_id = ?
                      AND i.is_active = 1
                      AND it.review_status IN ({approved_placeholder})
                      AND i.id NOT IN ({placeholders})
                    ORDER BY RANDOM()
                    LIMIT 1
                    """,
                    (tag_id, *approved_params, *excluded_image_ids),
                ).fetchone()
                if row:
                    return row
            return conn.execute(
                f"""
                SELECT DISTINCT i.id, i.file_path, i.file_name
                FROM image_tags it
                JOIN images i ON i.id = it.image_id
                WHERE it.tag_id = ? AND i.is_active = 1
                  AND it.review_status IN ({approved_placeholder})
                ORDER BY RANDOM()
                LIMIT 1
                """,
                (tag_id, *approved_params),
            ).fetchone()

    def get_image_file_path(self, image_id: int) -> str | None:
        with self._lock, self._connect() as conn:
            self._sync_image_file_state(conn, image_id)
            row = conn.execute(
                "SELECT file_path FROM images WHERE id = ? AND is_active = 1",
                (image_id,),
            ).fetchone()
            return str(row["file_path"]) if row and row["file_path"] else None

    def record_send_log(self, session_id: str, image_id: int, matched_tag: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute('INSERT INTO send_logs(session_id, image_id, matched_tag, sent_at) VALUES(?, ?, ?, ?)', (session_id, image_id, matched_tag, utcnow_str()))

    def upsert_source(self, image_id: int, platform: str, post_url: str, image_url: str, author: str = '', raw_tags: list[str] | None = None, extra_json: dict[str, Any] | None = None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sources(image_id, platform, post_url, image_url, author, raw_tags, extra_json, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (image_id, platform, post_url, image_url, author, json.dumps(raw_tags or [], ensure_ascii=False), json.dumps(extra_json or {}, ensure_ascii=False), utcnow_str()),
            )

    @staticmethod
    def normalize_source_post_url(platform: str, post_url: str) -> str:
        raw_post_url = str(post_url or '').strip()
        if not raw_post_url:
            return ''
        platform_text = str(platform or '').strip().lower()
        if platform_text == 'pixiv':
            match = re.search(r'(?:artworks/|illust_id=)(\d+)', raw_post_url)
            if match:
                return f'https://www.pixiv.net/artworks/{match.group(1)}'
        return raw_post_url.rstrip('/')

    @staticmethod
    def _source_uid_from_post_url(platform: str, post_url: str) -> str:
        platform_text = str(platform or '').strip().lower()
        if platform_text == 'pixiv':
            match = re.search(r'(?:artworks/|illust_id=)(\d+)', str(post_url or ''))
            if match:
                return match.group(1)
        return ''

    def is_rejected_source_post_url(self, post_url: str, *, platform: str = '') -> bool:
        platform_text = str(platform or '').strip().lower()
        normalized_post_url = self.normalize_source_post_url(platform_text, post_url)
        if not normalized_post_url:
            return False
        sql = 'SELECT 1 FROM rejected_sources WHERE normalized_post_url = ?'
        params: list[Any] = [normalized_post_url]
        if platform_text:
            sql += ' AND platform = ?'
            params.append(platform_text)
        sql += ' LIMIT 1'
        with self._lock, self._connect() as conn:
            return conn.execute(sql, params).fetchone() is not None

    def _upsert_rejected_source_conn(
        self,
        conn: sqlite3.Connection,
        *,
        platform: str,
        post_url: str,
        image_id: int = 0,
        reason: str = '',
        now: str | None = None,
    ) -> sqlite3.Row:
        platform_text = str(platform or '').strip().lower() or 'pixiv'
        post_url_text = str(post_url or '').strip()
        normalized_post_url = self.normalize_source_post_url(platform_text, post_url_text)
        if not normalized_post_url:
            raise ValueError('来源链接不能为空')
        current_time = now or utcnow_str()
        source_uid = self._source_uid_from_post_url(platform_text, normalized_post_url)
        conn.execute(
            """
            INSERT INTO rejected_sources(platform, post_url, normalized_post_url, source_uid, image_id, reason, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, normalized_post_url) DO UPDATE SET
                post_url = excluded.post_url,
                source_uid = excluded.source_uid,
                image_id = CASE WHEN excluded.image_id > 0 THEN excluded.image_id ELSE rejected_sources.image_id END,
                reason = excluded.reason,
                updated_at = excluded.updated_at
            """,
            (
                platform_text,
                post_url_text or normalized_post_url,
                normalized_post_url,
                source_uid,
                int(image_id or 0),
                str(reason or '').strip(),
                current_time,
                current_time,
            ),
        )
        return conn.execute(
            'SELECT * FROM rejected_sources WHERE platform = ? AND normalized_post_url = ? LIMIT 1',
            (platform_text, normalized_post_url),
        ).fetchone()

    def has_source_post_url(self, post_url: str, *, platform: str = '') -> bool:
        raw_post_url = str(post_url or '').strip()
        if not raw_post_url:
            return False
        platform_text = str(platform or '').strip().lower()
        normalized_post_url = self.normalize_source_post_url(platform_text, raw_post_url)
        sql = 'SELECT 1 FROM sources WHERE (post_url = ? OR post_url = ?)'
        params: list[Any] = [raw_post_url, normalized_post_url]
        if platform_text:
            sql += ' AND platform = ?'
            params.append(platform_text)
        sql += ' LIMIT 1'
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return row is not None

    def reject_image_source(
        self,
        image_id: int,
        *,
        platform: str = 'pixiv',
        reason: str = '',
    ) -> tuple[bool, dict[str, Any]]:
        platform_text = str(platform or 'pixiv').strip().lower() or 'pixiv'
        now = utcnow_str()
        rejected_reason = str(reason or '').strip() or f'{platform_text} 人工拒绝图片'
        with self._lock, self._connect() as conn:
            image = conn.execute('SELECT id FROM images WHERE id = ? AND is_active = 1 LIMIT 1', (int(image_id),)).fetchone()
            if not image:
                return False, {'message': f'图片不存在：{image_id}'}
            source = conn.execute(
                """
                SELECT post_url
                FROM sources
                WHERE image_id = ? AND platform = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(image_id), platform_text),
            ).fetchone()
            if not source:
                return False, {'message': f'图片 #{image_id} 没有 {platform_text} 来源记录。'}

            rejected_source = self._upsert_rejected_source_conn(
                conn,
                platform=platform_text,
                post_url=str(source['post_url'] or ''),
                image_id=int(image_id),
                reason=rejected_reason,
                now=now,
            )
            tasks = conn.execute(
                """
                SELECT rt.id, t.name AS tag_name
                FROM review_tasks rt
                JOIN tags t ON t.id = rt.tag_id
                WHERE rt.image_id = ?
                """,
                (int(image_id),),
            ).fetchall()
            conn.execute(
                """
                UPDATE review_tasks
                SET status = 'manual_rejected', manual_result = 'rejected', reason = ?, updated_at = ?
                WHERE image_id = ?
                """,
                (rejected_reason, now, int(image_id)),
            )
            cursor = conn.execute(
                """
                UPDATE image_tags
                SET review_status = 'manual_rejected', review_reason = ?, updated_at = ?
                WHERE image_id = ?
                """,
                (rejected_reason, now, int(image_id)),
            )

        return True, {
            'message': f'已拒绝图片 #{image_id}，后续 Pixiv 来源搜图会跳过该作品',
            'image_id': int(image_id),
            'post_url': str(rejected_source['normalized_post_url'] or source['post_url']),
            'rejected_tasks': [str(row['tag_name']) for row in tasks],
            'rejected_tag_links': int(cursor.rowcount if cursor.rowcount is not None else 0),
        }

    def create_crawl_job(
        self,
        platform: str,
        source_url: str,
        tags: list[str],
        *,
        include_tags: list[str] | None = None,
        exclude_tags: list[str] | None = None,
        match_mode: str = 'exact',
    ) -> int:
        now = utcnow_str()
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO crawl_jobs(
                    platform, source_url, tags_text, include_tags_text, exclude_tags_text, tag_match_mode,
                    status, progress, error_log, result_summary, attempt_count, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, 'pending', 0, '', '', 0, ?, ?)
                """,
                (
                    platform,
                    source_url,
                    ','.join(tags),
                    ','.join(include_tags or []),
                    ','.join(exclude_tags or []),
                    str(match_mode or 'exact'),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def get_crawl_job(self, job_id: int) -> sqlite3.Row | None:
        with self._lock, self._connect() as conn:
            return conn.execute('SELECT * FROM crawl_jobs WHERE id = ?', (job_id,)).fetchone()

    def update_crawl_job(self, job_id: int, *, status: str | None = None, progress: int | None = None, error_log: str | None = None, result_summary: str | None = None, attempt_count: int | None = None) -> None:
        fields: list[str] = ['updated_at = ?']
        params: list[Any] = [utcnow_str()]
        if status is not None:
            fields.append('status = ?')
            params.append(status)
        if progress is not None:
            fields.append('progress = ?')
            params.append(progress)
        if error_log is not None:
            fields.append('error_log = ?')
            params.append(error_log)
        if result_summary is not None:
            fields.append('result_summary = ?')
            params.append(result_summary)
        if attempt_count is not None:
            fields.append('attempt_count = ?')
            params.append(attempt_count)
        params.append(job_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE crawl_jobs SET {', '.join(fields)} WHERE id = ?", params)

    def increment_crawl_job_attempt(self, job_id: int) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT attempt_count FROM crawl_jobs WHERE id = ?', (job_id,)).fetchone()
            current = int(row['attempt_count'] or 0) if row else 0
            current += 1
            conn.execute('UPDATE crawl_jobs SET attempt_count = ?, updated_at = ? WHERE id = ?', (current, utcnow_str(), job_id))
            return current

    def list_crawl_jobs(self, *, limit: int = 20, statuses: Iterable[str] | None = None) -> list[sqlite3.Row]:
        sql = 'SELECT * FROM crawl_jobs'
        params: list[Any] = []
        if statuses:
            placeholders = ','.join('?' for _ in statuses)
            sql += f' WHERE status IN ({placeholders})'
            params.extend(list(statuses))
        sql += ' ORDER BY id DESC LIMIT ?'
        params.append(limit)
        with self._lock, self._connect() as conn:
            return conn.execute(sql, params).fetchall()

    def get_pending_job_ids(self) -> list[int]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT id FROM crawl_jobs WHERE status IN ('pending', 'retry') ORDER BY id ASC").fetchall()
            return [int(row['id']) for row in rows]

    def reset_running_jobs(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE crawl_jobs SET status = 'retry', updated_at = ? WHERE status = 'running'", (utcnow_str(),))

    def create_review_task(self, image_id: int, tag_id: int, status: str, model_result: str = '', reason: str = '') -> int:
        now = utcnow_str()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM review_tasks WHERE image_id = ? AND tag_id = ? ORDER BY id DESC LIMIT 1",
                (image_id, tag_id),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE review_tasks SET status = ?, model_result = ?, reason = ?, updated_at = ? WHERE id = ?",
                    (status, model_result, reason, now, existing['id']),
                )
                return int(existing['id'])
            cursor = conn.execute(
                """
                INSERT INTO review_tasks(image_id, tag_id, status, model_result, manual_result, reason, created_at, updated_at)
                VALUES(?, ?, ?, ?, '', ?, ?, ?)
                """,
                (image_id, tag_id, status, model_result, reason, now, now),
            )
            return int(cursor.lastrowid)

    def list_review_tasks(
        self,
        *,
        status: str | None = None,
        statuses: Iterable[str] | None = None,
        limit: int = 20,
    ) -> list[sqlite3.Row]:
        sql = """
            SELECT rt.id, rt.status, rt.reason, rt.model_result, rt.manual_result,
                   rt.created_at, rt.updated_at,
                   i.id AS image_id, i.file_path,
                   t.id AS tag_id, t.name AS tag_name,
                   COALESCE((
                       SELECT it.source_type
                       FROM image_tags it
                       WHERE it.image_id = rt.image_id AND it.tag_id = rt.tag_id
                       ORDER BY CASE
                           WHEN it.source_type LIKE 'crawl:%' THEN 0
                           WHEN it.source_type LIKE 'manual:%' THEN 1
                           ELSE 2
                       END, it.id DESC
                       LIMIT 1
                   ), '') AS source_type
            FROM review_tasks rt
            JOIN images i ON i.id = rt.image_id
            JOIN tags t ON t.id = rt.tag_id
        """
        params: list[Any] = []
        normalized_statuses = [str(item).strip() for item in (statuses or []) if str(item).strip()]
        if normalized_statuses:
            placeholders = ",".join("?" for _ in normalized_statuses)
            sql += f" WHERE rt.status IN ({placeholders})"
            params.extend(normalized_statuses)
        elif status:
            sql += ' WHERE rt.status = ?'
            params.append(status)
        sql += ' ORDER BY rt.id DESC LIMIT ?'
        params.append(limit)
        with self._lock, self._connect() as conn:
            return conn.execute(sql, params).fetchall()

    def get_review_task(self, review_id: int) -> sqlite3.Row | None:
        with self._lock, self._connect() as conn:
            return conn.execute(
                """
                SELECT rt.id, rt.status, rt.reason, rt.model_result, rt.manual_result,
                       i.id AS image_id, i.file_path,
                       t.id AS tag_id, t.name AS tag_name,
                       COALESCE((
                           SELECT it.source_type
                           FROM image_tags it
                           WHERE it.image_id = rt.image_id AND it.tag_id = rt.tag_id
                           ORDER BY CASE
                               WHEN it.source_type LIKE 'crawl:%' THEN 0
                               WHEN it.source_type LIKE 'manual:%' THEN 1
                               ELSE 2
                           END, it.id DESC
                           LIMIT 1
                       ), '') AS source_type
                FROM review_tasks rt
                JOIN images i ON i.id = rt.image_id
                JOIN tags t ON t.id = rt.tag_id
                WHERE rt.id = ?
                """,
                (review_id,),
            ).fetchone()

    def apply_manual_review(self, review_id: int, *, approved: bool, reason: str = '') -> tuple[bool, str]:
        new_status = 'manual_approved' if approved else 'manual_rejected'
        now = utcnow_str()
        review_reason = reason or ('人工通过' if approved else '人工拒绝')
        with self._lock, self._connect() as conn:
            task = conn.execute(
                """
                SELECT rt.id, rt.image_id, rt.tag_id,
                       COALESCE((
                           SELECT it.source_type
                           FROM image_tags it
                           WHERE it.image_id = rt.image_id AND it.tag_id = rt.tag_id
                           ORDER BY CASE
                               WHEN it.source_type LIKE 'crawl:%' THEN 0
                               WHEN it.source_type LIKE 'manual:%' THEN 1
                               ELSE 2
                           END, it.id DESC
                           LIMIT 1
                       ), '') AS source_type
                FROM review_tasks rt
                WHERE rt.id = ?
                LIMIT 1
                """,
                (review_id,),
            ).fetchone()
            if not task:
                return False, f'审核任务不存在：{review_id}'
            conn.execute(
                "UPDATE review_tasks SET status = ?, manual_result = ?, reason = ?, updated_at = ? WHERE id = ?",
                (new_status, 'approved' if approved else 'rejected', reason, now, review_id),
            )
            source_type = str(task['source_type'] or '').strip()
            if source_type:
                conn.execute(
                    """
                    UPDATE image_tags
                    SET review_status = ?, review_reason = ?, updated_at = ?
                    WHERE image_id = ? AND tag_id = ? AND source_type = ?
                    """,
                    (new_status, review_reason, now, int(task['image_id']), int(task['tag_id']), source_type),
                )
            else:
                conn.execute(
                    """
                    UPDATE image_tags
                    SET review_status = ?, review_reason = ?, updated_at = ?
                    WHERE image_id = ? AND tag_id = ? AND source_type = ''
                    """,
                    (new_status, review_reason, now, int(task['image_id']), int(task['tag_id'])),
                )
        return True, f"已{'通过' if approved else '拒绝'}审核任务 #{review_id}"


    def get_review_tasks_for_image(
        self,
        image_id: int,
        *,
        statuses: Iterable[str] | None = None,
    ) -> list[sqlite3.Row]:
        sql = """
            SELECT rt.id, rt.status, rt.reason, rt.model_result, rt.manual_result,
                   rt.created_at, rt.updated_at,
                   t.id AS tag_id, t.name AS tag_name, t.is_character,
                   COALESCE((
                       SELECT it.source_type
                       FROM image_tags it
                       WHERE it.image_id = rt.image_id AND it.tag_id = rt.tag_id
                       ORDER BY CASE
                           WHEN it.source_type LIKE 'crawl:%' THEN 0
                           WHEN it.source_type LIKE 'manual:%' THEN 1
                           ELSE 2
                       END, it.id DESC
                       LIMIT 1
                   ), '') AS source_type
            FROM review_tasks rt
            JOIN tags t ON t.id = rt.tag_id
            WHERE rt.image_id = ?
        """
        params: list[Any] = [image_id]
        normalized_statuses = [str(item).strip() for item in (statuses or []) if str(item).strip()]
        if normalized_statuses:
            placeholders = ','.join('?' for _ in normalized_statuses)
            sql += f' AND rt.status IN ({placeholders})'
            params.extend(normalized_statuses)
        sql += ' ORDER BY rt.id DESC'
        with self._lock, self._connect() as conn:
            return conn.execute(sql, params).fetchall()

    def _build_pixiv_review_images_query(
        self,
        *,
        statuses: Iterable[str] | None = None,
        keyword: str = '',
        search_context: dict[str, Any] | None = None,
        count: bool = False,
    ) -> tuple[str, list[Any]]:
        normalized_statuses = split_status_filter(statuses or ['pending', 'uncertain'])
        keyword_text = str(keyword or '').strip()
        context = search_context if search_context is not None else self.build_pixiv_review_search_context(keyword_text)
        select_sql = "SELECT i.id AS image_id" if count else """
            SELECT i.id AS image_id,
                   MAX(rt.id) AS latest_review_id,
                   MAX(rt.updated_at) AS latest_updated_at,
                   COUNT(DISTINCT rt.id) AS review_task_count,
                   COUNT(DISTINCT CASE WHEN rt.status IN ('pending', 'uncertain') THEN rt.id END) AS pending_task_count
        """
        sql = f"""
            {select_sql}
            FROM review_tasks rt
            JOIN images i ON i.id = rt.image_id
            WHERE i.is_active = 1
              AND EXISTS (
                  SELECT 1 FROM sources s
                  WHERE s.image_id = rt.image_id AND s.platform = 'pixiv'
              )
        """
        params: list[Any] = []
        if normalized_statuses:
            placeholders = ','.join('?' for _ in normalized_statuses)
            sql += f' AND rt.status IN ({placeholders})'
            params.extend(normalized_statuses)
        if keyword_text and context.get('has_query'):
            search_clauses: list[str] = []
            search_params: list[Any] = []
            target_ids = [
                int(item)
                for item in (context.get('tag_ids') or [])
                if int(item or 0) > 0
            ]
            if target_ids:
                id_placeholders = ','.join('?' for _ in target_ids)
                review_status_sql = ''
                review_status_params: list[Any] = []
                if normalized_statuses:
                    review_status_placeholders = ','.join('?' for _ in normalized_statuses)
                    review_status_sql = f' AND rt2.status IN ({review_status_placeholders})'
                    review_status_params.extend(normalized_statuses)
                search_clauses.append(
                    f"""
                    EXISTS (
                        SELECT 1
                        FROM review_tasks rt2
                        WHERE rt2.image_id = i.id
                          AND rt2.tag_id IN ({id_placeholders})
                          {review_status_sql}
                    )
                    """
                )
                search_params.extend(target_ids)
                search_params.extend(review_status_params)
                search_clauses.append(
                    f"""
                    EXISTS (
                        SELECT 1
                        FROM image_tags it2
                        WHERE it2.image_id = i.id
                          AND it2.tag_id IN ({id_placeholders})
                    )
                    """
                )
                search_params.extend(target_ids)

            normalized_terms = [
                normalize_tag_name(str(item or ''))
                for item in (context.get('terms') or [])
                if normalize_tag_name(str(item or ''))
            ]
            normalized_terms = list(dict.fromkeys(normalized_terms))[:80]
            if normalized_terms:
                tag_like_clauses = []
                tag_like_params: list[Any] = []
                for term in normalized_terms:
                    tag_like_clauses.append('t2.normalized_name LIKE ?')
                    tag_like_params.append(f'%{term}%')
                search_clauses.append(
                    f"""
                    EXISTS (
                        SELECT 1
                        FROM review_tasks rt2
                        JOIN tags t2 ON t2.id = rt2.tag_id
                        WHERE rt2.image_id = i.id
                          {'AND rt2.status IN (' + ','.join('?' for _ in normalized_statuses) + ')' if normalized_statuses else ''}
                          AND ({' OR '.join(tag_like_clauses)})
                    )
                    """
                )
                if normalized_statuses:
                    search_params.extend(normalized_statuses)
                search_params.extend(tag_like_params)
                search_clauses.append(
                    f"""
                    EXISTS (
                        SELECT 1
                        FROM image_tags it2
                        JOIN tags t2 ON t2.id = it2.tag_id
                        WHERE it2.image_id = i.id
                          AND ({' OR '.join(tag_like_clauses)})
                    )
                    """
                )
                search_params.extend(tag_like_params)

            text_terms = self._dedupe_terms([str(item) for item in (context.get('terms') or [])])[:80]
            if text_terms:
                source_clauses = []
                source_params: list[Any] = []
                for term in text_terms:
                    source_clauses.append('(s2.raw_tags LIKE ? OR s2.extra_json LIKE ?)')
                    source_params.extend([f'%{term}%', f'%{term}%'])
                search_clauses.append(
                    f"""
                    EXISTS (
                        SELECT 1
                        FROM sources s2
                        WHERE s2.image_id = i.id
                          AND s2.platform = 'pixiv'
                          AND ({' OR '.join(source_clauses)})
                    )
                    """
                )
                search_params.extend(source_params)

            if search_clauses:
                sql += ' AND (' + ' OR '.join(search_clauses) + ')'
                params.extend(search_params)
        return sql, params

    def list_pixiv_review_images(
        self,
        *,
        statuses: Iterable[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        keyword: str = '',
        search_context: dict[str, Any] | None = None,
    ) -> list[sqlite3.Row]:
        sql, params = self._build_pixiv_review_images_query(
            statuses=statuses,
            keyword=keyword,
            search_context=search_context,
        )
        sql += ' GROUP BY i.id ORDER BY latest_review_id DESC LIMIT ? OFFSET ?'
        params.extend([limit, offset])
        with self._lock, self._connect() as conn:
            return conn.execute(sql, params).fetchall()

    def count_pixiv_review_images(
        self,
        *,
        statuses: Iterable[str] | None = None,
        keyword: str = '',
        search_context: dict[str, Any] | None = None,
    ) -> int:
        sql, params = self._build_pixiv_review_images_query(
            statuses=statuses,
            keyword=keyword,
            search_context=search_context,
            count=True,
        )
        count_sql = f"SELECT COUNT(*) AS total FROM ({sql} GROUP BY i.id) AS pixiv_review_page"
        with self._lock, self._connect() as conn:
            row = conn.execute(count_sql, params).fetchone()
        return int(row["total"] or 0) if row else 0

    def build_pixiv_review_search_context(self, keyword: str, *, platform: str = 'pixiv') -> dict[str, Any]:
        keyword_text = str(keyword or '').strip()
        normalized_keyword = normalize_tag_name(keyword_text)
        platform_text = str(platform or 'pixiv').strip().lower() or 'pixiv'
        context: dict[str, Any] = {
            'keyword': keyword_text,
            'normalized_keyword': normalized_keyword,
            'has_query': bool(keyword_text and normalized_keyword),
            'matched_tags': [],
            'expanded_terms': [],
            'terms': [],
            'tag_ids': [],
            'message': '',
        }
        if not context['has_query']:
            return context

        seen_tag_ids: set[int] = set()
        seen_terms: set[str] = set()

        def push_term(value: str) -> None:
            text = str(value or '').strip()
            normalized = normalize_tag_name(text)
            if not text or not normalized or normalized in seen_terms:
                return
            seen_terms.add(normalized)
            context['terms'].append(text)
            context['expanded_terms'].append(text)

        def push_tag(
            conn: sqlite3.Connection,
            row: sqlite3.Row | None,
            match_type: str,
            *,
            include_suggestions: bool = True,
        ) -> None:
            if not row:
                return
            tag_id = int(row['id'])
            tag_name = str(row['name'])
            if tag_id not in seen_tag_ids:
                seen_tag_ids.add(tag_id)
                context['tag_ids'].append(tag_id)
                context['matched_tags'].append(
                    {
                        'id': tag_id,
                        'name': tag_name,
                        'match_type': match_type,
                    }
                )
            push_term(tag_name)
            alias_rows = conn.execute(
                'SELECT alias FROM tag_aliases WHERE tag_id = ? ORDER BY alias ASC',
                (tag_id,),
            ).fetchall()
            for alias_row in alias_rows:
                push_term(str(alias_row['alias']))
            platform_rows = conn.execute(
                """
                SELECT term
                FROM platform_tag_terms
                WHERE tag_id = ? AND platform = ?
                ORDER BY confidence DESC, term ASC
                LIMIT 100
                """,
                (tag_id, platform_text),
            ).fetchall()
            for term_row in platform_rows:
                push_term(str(term_row['term']))
            if include_suggestions:
                source_rows = conn.execute(
                    """
                    SELECT s.raw_tags, s.extra_json
                    FROM sources s
                    JOIN image_tags it ON it.image_id = s.image_id
                    WHERE it.tag_id = ?
                      AND s.platform = ?
                      AND it.review_status IN ('approved', 'manual_approved')
                    ORDER BY s.id DESC
                    LIMIT 200
                    """,
                    (tag_id, platform_text),
                ).fetchall()
                for source_row in source_rows:
                    try:
                        raw_terms = json.loads(source_row['raw_tags'] or '[]') if source_row['raw_tags'] else []
                    except Exception:
                        raw_terms = []
                    try:
                        extra = json.loads(source_row['extra_json'] or '{}') if source_row['extra_json'] else {}
                    except Exception:
                        extra = {}
                    translated_terms = extra.get('translated_tags') if isinstance(extra, dict) else []
                    if not isinstance(translated_terms, list):
                        translated_terms = []
                    for candidate_term in [*raw_terms, *translated_terms]:
                        candidate_row, candidate_match_type = self._resolve_tag_exact_conn(conn, str(candidate_term or ''))
                        if (
                            candidate_row
                            and int(candidate_row['id']) != tag_id
                            and self._looks_like_platform_term(str(candidate_term or ''))
                        ):
                            push_tag(
                                conn,
                                candidate_row,
                                f'suggested_{candidate_match_type or "tag"}',
                                include_suggestions=False,
                            )

        push_term(keyword_text)
        with self._lock, self._connect() as conn:
            exact_row, exact_type = self._resolve_tag_exact_conn(conn, keyword_text)
            push_tag(conn, exact_row, exact_type)
            platform_row = self._resolve_platform_term_exact_conn(conn, platform_text, keyword_text)
            if platform_row:
                tag_row = conn.execute('SELECT * FROM tags WHERE id = ? LIMIT 1', (int(platform_row['tag_id']),)).fetchone()
                push_tag(conn, tag_row, f'platform:{platform_text}')

            like_normalized = f'%{normalized_keyword}%'
            like_text = f'%{keyword_text}%'
            fuzzy_rows = conn.execute(
                """
                SELECT DISTINCT t.id, t.name,
                       CASE
                           WHEN t.normalized_name LIKE ? OR t.name LIKE ? THEN 'fuzzy_tag'
                           ELSE 'fuzzy_alias'
                       END AS match_type
                FROM tags t
                LEFT JOIN tag_aliases a ON a.tag_id = t.id
                WHERE t.normalized_name LIKE ?
                   OR t.name LIKE ?
                   OR a.normalized_alias LIKE ?
                   OR a.alias LIKE ?
                ORDER BY t.name ASC
                LIMIT 20
                """,
                (like_normalized, like_text, like_normalized, like_text, like_normalized, like_text),
            ).fetchall()
            for row in fuzzy_rows:
                push_tag(conn, row, str(row['match_type'] or 'fuzzy_tag'))

            platform_rows = conn.execute(
                """
                SELECT DISTINCT t.id, t.name, 'fuzzy_platform' AS match_type
                FROM platform_tag_terms p
                JOIN tags t ON t.id = p.tag_id
                WHERE p.platform = ?
                  AND (p.normalized_term LIKE ? OR p.term LIKE ?)
                ORDER BY t.name ASC
                LIMIT 20
                """,
                (platform_text, like_normalized, like_text),
            ).fetchall()
            for row in platform_rows:
                push_tag(conn, row, str(row['match_type'] or 'fuzzy_platform'))

        matched_names = [str(item['name']) for item in context['matched_tags']]
        term_preview = [str(item) for item in context['expanded_terms'][:12]]
        if matched_names:
            context['message'] = (
                f"{keyword_text} 命中：" + '、'.join(matched_names[:6])
                + ("；展开词：" + '、'.join(term_preview) if term_preview else '')
            )
        else:
            context['message'] = f"{keyword_text} 未命中主 tag；已按待审 tag / Pixiv 来源词直接模糊搜索"
        return context

    def apply_image_review(
        self,
        image_id: int,
        *,
        selected_tag_names: Iterable[str],
        source_terms: Iterable[str] | None = None,
        platform: str = 'pixiv',
        reason: str = '',
        reject_unselected: bool = True,
    ) -> tuple[bool, dict[str, Any]]:
        requested_tags: list[str] = []
        seen_tags: set[str] = set()
        for raw in selected_tag_names:
            text = str(raw or '').strip()
            normalized = normalize_tag_name(text)
            if not text or not normalized or normalized in seen_tags:
                continue
            seen_tags.add(normalized)
            requested_tags.append(text)
        if not requested_tags:
            return False, {'message': '请至少选择一个归入主 tag。'}

        requested_terms: list[str] = []
        seen_terms: set[str] = set()
        for raw in (source_terms or []):
            text = str(raw or '').strip()
            normalized = normalize_tag_name(text)
            if not text or not normalized or normalized in seen_terms:
                continue
            seen_terms.add(normalized)
            requested_terms.append(text)
        canonical_tag_name = requested_tags[0]
        alias_terms = self._dedupe_terms([*requested_tags[1:], *requested_terms])

        platform_text = str(platform or 'pixiv').strip().lower() or 'pixiv'
        now = utcnow_str()
        approved_reason = str(reason or '').strip() or f'{platform_text} 人工审批通过'
        rejected_reason = str(reason or '').strip() or f'{platform_text} 人工审批拒绝'

        with self._lock, self._connect() as conn:
            image = conn.execute('SELECT id FROM images WHERE id = ? AND is_active = 1 LIMIT 1', (image_id,)).fetchone()
            if not image:
                return False, {'message': f'图片不存在：{image_id}'}
            source_exists = conn.execute(
                'SELECT 1 FROM sources WHERE image_id = ? AND platform = ? LIMIT 1',
                (image_id, platform_text),
            ).fetchone()
            if not source_exists:
                return False, {'message': f'图片 #{image_id} 没有 {platform_text} 来源记录。'}

            target_row, _ = self._resolve_tag_exact_conn(conn, canonical_tag_name)
            if not target_row:
                return False, {'message': f'归入主 tag 不存在：{canonical_tag_name}'}

            target_id = int(target_row['id'])
            target_name = str(target_row['name'])
            selected_ids = {target_id}
            selected_names = [target_name]
            def fetch_review_tasks() -> list[sqlite3.Row]:
                return conn.execute(
                    """
                    SELECT rt.id, rt.tag_id, rt.status, t.name AS tag_name,
                           COALESCE((
                               SELECT it.source_type
                               FROM image_tags it
                               WHERE it.image_id = rt.image_id AND it.tag_id = rt.tag_id
                               ORDER BY CASE
                                   WHEN it.source_type = ? THEN 0
                                   WHEN it.source_type LIKE 'crawl:%' THEN 1
                                   WHEN it.source_type LIKE 'manual:%' THEN 2
                                   ELSE 3
                               END, it.id DESC
                               LIMIT 1
                           ), '') AS source_type
                    FROM review_tasks rt
                    JOIN tags t ON t.id = rt.tag_id
                    WHERE rt.image_id = ?
                    ORDER BY rt.id DESC
                    """,
                    (f'crawl:{platform_text}', image_id),
                ).fetchall()

            tasks = fetch_review_tasks()

            def upsert_image_tag_review(tag_id: int, status: str, reason_text: str, default_source_type: str) -> str:
                image_tag_rows = conn.execute(
                    """
                    SELECT id, source_type
                    FROM image_tags
                    WHERE image_id = ? AND tag_id = ?
                    ORDER BY CASE
                        WHEN source_type = ? THEN 0
                        WHEN source_type LIKE 'crawl:%' THEN 1
                        WHEN source_type LIKE 'manual:%' THEN 2
                        ELSE 3
                    END, id DESC
                    """,
                    (image_id, tag_id, f'crawl:{platform_text}'),
                ).fetchall()
                if image_tag_rows:
                    chosen = image_tag_rows[0]
                    chosen_source_type = str(chosen['source_type'] or '').strip() or default_source_type
                    conn.execute(
                        'UPDATE image_tags SET review_status = ?, review_reason = ?, updated_at = ? WHERE image_id = ? AND tag_id = ? AND source_type = ?',
                        (status, reason_text, now, image_id, tag_id, chosen_source_type),
                    )
                    return chosen_source_type
                conn.execute(
                    """
                    INSERT INTO image_tags(image_id, tag_id, source_type, score, review_status, review_reason, created_at, updated_at)
                    VALUES(?, ?, ?, 1.0, ?, ?, ?, ?)
                    """,
                    (image_id, tag_id, default_source_type, status, reason_text, now, now),
                )
                return default_source_type

            mapped_terms: list[dict[str, Any]] = []
            skipped_terms: list[str] = []
            aliases_added: list[str] = []
            merged_tags: list[str] = []
            merged_source_ids: set[int] = set()
            for term in alias_terms:
                plan = self._apply_review_alias_term_conn(
                    conn,
                    platform=platform_text,
                    target_row=target_row,
                    term=term,
                    now=now,
                )
                if plan.get('status') != 'mapped':
                    skipped_terms.append(f'{term}（{str(plan.get("message") or "无法沉淀")}）')
                    continue
                mapped_terms.append(
                    {
                        'term': str(plan['term']),
                        'tag_name': str(plan['tag_name']),
                        'action': str(plan.get('action') or 'add'),
                    }
                )
                if plan.get('alias_added'):
                    aliases_added.append(str(plan['term']))
                if plan.get('merged_tag'):
                    merged_tags.append(str(plan['merged_tag']))
                source_tag_id = int(plan.get('source_tag_id') or 0)
                if source_tag_id > 0 and str(plan.get('action') or '') == 'merge_tag':
                    merged_source_ids.add(source_tag_id)

            tasks = fetch_review_tasks()
            task_by_tag_id = {int(row['tag_id']): row for row in tasks}
            source_type = upsert_image_tag_review(target_id, 'manual_approved', approved_reason, f'manual:{platform_text}_review')
            task = task_by_tag_id.get(target_id)
            if task:
                conn.execute(
                    """
                    UPDATE review_tasks
                    SET status = 'manual_approved', manual_result = 'approved', reason = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (approved_reason, now, int(task['id'])),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO review_tasks(image_id, tag_id, status, model_result, manual_result, reason, created_at, updated_at)
                    VALUES(?, ?, 'manual_approved', '', 'approved', ?, ?, ?)
                    """,
                    (image_id, target_id, approved_reason, now, now),
                )
                task_by_tag_id[target_id] = {
                    'id': int(cursor.lastrowid),
                    'tag_id': target_id,
                    'tag_name': target_name,
                    'source_type': source_type,
                }

            rejected_names: list[str] = []
            if reject_unselected:
                for task in tasks:
                    tag_id = int(task['tag_id'])
                    if tag_id in selected_ids or tag_id in merged_source_ids:
                        continue
                    conn.execute(
                        """
                        UPDATE review_tasks
                        SET status = 'manual_rejected', manual_result = 'rejected', reason = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (rejected_reason, now, int(task['id'])),
                    )
                    source_type = str(task['source_type'] or '').strip()
                    if source_type:
                        conn.execute(
                            'UPDATE image_tags SET review_status = ?, review_reason = ?, updated_at = ? WHERE image_id = ? AND tag_id = ? AND source_type = ?',
                            ('manual_rejected', rejected_reason, now, image_id, tag_id, source_type),
                        )
                    else:
                        conn.execute(
                            'UPDATE image_tags SET review_status = ?, review_reason = ?, updated_at = ? WHERE image_id = ? AND tag_id = ? AND source_type = ?',
                            ('manual_rejected', rejected_reason, now, image_id, tag_id, ''),
                        )
                    rejected_names.append(str(task['tag_name']))

        return True, {
            'message': f'已完成图片 #{image_id} 的人工审核',
            'image_id': image_id,
            'canonical_tag': target_name,
            'approved_tags': selected_names,
            'rejected_tags': rejected_names,
            'alias_terms': alias_terms,
            'aliases_added': aliases_added,
            'merged_tags': merged_tags,
            'mapped_terms': mapped_terms,
            'skipped_terms': skipped_terms,
        }

    def _build_search_images_query(
        self,
        *,
        keyword: str = '',
        review_status: str = '',
        tag_name: str = '',
        platform: str = '',
        count: bool = False,
    ) -> tuple[str, list[Any]]:
        select_sql = "SELECT COUNT(DISTINCT i.id) AS total" if count else """
            SELECT DISTINCT i.id, i.file_path, i.file_name, i.width, i.height, i.format, i.phash, i.updated_at
        """
        sql = f"""
            {select_sql}
            FROM images i
            LEFT JOIN image_tags it ON it.image_id = i.id
            LEFT JOIN tags t ON t.id = it.tag_id
            LEFT JOIN tag_aliases a ON a.tag_id = t.id
            LEFT JOIN sources s ON s.image_id = i.id
            WHERE i.is_active = 1
        """
        params: list[Any] = []
        if keyword:
            normalized = normalize_tag_name(keyword)
            sql += " AND (i.file_name LIKE ? OR t.normalized_name LIKE ? OR a.normalized_alias LIKE ? OR s.post_url LIKE ? OR s.author LIKE ?)"
            params.extend([f'%{keyword}%', f'%{normalized}%', f'%{normalized}%', f'%{keyword}%', f'%{keyword}%'])
        statuses = split_status_filter(review_status)
        if statuses:
            placeholders = ','.join('?' for _ in statuses)
            sql += f' AND it.review_status IN ({placeholders})'
            params.extend(statuses)
        if tag_name:
            sql += ' AND t.normalized_name = ?'
            params.append(normalize_tag_name(tag_name))
        if platform:
            sql += ' AND s.platform = ?'
            params.append(platform)
        return sql, params

    def search_images(self, *, keyword: str = '', review_status: str = '', tag_name: str = '', platform: str = '', limit: int = 100, offset: int = 0) -> list[sqlite3.Row]:
        sql, params = self._build_search_images_query(
            keyword=keyword,
            review_status=review_status,
            tag_name=tag_name,
            platform=platform,
        )
        sql += ' ORDER BY i.id DESC LIMIT ? OFFSET ?'
        params.extend([limit, offset])
        with self._lock, self._connect() as conn:
            return conn.execute(sql, params).fetchall()

    def count_search_images(self, *, keyword: str = '', review_status: str = '', tag_name: str = '', platform: str = '') -> int:
        sql, params = self._build_search_images_query(
            keyword=keyword,
            review_status=review_status,
            tag_name=tag_name,
            platform=platform,
            count=True,
        )
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["total"] or 0) if row else 0

    def get_image_detail(self, image_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            self._sync_image_file_state(conn, image_id)
            image = conn.execute('SELECT * FROM images WHERE id = ?', (image_id,)).fetchone()
            if not image:
                return None
            file_locations = conn.execute(
                """
                SELECT file_path, file_name, storage_type, is_active, created_at, updated_at
                FROM image_files
                WHERE image_id = ?
                ORDER BY updated_at DESC, id DESC
                """,
                (image_id,),
            ).fetchall()
            tags = conn.execute(
                """
                SELECT t.name, t.is_character, it.source_type, it.review_status, it.review_reason, it.score
                FROM image_tags it
                JOIN tags t ON t.id = it.tag_id
                WHERE it.image_id = ?
                ORDER BY t.name ASC
                """,
                (image_id,),
            ).fetchall()
            sources = conn.execute('SELECT platform, post_url, image_url, author, raw_tags, extra_json FROM sources WHERE image_id = ?', (image_id,)).fetchall()
            return {
                'image': dict(image),
                'file_locations': [
                    {
                        'file_path': str(row['file_path']),
                        'file_name': str(row['file_name']),
                        'storage_type': str(row['storage_type']),
                        'is_active': bool(row['is_active']),
                        'created_at': str(row['created_at']),
                        'updated_at': str(row['updated_at']),
                    }
                    for row in file_locations
                ],
                'tags': [
                    {
                        'name': str(row['name']),
                        'is_character': bool(row['is_character']),
                        'source_type': str(row['source_type']),
                        'review_status': str(row['review_status']),
                        'review_reason': str(row['review_reason']),
                        'score': float(row['score']),
                    }
                    for row in tags
                ],
                'sources': [
                    {
                        'platform': str(row['platform']),
                        'post_url': str(row['post_url']),
                        'image_url': str(row['image_url']),
                        'author': str(row['author']),
                        'raw_tags': json.loads(row['raw_tags'] or '[]'),
                        'extra': json.loads(row['extra_json'] or '{}'),
                    }
                    for row in sources
                ],
            }

    def trash_image(self, image_id: int, *, trash_path: str | None = None) -> tuple[bool, str]:
        now = utcnow_str()
        with self._lock, self._connect() as conn:
            image = conn.execute('SELECT id FROM images WHERE id = ?', (image_id,)).fetchone()
            if not image:
                return False, f'图片不存在：{image_id}'

            conn.execute(
                'UPDATE image_files SET is_active = 0, updated_at = ? WHERE image_id = ? AND is_active = 1',
                (now, image_id),
            )

            if trash_path:
                trash_file_name = Path(trash_path).name
                self._upsert_file_location(
                    conn,
                    image_id=image_id,
                    file_path=trash_path,
                    file_name=trash_file_name,
                    storage_type='trash',
                    now=now,
                )
                conn.execute(
                    'UPDATE image_files SET is_active = 0, updated_at = ? WHERE image_id = ? AND file_path = ?',
                    (now, image_id, trash_path),
                )
                conn.execute(
                    'UPDATE images SET file_path = ?, file_name = ?, is_active = 0, updated_at = ? WHERE id = ?',
                    (trash_path, trash_file_name, now, image_id),
                )
            else:
                conn.execute(
                    'UPDATE images SET is_active = 0, updated_at = ? WHERE id = ?',
                    (now, image_id),
                )
        return True, f'已将图片 #{image_id} 移出可发送列表。'

    def restore_image(self, image_id: int, *, restored_path: str, trash_path: str | None = None) -> tuple[bool, str]:
        now = utcnow_str()
        with self._lock, self._connect() as conn:
            image = conn.execute('SELECT id FROM images WHERE id = ?', (image_id,)).fetchone()
            if not image:
                return False, f'图片不存在：{image_id}'

            self._upsert_file_location(
                conn,
                image_id=image_id,
                file_path=restored_path,
                file_name=Path(restored_path).name,
                storage_type=self._infer_storage_type(restored_path),
                now=now,
            )
            conn.execute(
                "UPDATE image_files SET is_active = 0, updated_at = ? WHERE image_id = ? AND storage_type = 'trash'",
                (now, image_id),
            )
            if trash_path:
                conn.execute(
                    'UPDATE image_files SET is_active = 0, updated_at = ? WHERE image_id = ? AND file_path = ?',
                    (now, image_id, trash_path),
                )
            self._sync_image_file_state(conn, image_id, preferred_path=restored_path, now=now)
        return True, f'已恢复图片 #{image_id}。'

    def list_tags(self, *, keyword: str = '', limit: int = 100, character_only: bool | None = None) -> list[sqlite3.Row]:
        sql = """
            SELECT t.id, t.name, t.is_character,
                   COUNT(DISTINCT CASE WHEN it.review_status IN ('approved', 'manual_approved') AND i.is_active = 1 THEN i.id END) AS image_count,
                   COUNT(DISTINCT a.id) AS alias_count
            FROM tags t
            LEFT JOIN tag_aliases a ON a.tag_id = t.id
            LEFT JOIN image_tags it ON it.tag_id = t.id
            LEFT JOIN images i ON i.id = it.image_id
        """
        params: list[Any] = []
        clauses: list[str] = []
        if character_only is not None:
            clauses.append('t.is_character = ?')
            params.append(1 if character_only else 0)
        if keyword:
            clauses.append('t.normalized_name LIKE ?')
            params.append(f"%{normalize_tag_name(keyword)}%")
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' GROUP BY t.id, t.name, t.is_character ORDER BY image_count DESC, t.name ASC LIMIT ?'
        params.append(limit)
        with self._lock, self._connect() as conn:
            return conn.execute(sql, params).fetchall()

    def preview_non_character_tag_cleanup(self, *, limit: int = 200) -> list[sqlite3.Row]:
        return self.list_tags(limit=limit, character_only=False)

    def cleanup_non_character_tags(self) -> dict[str, int]:
        with self._lock, self._connect() as conn:
            rows = conn.execute('SELECT id, normalized_name FROM tags WHERE is_character = 0').fetchall()
            if not rows:
                return {
                    'tags_removed': 0,
                    'image_links_removed': 0,
                    'review_tasks_removed': 0,
                    'aliases_removed': 0,
                    'platform_terms_removed': 0,
                    'subscriptions_removed': 0,
                }
            tag_ids = [int(row['id']) for row in rows]
            normalized_names = [str(row['normalized_name'] or '') for row in rows if str(row['normalized_name'] or '').strip()]
            id_placeholders = ','.join('?' for _ in tag_ids)

            image_cursor = conn.execute(f'DELETE FROM image_tags WHERE tag_id IN ({id_placeholders})', tag_ids)
            review_cursor = conn.execute(f'DELETE FROM review_tasks WHERE tag_id IN ({id_placeholders})', tag_ids)
            alias_cursor = conn.execute(f'DELETE FROM tag_aliases WHERE tag_id IN ({id_placeholders})', tag_ids)
            platform_term_cursor = conn.execute(f'DELETE FROM platform_tag_terms WHERE tag_id IN ({id_placeholders})', tag_ids)

            subscription_sql = f'DELETE FROM crawl_subscriptions WHERE tag_id IN ({id_placeholders})'
            subscription_params: list[Any] = list(tag_ids)
            if normalized_names:
                normalized_placeholders = ','.join('?' for _ in normalized_names)
                subscription_sql += f' OR normalized_tag IN ({normalized_placeholders})'
                subscription_params.extend(normalized_names)
            subscription_cursor = conn.execute(subscription_sql, subscription_params)
            tag_cursor = conn.execute(f'DELETE FROM tags WHERE id IN ({id_placeholders})', tag_ids)

            return {
                'tags_removed': max(0, int(tag_cursor.rowcount or 0)),
                'image_links_removed': max(0, int(image_cursor.rowcount or 0)),
                'review_tasks_removed': max(0, int(review_cursor.rowcount or 0)),
                'aliases_removed': max(0, int(alias_cursor.rowcount or 0)),
                'platform_terms_removed': max(0, int(platform_term_cursor.rowcount or 0)),
                'subscriptions_removed': max(0, int(subscription_cursor.rowcount or 0)),
            }

    def list_tags_for_auto_crawl(self, *, character_only: bool = True) -> list[sqlite3.Row]:
        sql = 'SELECT id, name, is_character FROM tags'
        if character_only:
            sql += ' WHERE is_character = 1'
        sql += ' ORDER BY name ASC'
        with self._lock, self._connect() as conn:
            return conn.execute(sql).fetchall()

    def upsert_crawl_subscription(
        self,
        *,
        platform: str,
        tag_id: int,
        tag_name: str,
        query_text: str = '',
        enabled: bool = True,
    ) -> int:
        normalized_tag = normalize_tag_name(tag_name)
        now = utcnow_str()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT id FROM crawl_subscriptions WHERE platform = ? AND normalized_tag = ? LIMIT 1',
                (platform, normalized_tag),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE crawl_subscriptions
                    SET tag_id = ?, tag_name = ?, query_text = ?, enabled = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (tag_id, tag_name.strip(), query_text, 1 if enabled else 0, now, int(existing['id'])),
                )
                return int(existing['id'])
            cursor = conn.execute(
                """
                INSERT INTO crawl_subscriptions(
                    platform, tag_id, tag_name, normalized_tag, query_text, enabled,
                    last_seen_source_uid, last_checked_at, last_success_at, last_error, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, '', '', '', '', ?, ?)
                """,
                (platform, tag_id, tag_name.strip(), normalized_tag, query_text, 1 if enabled else 0, now, now),
            )
            return int(cursor.lastrowid)

    def disable_missing_crawl_subscriptions(self, *, platform: str, keep_normalized_tags: set[str]) -> None:
        now = utcnow_str()
        with self._lock, self._connect() as conn:
            if keep_normalized_tags:
                placeholders = ','.join('?' for _ in keep_normalized_tags)
                conn.execute(
                    f"UPDATE crawl_subscriptions SET enabled = 0, updated_at = ? WHERE platform = ? AND normalized_tag NOT IN ({placeholders})",
                    (now, platform, *sorted(keep_normalized_tags)),
                )
            else:
                conn.execute(
                    'UPDATE crawl_subscriptions SET enabled = 0, updated_at = ? WHERE platform = ?',
                    (now, platform),
                )

    def list_crawl_subscriptions(self, *, platform: str = '', enabled_only: bool = False, limit: int = 200) -> list[sqlite3.Row]:
        sql = 'SELECT * FROM crawl_subscriptions'
        clauses: list[str] = []
        params: list[Any] = []
        if platform:
            clauses.append('platform = ?')
            params.append(platform)
        if enabled_only:
            clauses.append('enabled = 1')
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY platform ASC, tag_name ASC LIMIT ?'
        params.append(limit)
        with self._lock, self._connect() as conn:
            return conn.execute(sql, params).fetchall()

    def update_crawl_subscription_state(
        self,
        subscription_id: int,
        *,
        query_text: str | None = None,
        enabled: bool | None = None,
        last_seen_source_uid: str | None = None,
        last_checked_at: str | None = None,
        last_success_at: str | None = None,
        last_error: str | None = None,
    ) -> None:
        fields: list[str] = ['updated_at = ?']
        params: list[Any] = [utcnow_str()]
        if query_text is not None:
            fields.append('query_text = ?')
            params.append(query_text)
        if enabled is not None:
            fields.append('enabled = ?')
            params.append(1 if enabled else 0)
        if last_seen_source_uid is not None:
            fields.append('last_seen_source_uid = ?')
            params.append(last_seen_source_uid)
        if last_checked_at is not None:
            fields.append('last_checked_at = ?')
            params.append(last_checked_at)
        if last_success_at is not None:
            fields.append('last_success_at = ?')
            params.append(last_success_at)
        if last_error is not None:
            fields.append('last_error = ?')
            params.append(last_error)
        params.append(subscription_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE crawl_subscriptions SET {', '.join(fields)} WHERE id = ?", params)
