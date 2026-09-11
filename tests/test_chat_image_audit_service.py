from __future__ import annotations

import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


CORE_DIR = Path(__file__).resolve().parents[1] / "core"
PACKAGE = "pjsk_audit_test_core"


def install_stubs() -> None:
    try:
        import astrbot.api  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None, debug=lambda *a, **k: None,
    )
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api})
    astrbot.api = api


install_stubs()
if PACKAGE not in sys.modules:
    pkg = types.ModuleType(PACKAGE)
    pkg.__path__ = [str(CORE_DIR)]
    sys.modules[PACKAGE] = pkg

db_module = importlib.import_module(f"{PACKAGE}.db")
audit_module = importlib.import_module(f"{PACKAGE}.chat_image_audit_service")
review_module = importlib.import_module(f"{PACKAGE}.llm_image_review_service")

LlmImageReviewContractError = review_module.LlmImageReviewContractError

CANDIDATES = [
    {"tag_id": 1, "name": "初音未来", "tag_type": "character",
     "name_en": "Hatsune Miku", "name_ja": "初音ミク"},
    {"tag_id": 2, "name": "镜音铃", "tag_type": "character",
     "name_en": "Kagamine Rin", "name_ja": "鏡音リン"},
    {"tag_id": 3, "name": "巡音流歌", "tag_type": "character",
     "name_en": "Megurine Luka", "name_ja": "巡音ルカ"},
    {"tag_id": 10, "name": "VIRTUAL SINGER", "tag_type": "theme", "member_ids": [1, 2, 3]},
    {"tag_id": 11, "name": "杏豆", "tag_type": "pairing", "member_ids": [1, 2]},
]


def audit_text(*, characters=None, additional=None, flags=None,
               decision="approve", reason="图不错") -> str:
    payload = {
        "quality": {
            "technical": 90,
            "aesthetic": 88,
            "gallery_fit": 90,
            "overall": 89,
            "flags": list(flags or []),
        },
        "characters": (
            [{"tag_id": 1, "confidence": 0.95}] if characters is None else characters
        ),
        "additional_tags": list(additional or []),
        "decision": decision,
        "reason": reason,
    }
    return json.dumps(payload, ensure_ascii=False)


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.completion_text = text


class FakeContext:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def llm_generate(self, **_kwargs):
        self.calls += 1
        return FakeResponse(self.text)


class FakeReviewService:
    def __init__(self, *, preview=None) -> None:
        self.preview = preview or {
            "image_uri": "file:///tmp/preview.jpg",
            "blocking_flags": [],
            "quality": {"technical": 0.0, "aesthetic": 0.0,
                        "gallery_fit": 0.0, "overall": 0.0, "flags": []},
        }

    def provider_id(self) -> str:
        return "provider-1"

    def timeout_seconds(self) -> int:
        return 5

    def max_attempts(self) -> int:
        return 3

    def _prepare_preview(self, _image):
        return self.preview

    @staticmethod
    def _sanitize_error(error) -> str:
        return str(error)[:200]


class AuditParseTests(unittest.TestCase):
    def test_build_candidates_splits_characters_and_additional(self):
        payload = audit_module.build_audit_candidates(CANDIDATES)
        self.assertEqual([1, 2, 3], [item["tag_id"] for item in payload["characters"]])
        self.assertEqual(["Hatsune Miku", "初音ミク"], payload["characters"][0]["aliases"])
        group = next(item for item in payload["additional"] if item["tag_id"] == 10)
        self.assertEqual(["初音未来", "镜音铃", "巡音流歌"], group["member_names"])

    def test_prompt_mentions_candidates_and_flags(self):
        prompt = audit_module.build_audit_prompt(CANDIDATES)
        self.assertIn("初音未来", prompt)
        self.assertIn("low_resolution", prompt)
        self.assertIn("approve|reject|uncertain", prompt)

    def test_parse_approve_keeps_proposed_ids(self):
        parsed = audit_module.parse_audit_response(
            audit_text(
                characters=[{"tag_id": 1, "confidence": 0.9},
                            {"tag_id": 2, "confidence": 0.9}],
                additional=[11],
            ),
            candidates=CANDIDATES,
        )
        self.assertEqual("approve", parsed["decision"])
        self.assertEqual([1, 2, 11], parsed["proposed_tag_ids"])

    def test_parse_unknown_character_raises(self):
        with self.assertRaises(LlmImageReviewContractError):
            audit_module.parse_audit_response(
                audit_text(characters=[{"tag_id": 99, "confidence": 0.9}]),
                candidates=CANDIDATES,
            )

    def test_parse_approve_without_characters_downgrades(self):
        parsed = audit_module.parse_audit_response(
            audit_text(characters=[]), candidates=CANDIDATES,
        )
        self.assertEqual("uncertain", parsed["decision"])

    def test_parse_approve_with_flag_downgrades(self):
        parsed = audit_module.parse_audit_response(
            audit_text(flags=["blurry"]), candidates=CANDIDATES,
        )
        self.assertEqual("uncertain", parsed["decision"])
        self.assertEqual(["blurry"], parsed["quality"]["flags"])

    def test_parse_drops_group_missing_members(self):
        parsed = audit_module.parse_audit_response(
            audit_text(characters=[{"tag_id": 1, "confidence": 0.9}], additional=[10]),
            candidates=CANDIDATES,
        )
        self.assertEqual([1], parsed["proposed_tag_ids"])

    def test_parse_invalid_json_raises(self):
        with self.assertRaises(LlmImageReviewContractError):
            audit_module.parse_audit_response("不是 JSON", candidates=CANDIDATES)


class AuditServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = db_module.ImageIndexDB(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def make_candidate(self, *, ref="g1", file_path="C:/tmp/a.png") -> dict:
        return self.db.create_chat_image_candidate(
            ref=ref,
            session_id="aiocqhttp:GroupMessage:1",
            group_id="1",
            platform="aiocqhttp",
            sender_id="u1",
            sender_name="Alice",
            source_message_id="m1",
            image_url="file:///tmp/a.png",
            file_path=file_path,
        )

    def make_service(self, *, text=audit_text(), enabled=True, preview=None):
        context = FakeContext(text)
        review_service = FakeReviewService(preview=preview)
        service = audit_module.ChatImageAuditService(
            db=self.db,
            context=context,
            config={"chat_image_collection_enabled": enabled},
            review_service=review_service,
            candidate_tags_provider=lambda: [dict(item) for item in CANDIDATES],
        )
        return service, context

    async def test_run_once_writes_audit_result(self):
        candidate = self.make_candidate()
        service, context = self.make_service(text=audit_text(
            characters=[{"tag_id": 1, "confidence": 0.9},
                        {"tag_id": 2, "confidence": 0.9}],
            additional=[11],
        ))
        summary = await service.run_once()
        self.assertEqual(1, summary["processed"])
        self.assertEqual(1, summary["audited"])
        self.assertEqual(1, context.calls)
        row = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("audited", row["status"])
        self.assertEqual("approve", row["audit_decision"])
        self.assertEqual([1, 2, 11], row["proposed_tag_ids_json"])
        self.assertEqual("provider-1", row["audit_provider"])

    async def test_reject_goes_pending_webui(self):
        candidate = self.make_candidate()
        service, context = self.make_service(
            text=audit_text(characters=[], decision="reject", reason="不是 PJSK"),
        )
        summary = await service.run_once()
        self.assertEqual(1, summary["pending_webui"])
        self.assertEqual(1, context.calls)
        row = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("pending_webui", row["status"])
        self.assertEqual("reject", row["audit_decision"])

    async def test_blocking_preview_skips_llm(self):
        candidate = self.make_candidate()
        service, context = self.make_service(preview={
            "image_uri": "file:///tmp/preview.jpg",
            "blocking_flags": ["low_resolution"],
            "quality": {"technical": 0.0, "aesthetic": 0.0,
                        "gallery_fit": 0.0, "overall": 0.0, "flags": ["low_resolution"]},
        })
        summary = await service.run_once()
        self.assertEqual(1, summary["pending_webui"])
        self.assertEqual(0, context.calls)
        row = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("pending_webui", row["status"])
        self.assertEqual("uncertain", row["audit_decision"])

    async def test_invalid_response_retries_candidate(self):
        candidate = self.make_candidate()
        service, _ = self.make_service(text="呵呵")
        summary = await service.run_once()
        self.assertEqual(1, summary["retried"])
        row = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("captured", row["status"])
        self.assertEqual(1, int(row["attempt_count"]))

    async def test_disabled_service_does_nothing(self):
        candidate = self.make_candidate()
        service, context = self.make_service(enabled=False)
        summary = await service.run_once()
        self.assertEqual(0, summary["processed"])
        self.assertEqual(0, context.calls)
        row = self.db.get_chat_image_candidate(int(candidate["id"]))
        self.assertEqual("captured", row["status"])


if __name__ == "__main__":
    unittest.main()
