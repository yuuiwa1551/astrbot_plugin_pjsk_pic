from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path


CORE_DIR = Path(__file__).resolve().parents[1] / "core"
PACKAGE = "pjsk_intake_test_core"


class FakeImage:
    def __init__(self, file: str | None, *, url: str = "", **_) -> None:
        self.url = url
        self.file = file or ""

    async def convert_to_file_path(self) -> str:
        return self.file or self.url


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


install_stubs()
pkg = types.ModuleType(PACKAGE)
pkg.__path__ = [str(CORE_DIR)]
sys.modules[PACKAGE] = pkg
message_images = importlib.import_module(f"{PACKAGE}.message_images")
db_module = importlib.import_module(f"{PACKAGE}.db")
models = importlib.import_module(f"{PACKAGE}.models")
intake_module = importlib.import_module(f"{PACKAGE}.chat_image_intake")


class MessageObj:
    def __init__(self, message_id, raw_message=None):
        self.message_id = message_id
        self.raw_message = raw_message


class FakeBot:
    def __init__(self, payload):
        self.payload = payload

    async def call_action(self, name, **params):
        return self.payload


class Event:
    def __init__(self, components, *, message_id="m1", session="aiocqhttp:GroupMessage:1",
                 sender_id="u1", sender_name="Alice", group_id="1",
                 platform="aiocqhttp", self_id="bot", bot=None):
        self.unified_msg_origin = session
        self.message_obj = MessageObj(message_id, {"message": components})
        self._components = components
        self._extras = {}
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._group_id = group_id
        self._platform = platform
        self._self_id = self_id
        self.bot = bot

    def get_messages(self):
        return self._components

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_group_id(self):
        return self._group_id

    def get_platform_name(self):
        return self._platform

    def get_self_id(self):
        return self._self_id


class FakeImporter:
    def __init__(self, entries=None, default=None):
        self.entries = dict(entries or {})
        self.default = default
        self.calls: list[str] = []

    def _resolve(self, key):
        if key in self.entries:
            return self.entries[key]
        if self.default is None:
            raise AssertionError(f"unexpected import: {key}")
        return self.default

    async def import_local_file(self, source_path, *, platform="submission"):
        key = Path(source_path).name
        self.calls.append(key)
        image_id, sha = self._resolve(key)
        return models.ImportedImage(
            image_id=image_id, file_path=Path(source_path), sha256=sha,
            phash="", width=10, height=10, format="png",
        )

    async def import_candidate(self, candidate):
        key = candidate.image_url
        self.calls.append(key)
        image_id, sha = self._resolve(key)
        return models.ImportedImage(
            image_id=image_id, file_path=Path(f"/stored/{key}"), sha256=sha,
            phash="", width=10, height=10, format="png",
        )


class NoopChatContext:
    def start_prefetch_items(self, items):
        return None


async def settle(service):
    while service._tasks:
        await asyncio.gather(*list(service._tasks), return_exceptions=True)


class ChatImageIntakeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = db_module.ImageIndexDB(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def make_service(self, importer):
        return intake_module.ChatImageIntakeService(self.db, importer, NoopChatContext())

    async def test_direct_image_becomes_captured_candidate(self):
        event = Event([{"type": "image", "data": {"file": "/tmp/a.png"}}])
        importer = FakeImporter({"a.png": (7, "sha-a")})
        service = self.make_service(importer)
        items = message_images.direct_message_images(event)
        event.set_extra("pjsk_gallery_image_sources", items)
        await service.enroll(event, items)
        await settle(service)
        candidate = self.db.get_chat_image_candidate_by_ref(items[0].ref)
        self.assertEqual("captured", candidate["status"])
        self.assertEqual("sha-a", candidate["content_sha256"])
        self.assertTrue(str(candidate["file_path"]).endswith("a.png"))
        self.assertEqual("1", str(candidate["group_id"]))
        self.assertEqual("aiocqhttp", candidate["platform"])
        self.assertEqual("u1", candidate["sender_id"])
        self.assertEqual("Alice", candidate["sender_name"])

    async def test_same_content_marks_later_candidate_duplicate(self):
        importer = FakeImporter(default=(3, "sha-dup"))
        service = self.make_service(importer)
        first = Event([{"type": "image", "data": {"file": "/tmp/a.png"}}], message_id="m1")
        second = Event([{"type": "image", "data": {"file": "/tmp/b.png"}}], message_id="m2")
        first_items = message_images.direct_message_images(first)
        second_items = message_images.direct_message_images(second)
        await service.enroll(first, first_items)
        await settle(service)
        await service.enroll(second, second_items)
        await settle(service)
        c1 = self.db.get_chat_image_candidate_by_ref(first_items[0].ref)
        c2 = self.db.get_chat_image_candidate_by_ref(second_items[0].ref)
        self.assertEqual("captured", c1["status"])
        self.assertEqual("duplicate", c2["status"])
        self.assertEqual(int(c1["id"]), int(c2["dedupe_of"]))

    async def test_library_image_appends_source_without_audit(self):
        image_id = self.db.upsert_image(
            file_path="/lib/x.png", file_name="x.png", sha256="sha-lib",
            phash="", width=10, height=10, format_="png",
        )
        tag_id = self.db.get_or_create_tag("东云彰人", tag_type="character")
        self.db.link_image_tag(image_id, tag_id, review_status="approved")
        event = Event([{"type": "image", "data": {"file": "/tmp/lib.png"}}])
        importer = FakeImporter({"lib.png": (image_id, "sha-lib")})
        service = self.make_service(importer)
        items = message_images.direct_message_images(event)
        await service.enroll(event, items)
        await settle(service)
        candidate = self.db.get_chat_image_candidate_by_ref(items[0].ref)
        self.assertEqual("duplicate", candidate["status"])
        self.assertEqual(0, int(candidate["dedupe_of"]))
        detail = self.db.get_image_detail(image_id, sync_files=False)
        self.assertTrue(any(source["platform"] == "chat" for source in detail["sources"]))

    async def test_platform_emoji_is_not_enrolled(self):
        event = Event([{"type": "image", "data": {"file": "/tmp/e.png", "sub_type": 1}}])
        importer = FakeImporter(default=(1, "sha-e"))
        service = self.make_service(importer)
        items = message_images.direct_message_images(event)
        enrolled = await service.enroll(event, items)
        await settle(service)
        self.assertEqual([], enrolled)
        self.assertEqual([], self.db.list_chat_image_candidates(limit=10))
        self.assertEqual([], importer.calls)

    async def test_emoji_summary_is_not_enrolled(self):
        event = Event([{"type": "image", "data": {"file": "/tmp/e2.png", "summary": "[动画表情]"}}])
        importer = FakeImporter(default=(1, "sha-e2"))
        service = self.make_service(importer)
        items = message_images.direct_message_images(event)
        enrolled = await service.enroll(event, items)
        self.assertEqual([], enrolled)
        self.assertEqual([], importer.calls)

    async def test_quoted_image_is_collected(self):
        event = Event(
            [{"type": "reply", "data": {"id": "99"}}],
            message_id="m9",
            bot=FakeBot({
                "message": [{"type": "image", "data": {"file": "/tmp/q.png"}}],
                "sender": {"user_id": "u2", "nickname": "Bob"},
            }),
        )
        importer = FakeImporter({"q.png": (11, "sha-q")})
        service = self.make_service(importer)
        service.schedule_quoted(event)
        await settle(service)
        rows = self.db.list_chat_image_candidates(limit=10)
        self.assertEqual(1, len(rows))
        self.assertEqual("99", rows[0]["source_message_id"])
        self.assertEqual("u2", rows[0]["sender_id"])
        self.assertEqual("Bob", rows[0]["sender_name"])

    async def test_self_sent_direct_image_is_ignored(self):
        event = Event(
            [{"type": "image", "data": {"file": "/tmp/me.png"}}],
            sender_id="bot",
        )
        importer = FakeImporter(default=(1, "sha-me"))
        service = self.make_service(importer)
        items = message_images.direct_message_images(event)
        enrolled = await service.enroll(event, items)
        self.assertEqual([], enrolled)
        self.assertEqual([], importer.calls)


if __name__ == "__main__":
    unittest.main()
