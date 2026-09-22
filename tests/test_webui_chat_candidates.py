from __future__ import annotations

import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


CORE_DIR = Path(__file__).resolve().parents[1] / "core"
PACKAGE = "pjsk_webui_test_core"

SESSION = "aiocqhttp:GroupMessage:1"


def install_stubs() -> None:
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None, debug=lambda *a, **k: None,
    )
    components = types.ModuleType("astrbot.api.message_components")
    components.Image = FakeImage
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.message_components": components,
    })
    astrbot.api = api


class FakeImage:
    def __init__(self, file: str | None, *, url: str = "", **_) -> None:
        self.url = url
        self.file = file or ""

    async def convert_to_file_path(self) -> str:
        return self.file or self.url


install_stubs()
pkg = types.ModuleType(PACKAGE)
pkg.__path__ = [str(CORE_DIR)]
sys.modules[PACKAGE] = pkg
db_module = importlib.import_module(f"{PACKAGE}.db")
confirm_module = importlib.import_module(f"{PACKAGE}.chat_image_confirm_service")
webui_module = importlib.import_module(f"{PACKAGE}.webui")


class FakeRequest:
    def __init__(self, *, query=None, body=None):
        self.query = dict(query or {})
        self._body = body or {}
        self.cookies = {}
        self.headers = {}

    async def json(self):
        return self._body


class FakeImporter:
    def __init__(self, image_id):
        self.image_id = image_id
        self.calls: list[str] = []

    async def import_local_file(self, source_path, *, platform="submission"):
        self.calls.append(str(source_path))
        return types.SimpleNamespace(image_id=self.image_id, is_new=True)


class FakeContext:
    def __init__(self):
        self.platform_manager = types.SimpleNamespace(platform_insts=[])


class WebuiChatCandidateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = db_module.ImageIndexDB(Path(self.tmp.name) / "test.db")
        self.tag_akito = self.db.get_or_create_tag("东云彰人", tag_type="character")
        self.image_file = Path(self.tmp.name) / "a.png"
        self.image_file.write_bytes(b"png")
        self.image_id = self.db.upsert_image(
            file_path=str(self.image_file), file_name="a.png", sha256="sha-a",
            phash="", width=10, height=10, format_="png",
        )
        self.importer = FakeImporter(self.image_id)
        self.service = confirm_module.ChatImageConfirmService(
            db=self.db,
            context=FakeContext(),
            config={"chat_image_collection_enabled": True},
            importer=self.importer,
            candidate_tags_provider=lambda: [
                {"tag_id": self.tag_akito, "name": "东云彰人",
                 "standard_name": "东云彰人", "tag_type": "character"},
            ],
        )
        self.webui = webui_module.GalleryWebUI(
            self.db,
            types.SimpleNamespace(config={}),
            pixiv_backfill_service=object(),
            pixiv_client=object(),
            config={},
            chat_image_service=self.service,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def make_pending_candidate(self):
        candidate = self.db.create_chat_image_candidate(
            ref="ref-a", session_id=SESSION, group_id="1", platform="aiocqhttp",
            sender_id="u1", sender_name="Alice", source_message_id="m1",
            image_index=1, image_url="https://example.com/a.png",
            file_path=str(self.image_file),
        )
        self.db.complete_chat_image_candidate_audit(
            int(candidate["id"]), status="audited", decision="approve",
            provider="provider-1", prompt_version="chat-v1",
            quality={"overall": 90}, flags=[], reason="ok",
            proposed_tag_ids=[self.tag_akito],
        )
        self.db.mark_chat_image_candidates_asked(
            [int(candidate["id"])], confirm_message_id="555",
            expires_at="2000-01-01T00:00:00+00:00",
        )
        self.db.expire_asked_chat_image_candidates("2999-01-01T00:00:00+00:00")
        return self.db.get_chat_image_candidate(int(candidate["id"]))

    async def test_list_returns_pending_webui_candidates(self):
        candidate = self.make_pending_candidate()
        response = await self.webui.api_chat_candidates(FakeRequest())
        payload = json.loads(response.text)
        self.assertEqual(200, response.status)
        self.assertEqual(1, len(payload["items"]))
        item = payload["items"][0]
        self.assertEqual(int(candidate["id"]), item["id"])
        self.assertEqual("pending_webui", item["status"])
        self.assertTrue(item["has_file"])
        self.assertEqual(
            [{"id": self.tag_akito, "name": "东云彰人", "tag_type": "character"}],
            item["tags"],
        )
        self.assertEqual(1, payload["stats"]["pending_webui"])

    async def test_decision_approve_writes_image(self):
        candidate = self.make_pending_candidate()
        response = await self.webui.api_chat_candidate_decision(
            FakeRequest(body={"ids": [int(candidate["id"])], "decision": "approve"}))
        payload = json.loads(response.text)
        self.assertTrue(payload["ok"])
        self.assertIn("已入库 1 张", payload["message"])
        row = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("approved_written", row["status"])
        self.assertEqual("webui", row["confirm_user_id"])
        detail = self.db.get_image_detail(self.image_id, sync_files=False)
        self.assertIn("东云彰人", {tag["name"] for tag in detail["tags"]})

    async def test_decision_reject_marks_rejected(self):
        candidate = self.make_pending_candidate()
        response = await self.webui.api_chat_candidate_decision(
            FakeRequest(body={"ids": [int(candidate["id"])], "decision": "reject"}))
        payload = json.loads(response.text)
        self.assertTrue(payload["ok"])
        self.assertIn("已忽略 1 张", payload["message"])
        row = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("rejected", row["status"])
        self.assertEqual([], self.importer.calls)

    async def test_decision_requires_ids_and_valid_decision(self):
        response = await self.webui.api_chat_candidate_decision(
            FakeRequest(body={"decision": "approve"}))
        self.assertEqual(400, response.status)
        response = await self.webui.api_chat_candidate_decision(
            FakeRequest(body={"ids": [1], "decision": "maybe"}))
        self.assertEqual(400, response.status)

    async def test_candidate_file_serves_local_file(self):
        candidate = self.make_pending_candidate()
        response = await self.webui.api_chat_candidate_file(
            FakeRequest(query={"candidate_id": str(candidate["id"])}))
        self.assertEqual(200, response.status)

    def seed_gallery_chat_image(self):
        self.db.link_image_tag(
            self.image_id, self.tag_akito,
            source_type="chat_auto_collection", review_status="approved",
            review_reason="群聊自动收图",
        )
        self.db.upsert_source(
            self.image_id, "chat", "chat://m9", "https://example.com/a.png",
            "Bob", ["东云彰人"],
            {
                "source_kind": "chat_auto_collection",
                "session_id": SESSION,
                "source_message_id": "m9",
                "source_sender_id": "u9",
                "source_sender_name": "Bob",
                "image_index": 1,
            },
        )
        return f"backfill:chat_auto:{self.image_id}"

    async def test_backfill_creates_pending_candidate_once(self):
        ref = self.seed_gallery_chat_image()
        result = self.db.backfill_pending_webui_chat_candidates()
        self.assertEqual({"inserted": 1, "skipped": 0}, result)
        candidate = self.db.get_chat_image_candidate_by_ref(ref)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual("pending_webui", candidate["status"])
        self.assertEqual(self.image_id, int(candidate["image_id"]))
        self.assertEqual(str(self.image_file), candidate["file_path"])
        self.assertEqual([self.tag_akito], candidate["proposed_tag_ids_json"])
        self.assertEqual("backfill", candidate["audit_provider"])
        self.assertEqual("aiocqhttp", candidate["platform"])
        self.assertEqual("1", candidate["group_id"])
        self.assertEqual("Bob", candidate["sender_name"])
        self.assertEqual("m9", candidate["source_message_id"])
        self.assertEqual("chat://m9", candidate["image_url"])
        self.assertEqual({"inserted": 0, "skipped": 1},
                         self.db.backfill_pending_webui_chat_candidates())

    async def test_backfill_approve_keeps_gallery_image(self):
        ref = self.seed_gallery_chat_image()
        self.db.backfill_pending_webui_chat_candidates()
        candidate = self.db.get_chat_image_candidate_by_ref(ref)
        assert candidate is not None
        result = await self.service.approve_candidates([int(candidate["id"])])
        self.assertEqual({"approved": 1, "failed": 0}, result)
        self.assertEqual([], self.importer.calls)
        row = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("approved_written", row["status"])
        self.assertEqual(self.image_id, int(row["image_id"]))
        detail = self.db.get_image_detail(self.image_id, sync_files=False)
        self.assertIn("approved", {tag["review_status"] for tag in detail["tags"]})

    async def test_backfill_reject_marks_chat_links_manual_rejected(self):
        ref = self.seed_gallery_chat_image()
        self.db.backfill_pending_webui_chat_candidates()
        candidate = self.db.get_chat_image_candidate_by_ref(ref)
        assert candidate is not None
        rejected = self.service.reject_candidates([int(candidate["id"])])
        self.assertEqual(1, rejected)
        row = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("rejected", row["status"])
        detail = self.db.get_image_detail(self.image_id, sync_files=False)
        chat_tags = [
            tag for tag in detail["tags"] if tag["source_type"] == "chat_auto_collection"
        ]
        self.assertEqual(["manual_rejected"], [tag["review_status"] for tag in chat_tags])
        self.assertEqual([], self.importer.calls)


if __name__ == "__main__":
    unittest.main()
