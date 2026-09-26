from __future__ import annotations

import importlib
import asyncio
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
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

    def test_only_complete_json_fences_are_unwrapped(self):
        raw = audit_text()
        result = audit_module.parse_audit_response('```json\n' + raw + '\n```', candidates=CANDIDATES)
        self.assertTrue(result['fence_removed'])
        for wrapped in ['说明：' + raw, raw + '\n解释', '```json\n' + raw + '\n```\n还有说明']:
            with self.subTest(wrapped=wrapped), self.assertRaises(LlmImageReviewContractError):
                audit_module.parse_audit_response(wrapped, candidates=CANDIDATES)

    def test_confidence_is_not_clamped_or_fabricated(self):
        for value in [95, -0.1, True, None, '0.95', float('nan'), float('inf')]:
            with self.subTest(value=value), self.assertRaises(LlmImageReviewContractError):
                audit_module.parse_audit_response(
                    audit_text(characters=[{'tag_id': 1, 'confidence': value}]), candidates=CANDIDATES)

    def test_scores_must_be_finite_and_in_range(self):
        for value in [float('nan'), float('inf'), -1, 101, True]:
            raw = json.loads(audit_text())
            raw['quality']['overall'] = value
            with self.subTest(value=value), self.assertRaises(LlmImageReviewContractError):
                audit_module.parse_audit_response(json.dumps(raw), candidates=CANDIDATES)

    def test_identity_evidence_is_bounded_and_ids_remain_compatible(self):
        parsed = audit_module.parse_audit_response(audit_text(characters=[
            {'tag_id': 1, 'confidence': .87, 'evidence': '可见线索' * 100},
        ]), candidates=CANDIDATES)
        self.assertEqual([1], parsed['characters'])
        self.assertEqual(.87, parsed['identities'][0]['confidence'])
        self.assertEqual(160, len(parsed['identities'][0]['evidence']))

    def test_contract_categories_are_distinct(self):
        cases = [
            (audit_text(characters=[{'tag_id': 99, 'confidence': .9}]), 'unknown_tag_id'),
            (audit_text(characters=[{'tag_id': 1, 'confidence': .9}] * 2), 'duplicate_tag_id'),
            (audit_text(reason=''), 'missing_reason'),
            ('{"quality": {}, "quality": {}}', 'duplicate_json_key'),
        ]
        for raw, category in cases:
            with self.subTest(category=category), self.assertRaises(audit_module.AuditContractError) as err:
                audit_module.parse_audit_response(raw, candidates=CANDIDATES)
            self.assertEqual(category, err.exception.category)


class AuditServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_modes_preserve_call_count_and_trace_version(self):
        for mode in ('off', 'text'):
            candidate = self.make_candidate(ref=mode)
            service, context = self.make_service()
            service.config['chat_image_audit_profile_mode'] = mode
            await service.run_once()
            trace = self.db.get_chat_image_candidate(candidate['id'])['audit_identity_json']
            self.assertEqual(1, context.calls)
            self.assertEqual(mode == 'off', trace['profile_version'] == 'none')

    async def test_invalid_profile_stops_before_model_call(self):
        candidate = self.make_candidate()
        service, context = self.make_service()
        service.config['chat_image_audit_profile_mode'] = 'text'
        with patch.object(audit_module, 'build_profile_context', side_effect=ValueError('bad profile')):
            await service.run_once()
        trace = self.db.get_chat_image_candidate(candidate['id'])['audit_identity_json']
        self.assertEqual('profile_config', trace['last_error_category'])
        self.assertEqual(0, context.calls)

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

    def allow_retry_now(self, candidate_id):
        with self.db._connect() as c:
            c.execute("update chat_image_candidates set audit_identity_json = "
                      "json_set(audit_identity_json, '$.next_retry_at', '') where id=?", (candidate_id,))

    async def test_low_confidence_shadow_preserves_current_confirmation(self):
        candidate = self.make_candidate()
        service, context = self.make_service(text=audit_text(characters=[
            {'tag_id': 1, 'confidence': .2, 'evidence': '轮廓可见但细节不足'},
        ]))
        await service.run_once()
        row = self.db.get_chat_image_candidate(candidate['id'])
        self.assertEqual('audited', row['status'])
        trace = row['audit_identity_json']
        self.assertEqual(.2, trace['characters'][0]['confidence'])
        self.assertEqual('pending_review', trace['shadow']['suggested_action'])
        self.assertFalse(trace['shadow']['applied'])
        self.assertEqual(1, trace['call_count'])
        self.assertEqual(1, context.calls)
        self.assertTrue(trace['prompt_version'].endswith('/a1'))
        self.assertEqual(64, len(trace['prompt_sha256']))
        self.assertIn('total_call_ms', trace)

    async def test_high_confidence_shadow_and_off_mode(self):
        first = self.make_candidate(ref='first')
        service, _ = self.make_service()
        await service.run_once()
        trace = self.db.get_chat_image_candidate(first['id'])['audit_identity_json']
        self.assertEqual('ask_confirmation', trace['shadow']['suggested_action'])
        second = self.make_candidate(ref='second')
        service.config['chat_image_audit_identity_mode'] = 'off'
        await service.run_once()
        trace = self.db.get_chat_image_candidate(second['id'])['audit_identity_json']
        self.assertEqual('unknown', trace['shadow']['suggested_action'])
        self.assertEqual(.95, trace['characters'][0]['confidence'])

    async def test_missing_file_stops_without_a_model_call(self):
        candidate = self.make_candidate()
        service, context = self.make_service()
        def missing(_image):
            raise FileNotFoundError('gone')
        service.review_service._prepare_preview = missing
        result = await service.run_once()
        row = self.db.get_chat_image_candidate(candidate['id'])
        self.assertEqual('audit_error', row['status'])
        self.assertEqual('missing_file', row['audit_identity_json']['last_error_category'])
        self.assertEqual(0, row['audit_identity_json']['call_count'])
        self.assertEqual(0, context.calls)
        self.assertEqual(1, result['failed'])

    async def test_fenced_result_succeeds_without_extra_call(self):
        candidate = self.make_candidate()
        service, context = self.make_service(text='```json\n' + audit_text() + '\n```')
        await service.run_once()
        row = self.db.get_chat_image_candidate(candidate['id'])
        self.assertEqual('audited', row['status'])
        self.assertTrue(row['audit_identity_json']['fence_removed'])
        self.assertEqual(1, context.calls)

    async def test_retry_backoff_does_not_starve_later_candidates(self):
        first = self.make_candidate(ref='first')
        service, context = self.make_service(text='invalid')
        await service.run_once()
        trace = self.db.get_chat_image_candidate(first['id'])['audit_identity_json']
        self.assertEqual('invalid_json', trace['last_error_category'])
        self.assertTrue(trace['next_retry_at'])
        second = self.make_candidate(ref='second')
        context.text = audit_text()
        await service.run_once()
        self.assertEqual('captured', self.db.get_chat_image_candidate(first['id'])['status'])
        self.assertEqual('audited', self.db.get_chat_image_candidate(second['id'])['status'])
        self.assertEqual(2, context.calls)

    async def test_budget_survives_restart_and_manual_requeue(self):
        candidate = self.make_candidate()
        service, context = self.make_service(text='invalid')
        service.review_service.max_attempts = lambda: 2
        await service.run_once()
        self.allow_retry_now(candidate['id'])
        self.db = db_module.ImageIndexDB(Path(self.tmp.name) / 'test.db')
        restarted, new_context = self.make_service(text='invalid')
        restarted.review_service.max_attempts = lambda: 2
        await restarted.run_once()
        row = self.db.get_chat_image_candidate(candidate['id'])
        self.assertEqual('audit_error', row['status'])
        self.assertEqual(2, row['audit_identity_json']['call_count'])
        self.db.retry_chat_image_candidate_audit(candidate['id'])
        await restarted.run_once()
        row = self.db.get_chat_image_candidate(candidate['id'])
        self.assertEqual('budget_exhausted', row['audit_identity_json']['last_error_category'])
        self.assertEqual(1, context.calls)
        self.assertEqual(1, new_context.calls)

    async def test_cancelled_call_consumes_budget_before_restart(self):
        candidate = self.make_candidate()
        service, context = self.make_service()
        service.review_service.max_attempts = lambda: 1
        entered = asyncio.Event()
        async def hang(**kwargs):
            context.calls += 1
            entered.set()
            await asyncio.Event().wait()
        context.llm_generate = hang
        task = asyncio.create_task(service.run_once())
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.db.reset_running_chat_image_candidates()
        await service.run_once()
        row = self.db.get_chat_image_candidate(candidate['id'])
        self.assertEqual('audit_error', row['status'])
        self.assertEqual(1, context.calls)
        self.assertEqual(1, row['audit_identity_json']['call_count'])

    async def test_error_categories_and_permanent_auth_failure(self):
        for index, (status, category, retried) in enumerate([
            (429, 'rate_limit', True), (403, 'authentication', False),
            (400, 'invalid_request', False), (503, 'upstream', True),
            (None, 'timeout', True),
        ]):
            candidate = self.make_candidate(ref=f'error-{index}')
            service, context = self.make_service()
            async def fail(**kwargs):
                context.calls += 1
                if status is None:
                    raise TimeoutError()
                exc = RuntimeError('provider unavailable')
                exc.status_code = status
                raise exc
            context.llm_generate = fail
            await service.run_once()
            row = self.db.get_chat_image_candidate(candidate['id'])
            self.assertEqual(category, row['audit_identity_json']['last_error_category'])
            self.assertEqual('captured' if retried else 'audit_error', row['status'])
            self.assertEqual(1, context.calls)

    async def test_legacy_identity_unknown_and_manual_correction_preserves_prediction(self):
        candidate = self.make_candidate()
        self.assertEqual({}, candidate['audit_identity_json'])
        service, _ = self.make_service()
        await service.run_once()
        before = self.db.get_chat_image_candidate(candidate['id'])['audit_identity_json']
        self.db.mark_chat_image_candidates_asked([candidate['id']], confirm_message_id='1',
                                                expires_at='2999-01-01T00:00:00+00:00')
        self.db.mark_chat_image_candidate_corrected(candidate['id'], corrected_tag_ids=[2], user_id='u1')
        row = self.db.get_chat_image_candidate(candidate['id'])
        self.assertEqual(before, row['audit_identity_json'])
        self.assertEqual([2], row['confirmed_tag_ids_json'])

    async def test_expired_subscription_is_not_retried_as_recognition_failure(self):
        candidate = self.make_candidate()
        service, context = self.make_service()
        async def invalid_subscription(**kwargs):
            context.calls += 1
            raise RuntimeError('Error code: 400 InvalidSubscription: subscription has expired')
        context.llm_generate = invalid_subscription
        await service.run_once()
        row = self.db.get_chat_image_candidate(candidate['id'])
        self.assertEqual('audit_error', row['status'])
        self.assertEqual('subscription_inactive', row['audit_identity_json']['last_error_category'])
        self.assertFalse(row['audit_identity_json']['retryable'])
        await service.run_once()
        self.assertEqual(1, context.calls)

    async def test_old_schema_migrates_without_changing_candidates(self):
        path = Path(self.tmp.name) / 'old.db'
        old = db_module.ImageIndexDB(path)
        row = old.create_chat_image_candidate(ref='old', session_id='s', file_path='x')
        with sqlite3.connect(path) as c:
            c.execute('alter table chat_image_candidates drop column audit_identity_json')
        migrated = db_module.ImageIndexDB(path)
        migrated_row = migrated.get_chat_image_candidate(row['id'])
        self.assertEqual({}, migrated_row['audit_identity_json'])
        self.assertEqual('captured', migrated_row['status'])

    async def test_trace_storage_rejects_unbounded_and_nonfinite_payloads(self):
        with self.assertRaises(ValueError):
            self.db._audit_identity_dump({'evidence': '图' * 20000})
        with self.assertRaises(ValueError):
            self.db._audit_identity_dump({'confidence': float('nan')})

    async def test_concurrent_database_instances_claim_only_once(self):
        candidate = self.make_candidate()
        second_db = db_module.ImageIndexDB(Path(self.tmp.name) / 'test.db')
        rows = await asyncio.gather(asyncio.to_thread(self.db.claim_next_chat_image_candidate),
                                    asyncio.to_thread(second_db.claim_next_chat_image_candidate))
        self.assertEqual([candidate['id']], [r['id'] for r in rows if r])
        reserved = await asyncio.gather(
            asyncio.to_thread(self.db.reserve_chat_image_audit_call, candidate['id'], max_calls=1, metadata={}),
            asyncio.to_thread(second_db.reserve_chat_image_audit_call, candidate['id'], max_calls=1, metadata={}),
        )
        self.assertEqual(1, sum(r is not None for r in reserved))

    async def test_group_provider_override_is_independent(self):
        candidate = self.make_candidate()
        service, context = self.make_service()
        service.config['chat_image_audit_provider_id'] = 'deepseek/deepseek-v4-flash'
        self.assertEqual('provider-1', service.review_service.provider_id())
        self.assertEqual('deepseek/deepseek-v4-flash', service.provider_id())
        await service.run_once()
        row = self.db.get_chat_image_candidate(candidate['id'])
        self.assertEqual('deepseek/deepseek-v4-flash', row['audit_provider'])
        self.assertEqual('deepseek/deepseek-v4-flash', row['audit_identity_json']['requested_provider'])
        self.assertEqual(1, context.calls)

    async def test_astrbot_token_usage_is_recorded_without_fabricating_missing_usage(self):
        candidate = self.make_candidate()
        service, context = self.make_service()
        async def with_usage(**kwargs):
            response = FakeResponse(audit_text())
            response.usage = types.SimpleNamespace(input=120, output=15, input_cached=80)
            response.raw_completion = types.SimpleNamespace(model='test-vision', usage=object())
            return response
        context.llm_generate = with_usage
        await service.run_once()
        trace = self.db.get_chat_image_candidate(candidate['id'])['audit_identity_json']
        self.assertEqual({'input_tokens': 120, 'output_tokens': 15, 'cached_input_tokens': 80}, trace['usage'])
        self.assertEqual('test-vision', trace['reported_model'])
        missing = self.make_candidate(ref='missing-usage')
        async def without_usage(**kwargs):
            response = await with_usage(**kwargs)
            response.raw_completion.usage = None
            return response
        context.llm_generate = without_usage
        await service.run_once()
        self.assertEqual({}, self.db.get_chat_image_candidate(missing['id'])['audit_identity_json']['usage'])


if __name__ == "__main__":
    unittest.main()
