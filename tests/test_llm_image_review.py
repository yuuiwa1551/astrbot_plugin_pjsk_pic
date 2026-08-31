from __future__ import annotations

import hashlib
import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

from PIL import Image


CORE_DIR = Path(__file__).resolve().parents[1] / "core"
PACKAGE_NAME = "pjsk_pic_llm_review_test_core"


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

if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(CORE_DIR)]
    sys.modules[PACKAGE_NAME] = package

db_module = importlib.import_module(f"{PACKAGE_NAME}.db")
review_module = importlib.import_module(f"{PACKAGE_NAME}.llm_image_review_service")
legacy_review_module = importlib.import_module(f"{PACKAGE_NAME}.review_service")

ImageIndexDB = db_module.ImageIndexDB
LlmImageReviewContractError = review_module.LlmImageReviewContractError
LlmImageReviewService = review_module.LlmImageReviewService
VALID_FLAGS = review_module.VALID_FLAGS
ReviewService = legacy_review_module.ReviewService


class _Response:
    def __init__(self, text: str) -> None:
        self.completion_text = text


class _Context:
    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected LLM call")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return _Response(value)


class LlmImageReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = ImageIndexDB(self.root / "image_index.db")
        self.miku_id = self.db.get_or_create_tag("初音未来", is_character=True)
        self.rin_id = self.db.get_or_create_tag("镜音铃", is_character=True)
        self.db.add_alias("初音未来", "Hatsune Miku")
        self.image_id = self._add_image("sample", [self.miku_id, self.rin_id])

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _add_image(self, name: str, tag_ids: list[int], *, size: tuple[int, int] = (1024, 1024)) -> int:
        path = self.root / f"{name}.jpg"
        Image.new("RGB", size, (80, 160, 220)).save(path, format="JPEG")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        image_id = self.db.upsert_image(
            file_path=str(path),
            file_name=path.name,
            sha256=digest,
            width=size[0],
            height=size[1],
            format_="jpeg",
        )
        self.db.upsert_source(
            image_id,
            "pixiv",
            f"https://www.pixiv.net/artworks/{image_id}",
            f"https://i.pximg.net/{image_id}.jpg",
            raw_tags=[],
        )
        for tag_id in tag_ids:
            self.db.link_image_tag(
                image_id,
                tag_id,
                source_type="crawl:pixiv",
                review_status="pending",
            )
            self.db.create_review_task(image_id, tag_id, "pending", reason="test")
        return image_id

    def _payload(
        self,
        selected: list[tuple[int, float]] | None = None,
        *,
        overall: int = 93,
        aesthetic: int = 94,
        flags: list[str] | None = None,
        decision: str = "approve",
    ) -> str:
        return json.dumps(
            {
                "quality": {
                    "technical": 91,
                    "aesthetic": aesthetic,
                    "gallery_fit": 92,
                    "overall": overall,
                    "flags": list(flags or []),
                },
                "characters": [
                    {"tag_id": tag_id, "confidence": confidence}
                    for tag_id, confidence in (selected or [(self.miku_id, 0.98)])
                ],
                "decision": decision,
                "reason": "画面完整清晰，角色特征明确",
            },
            ensure_ascii=False,
        )

    def _service(self, context: _Context, *, mode: str = "shadow", **overrides):
        config = {
            "llm_image_review_enabled": True,
            "llm_image_review_mode": mode,
            "llm_image_review_provider_id": "vision-test",
            "llm_image_review_prompt_version": "test-v1",
            "llm_image_review_daily_limit": 100,
            "llm_image_review_max_per_cycle": 10,
            "llm_image_review_max_candidates": 8,
            "llm_image_review_preview_max_side": 768,
            "llm_image_review_min_side": 128,
            "llm_image_review_timeout_seconds": 10,
            "llm_image_review_max_attempts": 2,
            "llm_image_review_quality_threshold": 85,
            "llm_image_review_technical_threshold": 75,
            "llm_image_review_aesthetic_threshold": 82,
            "llm_image_review_gallery_fit_threshold": 80,
            "llm_image_review_identity_threshold": 0.93,
            "llm_image_review_spot_check_rate": 0.0,
            **overrides,
        }
        return LlmImageReviewService(
            db=self.db,
            context=context,
            config=config,
            data_dir=self.root,
            random_func=lambda: 0.5,
        )

    async def test_shadow_calls_model_once_and_does_not_change_review_status(self) -> None:
        context = _Context([self._payload()])
        service = self._service(context, mode="shadow")
        run_id, created, _ = service.queue_image(self.image_id, platform="pixiv")
        duplicate_id, duplicate_created, _ = service.queue_image(self.image_id, platform="pixiv")
        self.assertTrue(created)
        self.assertEqual(run_id, duplicate_id)
        self.assertFalse(duplicate_created)

        summary = await service.run_once(force=True, max_runs=1)

        self.assertEqual(1, summary["completed"])
        self.assertEqual(1, len(context.calls))
        self.assertTrue(context.calls[0]["image_urls"][0].startswith("file:"))
        statuses = {str(row["status"]) for row in self.db.get_review_tasks_for_image(self.image_id)}
        self.assertEqual({"pending"}, statuses)
        suggestion = service.latest_suggestion(self.image_id)
        self.assertEqual("shadow", suggestion["run_mode"])
        self.assertEqual(self.miku_id, suggestion["characters"][0]["tag_id"])

    async def test_auto_approve_updates_selected_and_rejects_unselected_atomically(self) -> None:
        context = _Context([self._payload()])
        service = self._service(context, mode="auto_approve")
        service.queue_image(self.image_id, platform="pixiv")

        summary = await service.run_once(force=True, max_runs=1)

        self.assertEqual(1, summary["auto_approved"])
        statuses = {
            int(row["tag_id"]): str(row["status"])
            for row in self.db.get_review_tasks_for_image(self.image_id)
        }
        self.assertEqual("approved", statuses[self.miku_id])
        self.assertEqual("rejected", statuses[self.rin_id])
        run = self.db.get_latest_llm_image_review_run(self.image_id)
        self.assertEqual("auto_approved", str(run["decision"]))

    async def test_auto_approve_supports_multiple_selected_characters(self) -> None:
        context = _Context([self._payload(selected=[(self.miku_id, 0.98), (self.rin_id, 0.96)])])
        service = self._service(context, mode="auto_approve")
        service.queue_image(self.image_id, platform="pixiv")

        summary = await service.run_once(force=True, max_runs=1)

        self.assertEqual(1, summary["auto_approved"])
        statuses = {
            int(row["tag_id"]): str(row["status"])
            for row in self.db.get_review_tasks_for_image(self.image_id)
        }
        self.assertEqual("approved", statuses[self.miku_id])
        self.assertEqual("approved", statuses[self.rin_id])

    async def test_blocking_quality_flag_never_auto_approves(self) -> None:
        context = _Context([self._payload(flags=["watermark_heavy"])])
        service = self._service(context, mode="auto_approve")
        service.queue_image(self.image_id, platform="pixiv")

        summary = await service.run_once(force=True, max_runs=1)

        self.assertEqual(0, summary["auto_approved"])
        self.assertEqual(
            {"pending"},
            {str(row["status"]) for row in self.db.get_review_tasks_for_image(self.image_id)},
        )

    async def test_low_aesthetic_score_never_auto_approves(self) -> None:
        context = _Context([self._payload(aesthetic=60)])
        service = self._service(context, mode="auto_approve")
        service.queue_image(self.image_id, platform="pixiv")

        summary = await service.run_once(force=True, max_runs=1)

        self.assertEqual(0, summary["auto_approved"])
        run = self.db.get_latest_llm_image_review_run(self.image_id)
        result = json.loads(str(run["result_json"]))
        self.assertIn("aesthetic_below_threshold", result["policy"]["blocking_reasons"])

    async def test_manual_result_wins_over_queued_auto_review(self) -> None:
        context = _Context([self._payload()])
        service = self._service(context, mode="auto_approve")
        service.queue_image(self.image_id, platform="pixiv")
        miku_task = next(
            row
            for row in self.db.get_review_tasks_for_image(self.image_id)
            if int(row["tag_id"]) == self.miku_id
        )
        ok, _ = self.db.apply_manual_review(int(miku_task["id"]), approved=True)
        self.assertTrue(ok)

        summary = await service.run_once(force=True, max_runs=1)

        self.assertEqual(0, summary["auto_approved"])
        statuses = {
            int(row["tag_id"]): str(row["status"])
            for row in self.db.get_review_tasks_for_image(self.image_id)
        }
        self.assertEqual("manual_approved", statuses[self.miku_id])
        self.assertEqual("pending", statuses[self.rin_id])
        run = self.db.get_latest_llm_image_review_run(self.image_id)
        result = json.loads(str(run["result_json"]))
        self.assertEqual("manual_review_won", result["policy"]["apply_result"]["code"])

    async def test_out_of_candidate_response_is_recorded_without_status_change(self) -> None:
        context = _Context([self._payload(selected=[(999999, 0.99)])])
        service = self._service(context, mode="auto_approve")
        service.queue_image(self.image_id, platform="pixiv")

        summary = await service.run_once(force=True, max_runs=1)

        self.assertEqual(1, summary["manual_review"])
        self.assertEqual(
            {"pending"},
            {str(row["status"]) for row in self.db.get_review_tasks_for_image(self.image_id)},
        )
        run = self.db.get_latest_llm_image_review_run(self.image_id)
        self.assertIn("候选范围", str(run["reason"]))

    async def test_provider_failure_retries_then_stops(self) -> None:
        context = _Context([RuntimeError("temporary timeout"), RuntimeError("temporary timeout")])
        service = self._service(context, mode="shadow")
        service.queue_image(self.image_id, platform="pixiv")

        first = await service.run_once(force=True, max_runs=1)
        second = await service.run_once(force=True, max_runs=1)

        self.assertEqual(1, first["retried"])
        self.assertEqual(1, second["failed"])
        stats = self.db.get_llm_image_review_stats()
        self.assertEqual(1, stats["failed"])
        run = self.db.get_latest_llm_image_review_run(self.image_id, completed_only=False)
        history = json.loads(str(run["error_history_json"] or "[]"))
        self.assertEqual(2, len(history))
        self.assertEqual([1, 2], [int(item["attempt"]) for item in history])

    async def test_retryable_failure_stops_current_cycle_before_other_runs(self) -> None:
        second_image_id = self._add_image("second", [self.miku_id], size=(900, 900))
        self.assertNotEqual(self.image_id, second_image_id)
        context = _Context([RuntimeError("provider not ready"), self._payload()])
        service = self._service(context, mode="shadow")
        service.queue_image(self.image_id, platform="pixiv")
        service.queue_image(second_image_id, platform="pixiv")

        summary = await service.run_once(force=True, max_runs=3)

        self.assertEqual(1, summary["processed"])
        self.assertEqual(1, summary["retried"])
        first = self.db.get_latest_llm_image_review_run(self.image_id, completed_only=False)
        second = self.db.get_latest_llm_image_review_run(second_image_id, completed_only=False)
        self.assertEqual("pending", str(first["status"]))
        self.assertEqual(1, int(first["attempt_count"]))
        self.assertEqual("pending", str(second["status"]))
        self.assertEqual(0, int(second["attempt_count"]))
        self.assertEqual(1, len(context.calls))

    async def test_local_low_resolution_skips_model_and_keeps_manual_review(self) -> None:
        small_id = self._add_image("small", [self.miku_id], size=(96, 96))
        context = _Context([])
        service = self._service(context, mode="auto_approve", llm_image_review_min_side=512)
        service.queue_image(small_id, platform="pixiv")

        summary = await service.run_once(force=True, max_runs=1)

        self.assertEqual(1, summary["manual_review"])
        self.assertEqual([], context.calls)
        run = self.db.get_latest_llm_image_review_run(small_id)
        result = json.loads(str(run["result_json"]))
        self.assertIn("low_resolution", result["policy"]["blocking_flags"])

    async def test_new_service_disables_legacy_per_tag_llm_call(self) -> None:
        context = _Context([self._payload()])
        reviewer = ReviewService(
            context,
            self.db,
            {
                "llm_image_review_enabled": True,
                "enable_auto_review": True,
                "review_provider_id": "legacy-provider",
            },
        )
        image = self.db.get_llm_review_image(self.image_id)

        decision = await reviewer.review_image_for_tag(Path(str(image["file_path"])), "初音未来")

        self.assertEqual("pending", decision.status)
        self.assertIn("候选 tag", decision.reason)
        self.assertEqual([], context.calls)

    def test_strict_parser_rejects_loose_text_unknown_flags_and_boolean_scores(self) -> None:
        with self.assertRaises(LlmImageReviewContractError):
            LlmImageReviewService.parse_response(
                "yes, this is fine",
                candidate_ids={self.miku_id},
            )
        with self.assertRaises(LlmImageReviewContractError):
            LlmImageReviewService.parse_response(
                self._payload(flags=["invented_flag"]),
                candidate_ids={self.miku_id},
            )
        payload = json.loads(self._payload())
        payload["quality"]["overall"] = True
        with self.assertRaises(LlmImageReviewContractError):
            LlmImageReviewService.parse_response(
                json.dumps(payload, ensure_ascii=False),
                candidate_ids={self.miku_id},
            )
        with self.assertRaises(LlmImageReviewContractError):
            LlmImageReviewService.parse_response(
                "```json\n" + self._payload() + "\n```",
                candidate_ids={self.miku_id},
            )

    def test_prompt_lists_every_parser_flag_and_no_match_contract(self) -> None:
        candidates = self.db.get_llm_review_candidates(self.image_id)
        prompt = LlmImageReviewService._build_prompt(candidates)
        for flag in VALID_FLAGS:
            self.assertIn(flag, prompt)
        self.assertIn("characters 必须返回空数组", prompt)
        self.assertIn(str(self.miku_id), prompt)
        self.assertEqual(30, self._service(_Context([])).startup_delay_seconds())


if __name__ == "__main__":
    unittest.main()
