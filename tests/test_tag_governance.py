from __future__ import annotations

import importlib
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path


CORE_DIR = Path(__file__).resolve().parents[1] / "core"
PACKAGE_NAME = "pjsk_pic_tag_governance_test_core"


class _FakeLogger:
    def info(self, *_args, **_kwargs) -> None:
        pass

    def warning(self, *_args, **_kwargs) -> None:
        pass

    def error(self, *_args, **_kwargs) -> None:
        pass


try:
    import astrbot.api  # noqa: F401
except ModuleNotFoundError:
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_module.logger = _FakeLogger()
    astrbot_module.api = astrbot_api_module
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = astrbot_api_module

astrbot_api_module = sys.modules["astrbot.api"]
if not hasattr(astrbot_api_module, "logger"):
    astrbot_api_module.logger = _FakeLogger()

message_components_module = sys.modules.get("astrbot.api.message_components")
if message_components_module is None:
    message_components_module = types.ModuleType("astrbot.api.message_components")
    sys.modules["astrbot.api.message_components"] = message_components_module


class _Image:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    async def convert_to_file_path(self) -> str:
        return str(self.path)


class _Reply:
    def __init__(self, chain=None) -> None:
        self.chain = chain or []


message_components_module.Image = _Image
message_components_module.Reply = _Reply

if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(CORE_DIR)]
    sys.modules[PACKAGE_NAME] = package

ImageIndexDB = importlib.import_module(f"{PACKAGE_NAME}.db").ImageIndexDB
SubmissionService = importlib.import_module(f"{PACKAGE_NAME}.submission_service").SubmissionService
TagGovernanceService = importlib.import_module(
    f"{PACKAGE_NAME}.tag_governance_service"
).TagGovernanceService
TagIdentityService = importlib.import_module(f"{PACKAGE_NAME}.tag_identity_service").TagIdentityService


class _FakeEvent:
    def __init__(self, image_path: Path) -> None:
        self.message_obj = types.SimpleNamespace(message=[_Image(image_path)], message_id="message-1")
        self.unified_msg_origin = "group:1"

    @staticmethod
    def get_sender_id() -> str:
        return "10001"

    @staticmethod
    def get_sender_name() -> str:
        return "tester"

    @staticmethod
    def get_platform_name() -> str:
        return "aiocqhttp"


class _FakeImporter:
    def __init__(self, db: ImageIndexDB, *, fail_if_called: bool = False) -> None:
        self.db = db
        self.fail_if_called = fail_if_called
        self.calls = 0

    async def import_local_file(self, image_path: Path, *, platform: str):
        self.calls += 1
        if self.fail_if_called:
            raise AssertionError("unknown tag must not import its image")
        image_id = self.db.upsert_image(
            file_path=str(image_path),
            file_name=image_path.name,
            sha256=f"submission-{self.calls}",
            width=32,
            height=32,
            format_="jpeg",
        )
        return types.SimpleNamespace(
            image_id=image_id,
            file_path=image_path,
            sha256=f"submission-{self.calls}",
            similar_image_ids=[],
        )


class TagGovernanceMigrationTests(unittest.TestCase):
    def test_legacy_character_flag_is_preserved_during_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "legacy.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE tags (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        normalized_name TEXT NOT NULL UNIQUE,
                        is_character INTEGER DEFAULT 0,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO tags(name, normalized_name, is_character, created_at) VALUES(?, ?, 1, ?)",
                    ("初音未来", "初音未来", "2026-01-01T00:00:00+00:00"),
                )
                conn.execute(
                    "INSERT INTO tags(name, normalized_name, is_character, created_at) VALUES(?, ?, 0, ?)",
                    ("插画", "插画", "2026-01-01T00:00:00+00:00"),
                )
                conn.commit()
            finally:
                conn.close()

            db = ImageIndexDB(db_path)
            character = db.get_tag_row("初音未来")
            other = db.get_tag_row("插画")
            self.assertEqual("character", character["tag_type"])
            self.assertEqual(1, int(character["is_character"]))
            self.assertEqual("other", other["tag_type"])
            self.assertEqual("active", character["status"])


class TagGovernanceDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = ImageIndexDB(self.root / "image_index.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _image(self, name: str) -> int:
        path = self.root / f"{name}.jpg"
        path.write_bytes(name.encode("utf-8"))
        return self.db.upsert_image(
            file_path=str(path),
            file_name=path.name,
            sha256=f"sha-{name}",
            width=32,
            height=32,
            format_="jpeg",
        )

    def test_proposal_lifecycle_and_alias_merge(self) -> None:
        target_id = self.db.get_or_create_tag("初音未来", tag_type="character")
        first = self.db.create_or_increment_tag_proposal(
            "初音ミク",
            aliases=["miku"],
            submitter_id="1",
        )
        second = self.db.create_or_increment_tag_proposal(
            "初音ミク",
            aliases=["Miku"],
            submitter_id="2",
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(2, second["occurrence_count"])

        ok, merged = self.db.merge_tag_proposal(first["id"], "初音未来")
        self.assertTrue(ok, merged)
        self.assertEqual(target_id, merged["tag_id"])
        self.assertEqual("初音未来", self.db.resolve_tag("初音ミク", allow_fuzzy=False).tag_name)

        approved = self.db.create_or_increment_tag_proposal("世界计划", submitter_id="3")
        ok, result = self.db.approve_tag_proposal(approved["id"], "主题")
        self.assertTrue(ok, result)
        row = self.db.get_tag_row("世界计划")
        self.assertEqual("theme", row["tag_type"])
        self.assertEqual("active", row["status"])

        rejected = self.db.create_or_increment_tag_proposal("随便", submitter_id="4")
        ok, _ = self.db.reject_tag_proposal(rejected["id"], "不是图库词条")
        self.assertTrue(ok)
        self.assertEqual("rejected", self.db.get_tag_proposal(rejected["id"])["status"])

    def test_cleanup_removes_only_unprotected_other_tags(self) -> None:
        protected_tag_id = self.db.get_or_create_tag("望月穗波", tag_type="other")
        protected_image_id = self._image("approved")
        self.db.link_image_tag(
            protected_image_id,
            protected_tag_id,
            source_type="legacy:pixiv",
            review_status="manual_approved",
        )

        safe_tag_id = self.db.get_or_create_tag("100users入り", tag_type="other")
        safe_image_id = self._image("rejected")
        self.db.link_image_tag(
            safe_image_id,
            safe_tag_id,
            source_type="legacy:pixiv",
            review_status="manual_rejected",
        )
        self.db.create_review_task(safe_image_id, safe_tag_id, "manual_rejected", reason="test")

        alias_tag_id = self.db.get_or_create_tag("主题词", tag_type="other")
        self.assertTrue(self.db.add_alias("主题词", "专题")[0])

        pending_tag_id = self.db.get_or_create_tag("待确认词", tag_type="other")
        pending_image_id = self._image("pending")
        self.db.link_image_tag(
            pending_image_id,
            pending_tag_id,
            source_type="legacy:pixiv",
            review_status="pending",
        )

        preview_names = {str(row["name"]) for row in self.db.preview_non_character_tag_cleanup()}
        self.assertIn("100users入り", preview_names)
        self.assertNotIn("望月穗波", preview_names)
        self.assertNotIn("主题词", preview_names)
        self.assertNotIn("待确认词", preview_names)

        summary = self.db.cleanup_non_character_tags()
        self.assertEqual(1, summary["tags_removed"])
        self.assertIsNone(self.db.get_tag_row("100users入り"))
        self.assertIsNotNone(self.db.get_tag_row("望月穗波"))
        self.assertIsNotNone(self.db.get_tag_row("主题词"))
        self.assertIsNotNone(self.db.get_tag_row("待确认词"))
        self.assertEqual(1, self.db.count_images_for_tag("望月穗波"))
        self.assertGreaterEqual(summary["protected_tags"], 3)
        self.assertEqual(alias_tag_id, int(self.db.get_tag_row("主题词")["id"]))

    def test_auto_crawl_and_identity_inputs_require_active_character(self) -> None:
        active_id = self.db.get_or_create_tag("初音未来", tag_type="character", status="active")
        self.db.get_or_create_tag("镜音铃", tag_type="character", status="pending")
        self.db.get_or_create_tag("镜音双子", tag_type="pairing", status="active")
        self.db.get_or_create_tag("巡音流歌", tag_type="character", status="archived")

        crawl_ids = {int(row["id"]) for row in self.db.list_tags_for_auto_crawl(character_only=True)}
        identity_ids = {int(row["id"]) for row in self.db.list_tag_identity_scan_inputs()}
        self.assertEqual({active_id}, crawl_ids)
        self.assertEqual({active_id}, identity_ids)

    def test_report_flags_broad_alias_without_mutation(self) -> None:
        self.db.get_or_create_tag("初音未来", tag_type="character")
        self.assertTrue(self.db.add_alias("初音未来", "画")[0])
        before = self.db.get_tag_governance_snapshot()["totals"]
        report = TagGovernanceService(self.db).build_report()
        after = self.db.get_tag_governance_snapshot()["totals"]
        self.assertEqual(1, report["totals"]["broad_aliases"])
        self.assertEqual(before, after)

    def test_historical_pixiv_cooccurrence_is_not_identity_evidence(self) -> None:
        source_id = self.db.get_or_create_tag("日野森志步", tag_type="character")
        target_id = self.db.get_or_create_tag("日野森雫", tag_type="character")
        image_id = self._image("cooccurrence")
        self.db.link_image_tag(
            image_id,
            target_id,
            source_type="crawl:pixiv",
            review_status="manual_approved",
        )
        self.db.upsert_source(
            image_id,
            "pixiv",
            "https://www.pixiv.net/artworks/123",
            "https://i.pximg.net/123.jpg",
            raw_tags=["日野森志步"],
            extra_json={"translated_tags": []},
        )
        suggestions = self.db.suggest_platform_terms_for_tag(tag_name="日野森雫", platform="pixiv")
        self.assertIn("日野森志步", {str(item["term"]) for item in suggestions})
        candidates = self.db.list_tag_merge_candidates(limit=20)
        self.assertNotIn(
            (source_id, target_id),
            {(int(item["source_tag_id"]), int(item["target_tag_id"])) for item in candidates},
        )


class SubmissionAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = ImageIndexDB(self.root / "image_index.db")
        self.image_path = self.root / "submission.jpg"
        self.image_path.write_bytes(b"submission")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_unknown_tag_creates_proposal_without_importing_image(self) -> None:
        importer = _FakeImporter(self.db, fail_if_called=True)
        service = SubmissionService(self.db, importer, object())
        result = await service.submit_from_event(
            _FakeEvent(self.image_path),
            "手毬",
            aliases=["花海咲季"],
            review_enabled=False,
        )
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.proposal_id)
        self.assertEqual(0, importer.calls)
        self.assertIsNone(self.db.get_tag_row("手毬"))
        proposal = self.db.get_tag_proposal(result.proposal_id)
        self.assertEqual("pending", proposal["status"])
        self.assertEqual(["花海咲季"], proposal["aliases"])

    async def test_known_alias_can_submit_to_active_canonical_tag(self) -> None:
        self.db.get_or_create_tag("初音未来", tag_type="character", status="active")
        self.assertTrue(self.db.add_alias("初音未来", "miku")[0])
        importer = _FakeImporter(self.db)
        service = SubmissionService(self.db, importer, object())
        result = await service.submit_from_event(
            _FakeEvent(self.image_path),
            "miku",
            review_enabled=False,
        )
        self.assertTrue(result.ok, result.reply_message)
        self.assertTrue(result.resolved_from_alias)
        self.assertEqual("初音未来", result.tag_name)
        self.assertEqual(1, importer.calls)
        self.assertEqual(1, self.db.count_images_for_tag("初音未来"))


class TagIdentityPolicyTests(unittest.TestCase):
    def test_moderate_name_overlap_without_curated_terms_is_not_a_candidate(self) -> None:
        service = TagIdentityService(None, None, {})
        left = {
            "id": 1,
            "name": "日野森志步",
            "image_count": 10,
            "aliases": [],
            "platform_terms": [],
            "history_terms": [{"term": "日野森雫", "count": 100}],
        }
        right = {
            "id": 2,
            "name": "日野森雫",
            "image_count": 8,
            "aliases": [],
            "platform_terms": [],
            "history_terms": [{"term": "日野森志步", "count": 100}],
        }
        self.assertIsNone(service._evaluate_pair(left, right))


if __name__ == "__main__":
    unittest.main()
