from __future__ import annotations

import importlib
import asyncio
import base64
import sys
import tempfile
import types
import unittest
from pathlib import Path
from PIL import Image as PillowImage


CORE_DIR = Path(__file__).resolve().parents[1] / "core"
PACKAGE = "pjsk_confirm_test_core"


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
db_module = importlib.import_module(f"{PACKAGE}.db")
models = importlib.import_module(f"{PACKAGE}.models")
confirm_module = importlib.import_module(f"{PACKAGE}.chat_image_confirm_service")

SESSION = "aiocqhttp:GroupMessage:1"


class MessageObj:
    def __init__(self, message_id, raw_message=None):
        self.message_id = message_id
        self.raw_message = raw_message


class FakeBot:
    def __init__(self, message_id=555, fail=False, get_msg=None):
        self.sent: list[tuple[int, list]] = []
        self.message_id = message_id
        self.fail = fail
        self.get_msg = get_msg

    async def send_group_msg(self, group_id, message):
        self.sent.append((group_id, message))
        if self.fail:
            raise RuntimeError("send failed")
        return {"status": "ok", "data": {"message_id": self.message_id}}

    async def call_action(self, name, **params):
        if name == "get_msg" and self.get_msg is not None:
            return self.get_msg
        raise AssertionError(f"unexpected action: {name}")


class FakePlatform:
    def __init__(self, platform_id, bot):
        self.platform_id = platform_id
        self.bot = bot

    def meta(self):
        return types.SimpleNamespace(id=self.platform_id)


class FakeContext:
    def __init__(self, bot=None, platform_id="aiocqhttp"):
        insts = [FakePlatform(platform_id, bot)] if bot is not None else []
        self.platform_manager = types.SimpleNamespace(platform_insts=insts)


class Event:
    def __init__(self, components, *, message_str="", message_id="m1", session=SESSION,
                 sender_id="u1", sender_name="Alice", group_id="1",
                 platform="aiocqhttp", self_id="bot", bot=None, private=False):
        self.unified_msg_origin = session
        self.message_obj = MessageObj(message_id, {"message": components})
        self._components = components
        self.message_str = message_str
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._group_id = group_id
        self._platform = platform
        self._self_id = self_id
        self._private = private
        self.bot = bot

    def get_messages(self):
        return self._components

    def is_private_chat(self):
        return self._private

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
        entry = self._resolve(key)
        image_id, sha = entry[:2]
        return models.ImportedImage(
            image_id=image_id, file_path=Path(source_path), sha256=sha,
            phash="", width=10, height=10, format="png",
            is_new=entry[2] if len(entry) > 2 else True,
        )


class ParseCorrectionTests(unittest.TestCase):
    def test_parses_digits(self):
        self.assertEqual(
            (2, ["东云彰人", "青柳冬弥"]),
            confirm_module.parse_correction("第2张是东云彰人、青柳冬弥"),
        )

    def test_parses_chinese_number_and_separators(self):
        self.assertEqual(
            (3, ["白石杏", "小豆泽心羽"]),
            confirm_module.parse_correction("第三张：白石杏，小豆泽心羽"),
        )

    def test_parses_fullwidth_digits_and_alt_verb(self):
        self.assertEqual(
            (2, ["东云彰人"]),
            confirm_module.parse_correction("第２张改成东云彰人"),
        )

    def test_rejects_non_correction(self):
        self.assertIsNone(confirm_module.parse_correction("确认"))
        self.assertIsNone(confirm_module.parse_correction("第2张"))
        self.assertTrue(confirm_module.looks_like_correction("第2张？"))
        self.assertFalse(confirm_module.looks_like_correction("收"))


class ChatImageConfirmTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiselect_skip_and_numbering(self):
        ids = [self.make_image(name, 'sha-' + name) for name in ('a.png', 'b.png', 'c.png')]
        self.importer = FakeImporter({name: (image_id, 'sha-' + name) for name, image_id in zip(('a.png', 'b.png', 'c.png'), ids)})
        bot = FakeBot()
        service = self.make_service(bot)
        rows = [self.make_audited(name) for name in ('a.png', 'b.png', 'c.png')]
        await service.run_once()
        async def reply(text):
            return await service.handle_reply(Event([{'type': 'reply', 'data': {'id': '555'}}], message_str=text, bot=bot))
        self.assertTrue(await reply('收1、9'))
        self.assertEqual([], self.importer.calls)
        self.assertTrue(await reply('收1、3、1'))
        self.assertEqual(['a.png', 'c.png'], self.importer.calls)
        receipt = bot.sent[-1][1][-1]['data']['text']
        self.assertIn(f'第3张 → 图片 ID：#{ids[2]}', receipt)
        self.assertIn('还有 1 张', receipt)
        self.assertTrue(await reply('跳过2'))
        self.assertEqual('rejected', self.db.get_chat_image_candidate(rows[1]['id'])['status'])
        self.assertEqual(['a.png', 'c.png'], self.importer.calls)

    async def test_this_image_correction_and_ambiguous_batch(self):
        image_id = self.make_image('a.png', 'sha-a')
        self.importer = FakeImporter({'a.png': (image_id, 'sha-a')})
        bot = FakeBot()
        service = self.make_service(bot)
        row = self.make_audited('a.png')
        await service.run_once()
        event = Event([{'type': 'reply', 'data': {'id': '555'}}], message_str='这张是白石杏和小豆泽心羽，收了', bot=bot)
        self.assertTrue(await service.handle_reply(event))
        self.assertEqual([self.tag_an, self.tag_kohane], self.db.get_chat_image_candidate(row['id'])['confirmed_tag_ids_json'])
        bot.message_id = 556
        for name in ('b.png', 'c.png'):
            self.make_audited(name)
        await service.run_once()
        event = Event([{'type': 'reply', 'data': {'id': '556'}}], message_str='这张是白石杏，收了', bot=bot)
        self.assertTrue(await service.handle_reply(event))
        self.assertEqual(['a.png'], self.importer.calls)
        self.assertIn('明确编号', bot.sent[-1][1][-1]['data']['text'])

    async def test_collect_all_alias_with_custom_accept_words(self):
        image_id = self.make_image('a.png', 'sha-a')
        self.importer = FakeImporter({'a.png': (image_id, 'sha-a')})
        bot = FakeBot()
        service = self.make_service(bot, config={'chat_image_confirm_accept_words': '确定'})
        self.make_audited('a.png')
        await service.run_once()
        self.assertTrue(await service.handle_reply(Event([{'type': 'reply', 'data': {'id': '555'}}], message_str='收全部', bot=bot)))
        self.assertEqual(['a.png'], self.importer.calls)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = db_module.ImageIndexDB(Path(self.tmp.name) / "test.db")
        self.tag_akito = self.db.get_or_create_tag("东云彰人", tag_type="character")
        self.tag_toya = self.db.get_or_create_tag("青柳冬弥", tag_type="character")
        self.tag_an = self.db.get_or_create_tag("白石杏", tag_type="character")
        self.tag_kohane = self.db.get_or_create_tag("小豆泽心羽", tag_type="character")
        self.tag_vbs = self.db.get_or_create_tag("Vivid BAD SQUAD", tag_type="theme")
        self.tag_pair = self.db.get_or_create_tag("杏豆", tag_type="pairing")
        self.candidates = [
            {"tag_id": self.tag_akito, "name": "东云彰人", "standard_name": "东云彰人",
             "name_en": "Akito Shinonome", "name_ja": "東雲彰人", "tag_type": "character"},
            {"tag_id": self.tag_toya, "name": "青柳冬弥", "standard_name": "青柳冬弥",
             "name_en": "Toya Aoyagi", "name_ja": "青柳冬弥", "tag_type": "character"},
            {"tag_id": self.tag_an, "name": "白石杏", "standard_name": "白石杏",
             "name_en": "An Shiraishi", "name_ja": "白石杏", "tag_type": "character"},
            {"tag_id": self.tag_kohane, "name": "小豆泽心羽", "standard_name": "小豆泽心羽",
             "name_en": "Kohane Azusawa", "name_ja": "小豆沢こはね", "tag_type": "character"},
            {"tag_id": self.tag_vbs, "name": "Vivid BAD SQUAD", "tag_type": "theme",
             "member_ids": [self.tag_akito, self.tag_toya, self.tag_an, self.tag_kohane]},
            {"tag_id": self.tag_pair, "name": "杏豆", "tag_type": "pairing",
             "member_ids": [self.tag_an, self.tag_kohane]},
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def make_service(self, bot=None, *, config=None):
        merged = {"chat_image_collection_enabled": True}
        merged.update(config or {})
        return confirm_module.ChatImageConfirmService(
            db=self.db,
            context=FakeContext(bot),
            config=merged,
            importer=self.importer,
            candidate_tags_provider=lambda: [dict(item) for item in self.candidates],
        )

    def make_audited(self, name="a.png", tag_ids=None, *, message_id="m1"):
        local_path = Path(self.tmp.name) / name
        PillowImage.new('RGB', (10, 10), color=(len(name), ord(name[0]), 30)).save(local_path)
        candidate = self.db.create_chat_image_candidate(
            ref=f"ref-{name}-{message_id}", session_id=SESSION, group_id="1",
            platform="aiocqhttp", sender_id="u1", sender_name="Alice",
            source_message_id=message_id, image_index=1,
            image_url=f"https://example.com/{name}", file_path=str(local_path),
        )
        self.db.complete_chat_image_candidate_audit(
            int(candidate["id"]), status="audited", decision="approve",
            provider="provider-1", prompt_version="chat-v1",
            quality={"overall": 90}, flags=[], reason="ok",
            proposed_tag_ids=tag_ids or [self.tag_akito],
        )
        return self.db.get_chat_image_candidate(int(candidate["id"]))

    def make_image(self, name="a.png", sha="sha-a"):
        return self.db.upsert_image(
            file_path=f"/lib/{name}", file_name=name, sha256=sha,
            phash="", width=10, height=10, format_="png",
        )

    async def test_announce_then_confirm_writes_and_receipts(self):
        image_id = self.make_image("a.png")
        self.importer = FakeImporter({"a.png": (image_id, "sha-a")})
        bot = FakeBot(message_id=555)
        service = self.make_service(bot)
        candidate = self.make_audited("a.png", [self.tag_akito])
        summary = await service.run_once()
        self.assertEqual({"batches": 1, "sent": 1, "expired": 0, "failed": 0, "auto": 0}, summary)
        self.assertEqual(1, len(bot.sent))
        group_id, segments = bot.sent[0]
        self.assertEqual(1, group_id)
        self.assertEqual("at", segments[0]["type"])
        self.assertEqual("u1", segments[0]["data"]["qq"])
        image_segments = [part for part in segments if part['type'] == 'image']
        self.assertEqual(1, len(image_segments))
        self.assertEqual(Path(candidate['file_path']).read_bytes(),
                         base64.b64decode(image_segments[0]['data']['file'].removeprefix('base64://')))
        asked = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("asked", asked["status"])
        self.assertEqual("555", asked["confirm_message_id"])

        event = Event(
            [{"type": "reply", "data": {"id": "555"}}],
            message_str="确认", sender_id="u1", bot=bot,
        )
        self.assertTrue(await service.handle_reply(event))
        written = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("approved_written", written["status"])
        self.assertEqual(image_id, int(written["image_id"]))
        self.assertEqual([self.tag_akito], written["confirmed_tag_ids_json"])
        detail = self.db.get_image_detail(image_id, sync_files=False)
        self.assertIn("东云彰人", {tag["name"] for tag in detail["tags"]})
        self.assertTrue(any(source["platform"] == "chat" for source in detail["sources"]))
        self.assertEqual(2, len(bot.sent))
        self.assertEqual('reply', bot.sent[-1][1][0]['type'])
        self.assertIn(f'第1张 → 图片 ID：#{image_id}', bot.sent[-1][1][-1]['data']['text'])
        self.assertFalse(any(part['type'] == 'image' for part in bot.sent[-1][1]))

    async def test_announce_pairs_each_number_with_its_image_even_with_same_tags(self):
        self.importer = FakeImporter(default=(1, "sha-x"))
        bot = FakeBot(message_id=555)
        service = self.make_service(bot)
        self.make_audited("a.png", [self.tag_akito], message_id="m1")
        self.make_audited("b.png", [self.tag_akito], message_id="m2")
        self.make_audited("c.png", [self.tag_akito, self.tag_toya], message_id="m3")
        await service.run_once()
        parts = bot.sent[0][1]
        labels = [part['data']['text'] for part in parts if part['type'] == 'text']
        self.assertIn('\n第1张：东云彰人\n', labels)
        self.assertIn('\n第2张：东云彰人\n', labels)
        self.assertIn('\n第3张：东云彰人、青柳冬弥\n', labels)
        image_parts = [part for part in parts if part['type'] == 'image']
        self.assertEqual(3, len(image_parts))
        for filename, part in zip(['a.png', 'b.png', 'c.png'], image_parts):
            self.assertEqual((Path(self.tmp.name) / filename).read_bytes(),
                             base64.b64decode(part['data']['file'].removeprefix('base64://')))

    async def test_auto_approve_writes_without_asking(self):
        image_id = self.make_image("a.png")
        self.importer = FakeImporter({"a.png": (image_id, "sha-a")})
        bot = FakeBot(message_id=555)
        service = self.make_service(bot, config={"chat_image_auto_approve_enabled": True})
        candidate = self.make_audited("a.png", [self.tag_akito])
        summary = await service.run_once()
        self.assertEqual(1, summary["auto"])
        self.assertEqual(0, summary["sent"])
        row = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("approved_written", row["status"])
        self.assertEqual([], bot.sent)

    async def test_reject_reply_marks_rejected(self):
        self.importer = FakeImporter(default=(1, "sha-x"))
        bot = FakeBot(message_id=555)
        service = self.make_service(bot)
        candidate = self.make_audited("a.png")
        await service.run_once()
        event = Event([{"type": "reply", "data": {"id": "555"}}], message_str="不要", bot=bot)
        self.assertTrue(await service.handle_reply(event))
        rejected = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("rejected", rejected["status"])
        self.assertEqual([], self.importer.calls)

    async def test_reply_from_other_sender_is_ignored(self):
        self.importer = FakeImporter(default=(1, "sha-x"))
        bot = FakeBot(message_id=555)
        service = self.make_service(bot)
        candidate = self.make_audited("a.png")
        await service.run_once()
        event = Event(
            [{"type": "reply", "data": {"id": "555"}}],
            message_str="确认", sender_id="u2", bot=bot,
        )
        self.assertFalse(await service.handle_reply(event))
        row = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("asked", row["status"])

    async def test_at_bot_confirms_only_unambiguous_batch(self):
        image_id = self.make_image("a.png")
        self.importer = FakeImporter({"a.png": (image_id, "sha-a")})
        bot = FakeBot(message_id=555)
        service = self.make_service(bot)
        candidate = self.make_audited("a.png")
        await service.run_once()
        event = Event(
            [{"type": "at", "data": {"qq": "bot"}}],
            message_str="确认", sender_id="u1", bot=bot,
        )
        self.assertTrue(await service.handle_reply(event))
        row = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("approved_written", row["status"])

    async def test_correction_replaces_tags_for_one_image(self):
        image_a = self.make_image("a.png", "sha-a")
        image_b = self.make_image("b.png", "sha-b")
        self.importer = FakeImporter({"a.png": (image_a, "sha-a"), "b.png": (image_b, "sha-b")})
        bot = FakeBot(message_id=555)
        service = self.make_service(bot)
        first = self.make_audited("a.png", [self.tag_an, self.tag_kohane, self.tag_pair], message_id="m1")
        second = self.make_audited("b.png", [self.tag_akito], message_id="m2")
        await service.run_once()
        event = Event(
            [{"type": "reply", "data": {"id": "555"}}],
            message_str="第2张是白石杏", sender_id="u1", bot=bot,
        )
        self.assertTrue(await service.handle_reply(event))
        corrected = self.db.get_chat_image_candidate(int(second["id"]))
        self.assertEqual("approved_written", corrected["status"])
        self.assertEqual([self.tag_an], corrected["confirmed_tag_ids_json"])
        peer = self.db.get_chat_image_candidate(int(first["id"]))
        self.assertEqual("asked", peer["status"])
        self.assertEqual([], peer["confirmed_tag_ids_json"])
        detail = self.db.get_image_detail(image_b, sync_files=False)
        self.assertEqual({"白石杏"}, {tag["name"] for tag in detail["tags"]})

    async def test_correction_missing_member_keeps_asked(self):
        self.importer = FakeImporter(default=(1, "sha-x"))
        bot = FakeBot(message_id=555)
        service = self.make_service(bot)
        candidate = self.make_audited("a.png", [self.tag_akito])
        await service.run_once()
        sent_before = len(bot.sent)
        event = Event(
            [{"type": "reply", "data": {"id": "555"}}],
            message_str="第1张是白石杏、杏豆", sender_id="u1", bot=bot,
        )
        self.assertTrue(await service.handle_reply(event))
        row = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("asked", row["status"])
        self.assertEqual(sent_before + 1, len(bot.sent))
        self.assertIn("小豆泽心羽", bot.sent[-1][1][-1]["data"]["text"])
        self.assertEqual([], self.importer.calls)

    async def test_quoted_bot_message_path_without_stored_id(self):
        image_id = self.make_image("a.png")
        self.importer = FakeImporter({"a.png": (image_id, "sha-a")})
        get_msg = {
            "sender": {"user_id": "bot", "nickname": "Bot"},
            "message": [
                {"type": "at", "data": {"qq": "u1"}},
                {"type": "text", "data": {"text": "【收图确认】确认批次：local:" + "a" * 32}},
            ],
        }
        bot = FakeBot(message_id=None, get_msg=get_msg)
        service = self.make_service(bot)
        candidate = self.make_audited("a.png")
        self.db.mark_chat_image_candidates_asked(
            [int(candidate["id"])], confirm_message_id="local:" + "a" * 32,
            expires_at="2999-01-01T00:00:00+00:00",
        )
        event = Event(
            [{"type": "reply", "data": {"id": "9001"}}],
            message_str="确认", sender_id="u1", bot=bot,
        )
        self.assertTrue(await service.handle_reply(event))
        row = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("approved_written", row["status"])

    async def test_timeout_moves_asked_to_pending_webui(self):
        self.importer = FakeImporter(default=(1, "sha-x"))
        service = self.make_service()
        candidate = self.make_audited("a.png")
        self.db.mark_chat_image_candidates_asked(
            [int(candidate["id"])], confirm_message_id="555",
            expires_at="2000-01-01T00:00:00+00:00",
        )
        summary = await service.run_once()
        self.assertEqual(1, summary["expired"])
        row = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("pending_webui", row["status"])

    async def test_webui_approve_writes_pending_webui_candidate(self):
        image_id = self.make_image("a.png")
        self.importer = FakeImporter({"a.png": (image_id, "sha-a")})
        service = self.make_service()
        candidate = self.make_audited("a.png", [self.tag_akito])
        self.db.mark_chat_image_candidates_asked(
            [int(candidate["id"])], confirm_message_id="555",
            expires_at="2000-01-01T00:00:00+00:00",
        )
        self.db.expire_asked_chat_image_candidates("2999-01-01T00:00:00+00:00")
        self.assertEqual(
            "pending_webui", self.db.get_chat_image_candidate(int(candidate["id"]))["status"])

        result = await service.approve_candidates([int(candidate["id"])])
        self.assertEqual({"approved": 1, "failed": 0}, result)
        row = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("approved_written", row["status"])
        self.assertEqual("webui", row["confirm_user_id"])
        detail = self.db.get_image_detail(image_id, sync_files=False)
        self.assertIn("东云彰人", {tag["name"] for tag in detail["tags"]})

    async def test_webui_reject_marks_pending_webui_rejected(self):
        self.importer = FakeImporter(default=(1, "sha-x"))
        service = self.make_service()
        candidate = self.make_audited("a.png")
        self.db.mark_chat_image_candidates_asked(
            [int(candidate["id"])], confirm_message_id="555",
            expires_at="2000-01-01T00:00:00+00:00",
        )
        self.db.expire_asked_chat_image_candidates("2999-01-01T00:00:00+00:00")

        rejected = service.reject_candidates([int(candidate["id"])])
        self.assertEqual(1, rejected)
        row = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("rejected", row["status"])
        self.assertEqual("webui", row["confirm_user_id"])
        self.assertEqual([], self.importer.calls)

    async def test_webui_approve_skips_non_pending_candidates(self):
        self.importer = FakeImporter(default=(1, "sha-x"))
        service = self.make_service()
        candidate = self.make_audited("a.png")
        result = await service.approve_candidates([int(candidate["id"])])
        self.assertEqual({"approved": 0, "failed": 0}, result)
        row = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("audited", row["status"])

    async def test_send_failure_keeps_candidate_audited(self):
        self.importer = FakeImporter(default=(1, "sha-x"))
        bot = FakeBot(message_id=555, fail=True)
        service = self.make_service(bot)
        candidate = self.make_audited("a.png")
        summary = await service.run_once()
        self.assertEqual(0, summary["sent"])
        row = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("audited", row["status"])

    async def test_disabled_service_does_nothing(self):
        self.importer = FakeImporter(default=(1, "sha-x"))
        service = self.make_service(config={"chat_image_collection_enabled": False})
        self.make_audited("a.png")
        summary = await service.run_once()
        self.assertEqual(0, summary["sent"])

    async def test_missing_preview_moves_to_review_without_text_only_question(self):
        self.importer = FakeImporter(default=(1, 'sha-x'))
        bot = FakeBot()
        service = self.make_service(bot)
        candidate = self.make_audited()
        Path(candidate['file_path']).unlink()
        result = await service.run_once()
        self.assertEqual(0, result['sent'])
        self.assertEqual([], bot.sent)
        self.assertEqual('pending_webui', self.db.get_chat_image_candidate(candidate['id'])['status'])

    async def test_generated_fallback_token_selects_original_not_newest_batch(self):
        image_a = self.make_image('a.png', 'sha-a')
        self.importer = FakeImporter({'a.png': (image_a, 'sha-a')})
        bot = FakeBot(message_id=None)
        service = self.make_service(bot)
        first = self.make_audited('a.png')
        await service.run_once()
        first_message = bot.sent[0][1]
        second = self.make_audited('b.png')
        await service.run_once()
        bot.get_msg = {'sender': {'user_id': 'bot'}, 'group_id': 1, 'message': first_message}
        event = Event([{'type': 'reply', 'data': {'id': '9001'}}], message_str='确认', bot=bot)
        self.assertTrue(await service.handle_reply(event))
        self.assertEqual('approved_written', self.db.get_chat_image_candidate(first['id'])['status'])
        self.assertEqual('asked', self.db.get_chat_image_candidate(second['id'])['status'])
        self.assertEqual(['a.png'], self.importer.calls)

    async def test_selective_confirmation_keeps_original_number_after_partial_write(self):
        image_a = self.make_image('a.png', 'sha-a')
        image_b = self.make_image('b.png', 'sha-b')
        self.importer = FakeImporter({'a.png': (image_a, 'sha-a'), 'b.png': (image_b, 'sha-b')})
        bot = FakeBot()
        service = self.make_service(bot)
        first = self.make_audited('a.png')
        second = self.make_audited('b.png')
        await service.run_once()
        event = Event([{'type': 'reply', 'data': {'id': '555'}}], message_str='收第2张', bot=bot)
        self.assertTrue(await service.handle_reply(event))
        self.assertEqual('asked', self.db.get_chat_image_candidate(first['id'])['status'])
        self.assertIn(f'第2张 → 图片 ID：#{image_b}', bot.sent[-1][1][-1]['data']['text'])
        event.message_str = '全部收'
        self.assertTrue(await service.handle_reply(event))
        receipt = bot.sent[-1][1][-1]['data']['text']
        self.assertIn(f'第1张 → 图片 ID：#{image_a}', receipt)
        self.assertNotIn(f'#{image_b}', receipt)
        self.assertEqual(['b.png', 'a.png'], self.importer.calls)

    async def test_failure_does_not_renumber_success_or_claim_a_failed_id(self):
        image_b = self.make_image('b.png', 'sha-b')
        self.importer = FakeImporter({'b.png': (image_b, 'sha-b')})
        bot = FakeBot()
        service = self.make_service(bot)
        first = self.make_audited('a.png')
        self.make_audited('b.png')
        await service.run_once()
        event = Event([{'type': 'reply', 'data': {'id': '555'}}], message_str='确认', bot=bot)
        await service.handle_reply(event)
        text = bot.sent[-1][1][-1]['data']['text']
        self.assertIn('第1张：收录失败', text)
        self.assertIn(f'第2张 → 图片 ID：#{image_b}', text)
        self.assertEqual('write_failed', self.db.get_chat_image_candidate(first['id'])['status'])

    async def test_duplicate_uses_existing_gallery_id(self):
        existing_id = self.make_image()
        self.importer = FakeImporter({'a.png': (existing_id, 'sha-a', False)})
        bot = FakeBot()
        service = self.make_service(bot)
        self.make_audited()
        await service.run_once()
        event = Event([{'type': 'reply', 'data': {'id': '555'}}], message_str='确认', bot=bot)
        await service.handle_reply(event)
        receipt = bot.sent[-1][1][-1]['data']['text']
        self.assertIn(f'图片 ID：#{existing_id}', receipt)
        self.assertIn('已在图库，本次未新增图片', receipt)

    async def test_parallel_repeated_confirmation_imports_once(self):
        existing_id = self.make_image()
        self.importer = FakeImporter({'a.png': (existing_id, 'sha-a')})
        bot = FakeBot()
        service = self.make_service(bot)
        self.make_audited()
        await service.run_once()
        event = Event([{'type': 'reply', 'data': {'id': '555'}}], message_str='确认', bot=bot)
        results = await asyncio.gather(service.handle_reply(event), service.handle_reply(event))
        self.assertEqual([True, False], results)
        self.assertEqual(['a.png'], self.importer.calls)

    async def test_multiple_batches_at_only_does_not_choose_latest(self):
        self.importer = FakeImporter(default=(1, 'sha-x'))
        bot = FakeBot()
        service = self.make_service(bot)
        first = self.make_audited('a.png')
        await service.run_once()
        bot.message_id = 556
        second = self.make_audited('b.png')
        await service.run_once()
        event = Event([{'type': 'at', 'data': {'qq': 'bot'}}], message_str='确认', bot=bot)
        self.assertTrue(await service.handle_reply(event))
        self.assertIn('有多批图片', bot.sent[-1][1][-1]['data']['text'])
        self.assertEqual([], self.importer.calls)
        for row in [first, second]:
            self.assertEqual('asked', self.db.get_chat_image_candidate(row['id'])['status'])

    async def test_old_expired_quote_with_at_cannot_confirm_new_batch(self):
        self.importer = FakeImporter(default=(1, 'sha-x'))
        bot = FakeBot(get_msg={'sender': {'user_id': 'bot'}, 'message': [
            {'type': 'at', 'data': {'qq': 'u1'}},
            {'type': 'text', 'data': {'text': '【收图确认】旧的文字确认'}},
        ]})
        service = self.make_service(bot)
        old = self.make_audited('a.png')
        self.db.mark_chat_image_candidates_asked([old['id']], confirm_message_id='554',
                                                expires_at='2000-01-01T00:00:00+00:00')
        new = self.make_audited('b.png')
        await service.run_once()
        event = Event([{'type': 'reply', 'data': {'id': '554'}},
                       {'type': 'at', 'data': {'qq': 'bot'}}], message_str='确认', bot=bot)
        self.assertFalse(await service.handle_reply(event))
        self.assertEqual([], self.importer.calls)
        self.assertEqual('asked', self.db.get_chat_image_candidate(new['id'])['status'])

    async def test_cross_group_quote_does_not_confirm(self):
        self.importer = FakeImporter(default=(1, 'sha-x'))
        bot = FakeBot()
        service = self.make_service(bot)
        self.make_audited()
        await service.run_once()
        event = Event([{'type': 'reply', 'data': {'id': '555'}}], message_str='确认',
                      session='aiocqhttp:GroupMessage:2', group_id='2', bot=bot)
        self.assertFalse(await service.handle_reply(event))
        self.assertEqual([], self.importer.calls)

    async def test_rejected_send_ack_does_not_mark_asked(self):
        class RejectedBot(FakeBot):
            async def send_group_msg(self, group_id, message):
                return {'status': 'failed', 'retcode': 100}
        self.importer = FakeImporter(default=(1, 'sha-x'))
        service = self.make_service(RejectedBot())
        candidate = self.make_audited()
        self.assertEqual(0, (await service.run_once())['sent'])
        self.assertEqual('audited', self.db.get_chat_image_candidate(candidate['id'])['status'])


if __name__ == "__main__":
    unittest.main()
