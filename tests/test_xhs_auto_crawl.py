from __future__ import annotations

import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

import requests


CORE_DIR = Path(__file__).resolve().parents[1] / "core"
PACKAGE_NAME = "pjsk_pic_xhs_test_core"


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
provider_module = importlib.import_module(f"{PACKAGE_NAME}.xhs_provider")
adapter_module = importlib.import_module(f"{PACKAGE_NAME}.adapters.xiaohongshu_adapter")
auto_module = importlib.import_module(f"{PACKAGE_NAME}.xhs_auto_crawl_service")

ImageIndexDB = db_module.ImageIndexDB
XiaohongshuAdapter = adapter_module.XiaohongshuAdapter
XhsAutoCrawlService = auto_module.XhsAutoCrawlService
XhsImageRef = provider_module.XhsImageRef
XhsNoteDetail = provider_module.XhsNoteDetail
XhsProviderClient = provider_module.XhsProviderClient
XhsProviderError = provider_module.XhsProviderError
XhsSearchHit = provider_module.XhsSearchHit
normalize_xhs_image_url = provider_module.normalize_xhs_image_url


class FakeResponse:
    def __init__(self, payload: dict, *, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self.payload


class ContractSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.closed = False

    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if url.endswith("/health"):
            return FakeResponse(
                {"success": True, "data": {"status": "healthy", "version": "v2.5.0"}}
            )
        if url.endswith("/api/v1/login/status"):
            return FakeResponse({"success": True, "data": {"is_logged_in": True}})
        if url.endswith("/api/v1/feeds/search"):
            return FakeResponse(
                {
                    "success": True,
                    "data": {
                        "feeds": [
                            {
                                "id": "note-1",
                                "xsecToken": "temporary-context",
                                "modelType": "note",
                                "index": 0,
                                "noteCard": {
                                    "type": "normal",
                                    "displayTitle": "初音未来壁纸",
                                    "user": {"nickname": "tester"},
                                },
                            }
                        ]
                    },
                }
            )
        if url.endswith("/api/v1/feeds/detail"):
            images = [
                {
                    "width": 1080,
                    "height": 1440,
                    "urlDefault": f"http://sns-webpic-qc.xhscdn.com/path/image-{index}.webp",
                }
                for index in range(14)
            ]
            return FakeResponse(
                {
                    "success": True,
                    "data": {
                        "feed_id": "note-1",
                        "data": {
                            "note": {
                                "noteId": "note-1",
                                "xsecToken": "temporary-context",
                                "title": "初音未来壁纸",
                                "desc": "#初音未来[话题]# #PJSK[话题]#",
                                "type": "normal",
                                "time": 1_700_000_000_000,
                                "user": {"nickname": "tester"},
                                "imageList": images,
                            }
                        },
                    },
                }
            )
        raise AssertionError(f"unexpected URL: {url}")

    def close(self) -> None:
        self.closed = True


class RiskSession(ContractSession):
    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse(
            {
                "success": False,
                "error": "搜索Feeds失败",
                "code": "SEARCH_FEEDS_FAILED",
                "details": "300012 当前网络环境存在风控",
            }
        )


class EchoedSecretSession(ContractSession):
    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse(
            {
                "success": False,
                "error": "detail failed for super-secret-xsec and provider-secret",
                "code": "GET_FEED_DETAIL_FAILED",
            }
        )


class StaticDetailProvider:
    def __init__(self, detail: XhsNoteDetail) -> None:
        self.detail = detail
        self.calls = 0

    def fetch_note_detail(self, *_args, **_kwargs) -> XhsNoteDetail:
        self.calls += 1
        return self.detail


class FakeDiscoveryProvider:
    def __init__(self) -> None:
        self.search_calls: list[str] = []
        self.detail_calls: list[str] = []

    def health(self, **_kwargs):
        return {"status": "healthy", "version": "v2.5.0"}

    def login_status(self, **_kwargs) -> bool:
        return True

    def search_notes(self, keyword: str, **_kwargs) -> list[XhsSearchHit]:
        self.search_calls.append(keyword)
        return [
            XhsSearchHit(
                note_id="same-note",
                xsec_token="context-token",
                post_url="https://www.xiaohongshu.com/explore/same-note",
                title="初音未来与镜音铃",
                author="tester",
            )
        ]

    def fetch_note_detail(self, note_id: str, _token: str, **_kwargs) -> XhsNoteDetail:
        self.detail_calls.append(note_id)
        return XhsNoteDetail(
            note_id=note_id,
            xsec_token="context-token",
            post_url=f"https://www.xiaohongshu.com/explore/{note_id}",
            title="初音未来与镜音铃",
            description="#初音未来[话题]# #镜音铃[话题]#",
            author="tester",
            topics=["初音未来", "镜音铃"],
            images=[
                XhsImageRef(
                    url="https://sns-webpic-qc.xhscdn.com/path/one.webp",
                    index=1,
                    width=1080,
                    height=1440,
                )
            ],
        )


class RetryableFailureProvider(FakeDiscoveryProvider):
    def search_notes(self, keyword: str, **_kwargs) -> list[XhsSearchHit]:
        self.search_calls.append(keyword)
        raise XhsProviderError(
            "provider browser timed out",
            category="timeout",
            retryable=True,
        )


class FakeCrawlService:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}

    async def submit_job_once(
        self,
        _platform: str,
        source_url: str,
        tags: list[str],
        **kwargs,
    ) -> tuple[int, bool]:
        existing = self.jobs.get(source_url)
        if existing:
            for tag in tags:
                if tag not in existing["tags"]:
                    existing["tags"].append(tag)
            existing["source_context"].update(kwargs.get("source_context") or {})
            return int(existing["id"]), False
        job_id = len(self.jobs) + 1
        self.jobs[source_url] = {
            "id": job_id,
            "tags": list(tags),
            "source_context": dict(kwargs.get("source_context") or {}),
        }
        return job_id, True


class XhsProviderContractTests(unittest.TestCase):
    def test_search_contract_omits_unlimited_filters_and_detail_keeps_all_images(self) -> None:
        session = ContractSession()
        client = XhsProviderClient(
            {
                "xhs_provider_base_url": "http://provider:18060",
                "xhs_provider_min_interval_seconds": 0,
                "xhs_provider_access_token": "secret-token",
            },
            session=session,
            sleep_func=lambda _seconds: None,
            clock=lambda: 1000.0,
        )

        hits = client.search_notes("初音未来")
        self.assertEqual(1, len(hits))
        search_call = next(call for call in session.calls if call["url"].endswith("/feeds/search"))
        self.assertEqual("Bearer secret-token", search_call["headers"]["Authorization"])
        self.assertEqual(
            {"sort_by", "note_type", "publish_time"},
            set(search_call["json"]["filters"]),
        )
        detail = client.fetch_note_detail(hits[0].note_id, hits[0].xsec_token)
        self.assertEqual(14, len(detail.images))
        self.assertTrue(all(image.url.startswith("https://") for image in detail.images))
        self.assertEqual(["初音未来", "PJSK"], detail.topics)

    def test_risk_control_is_non_retryable_and_requires_pause(self) -> None:
        client = XhsProviderClient(
            {
                "xhs_provider_base_url": "http://provider:18060",
                "xhs_provider_min_interval_seconds": 0,
            },
            session=RiskSession(),
            sleep_func=lambda _seconds: None,
            clock=lambda: 1000.0,
        )
        with self.assertRaises(XhsProviderError) as raised:
            client.search_notes("初音未来")
        self.assertEqual("risk_control", raised.exception.category)
        self.assertFalse(raised.exception.retryable)
        self.assertTrue(raised.exception.pause_required)

    def test_unknown_image_domain_is_rejected(self) -> None:
        with self.assertRaises(XhsProviderError) as raised:
            normalize_xhs_image_url("https://example.com/not-an-xhs-image.webp")
        self.assertEqual("unsafe_image_url", raised.exception.category)
        self.assertTrue(raised.exception.pause_required)

    def test_provider_base_url_rejects_embedded_credentials(self) -> None:
        client = XhsProviderClient(
            {"xhs_provider_base_url": "http://user:password@provider:18060"},
            session=ContractSession(),
        )
        with self.assertRaises(XhsProviderError) as raised:
            client.base_url()
        self.assertEqual("configuration", raised.exception.category)

    def test_provider_error_redacts_access_and_xsec_tokens(self) -> None:
        client = XhsProviderClient(
            {
                "xhs_provider_base_url": "http://provider:18060",
                "xhs_provider_access_token": "provider-secret",
                "xhs_provider_min_interval_seconds": 0,
            },
            session=EchoedSecretSession(),
            sleep_func=lambda _seconds: None,
            clock=lambda: 1000.0,
        )
        with self.assertRaises(XhsProviderError) as raised:
            client.fetch_note_detail("note-1", "super-secret-xsec")
        message = str(raised.exception)
        self.assertNotIn("super-secret-xsec", message)
        self.assertNotIn("provider-secret", message)
        self.assertIn("<redacted>", message)


class XhsAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_detail_bypasses_generic_six_image_limit(self) -> None:
        detail = XhsNoteDetail(
            note_id="note-14",
            xsec_token="context",
            post_url="https://www.xiaohongshu.com/explore/note-14",
            title="十四张图",
            description="#初音未来[话题]#",
            author="tester",
            topics=["初音未来"],
            images=[
                XhsImageRef(
                    url=f"https://sns-webpic-qc.xhscdn.com/path/{index}.webp",
                    index=index,
                    width=1080,
                    height=1440,
                )
                for index in range(1, 15)
            ],
        )
        provider = StaticDetailProvider(detail)
        adapter = XiaohongshuAdapter(
            {"xhs_max_images_per_note": 60},
            provider_client=provider,
        )
        candidates = await adapter.fetch_candidates(
            detail.post_url,
            max_candidates=6,
            source_context={"note_id": detail.note_id, "xsec_token": "context"},
        )
        self.assertEqual(14, len(candidates))
        self.assertEqual(list(range(1, 15)), [item.extra["page_index"] for item in candidates])
        self.assertTrue(all(item.extra["require_image_mime"] for item in candidates))

    async def test_abnormal_image_count_stops_instead_of_truncating(self) -> None:
        detail = XhsNoteDetail(
            note_id="note-61",
            xsec_token="context",
            post_url="https://www.xiaohongshu.com/explore/note-61",
            images=[
                XhsImageRef(
                    url=f"https://sns-webpic-qc.xhscdn.com/path/{index}.webp",
                    index=index,
                )
                for index in range(1, 62)
            ],
        )
        adapter = XiaohongshuAdapter(
            {"xhs_max_images_per_note": 60},
            provider_client=StaticDetailProvider(detail),
        )
        with self.assertRaises(XhsProviderError) as raised:
            await adapter.fetch_candidates(
                detail.post_url,
                max_candidates=6,
                source_context={"note_id": detail.note_id, "xsec_token": "context"},
            )
        self.assertEqual("response_too_large", raised.exception.category)
        self.assertTrue(raised.exception.pause_required)


class XhsAutoCrawlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = ImageIndexDB(Path(self.temp_dir.name) / "image_index.db")
        self.db.get_or_create_tag("初音未来", is_character=True)
        self.db.get_or_create_tag("镜音铃", is_character=True)
        self.db.get_or_create_tag("无平台词", is_character=True)
        self.db.add_platform_term(
            "初音未来",
            "初音未来",
            platform="xiaohongshu",
            term_type="both",
        )
        self.db.add_platform_term(
            "镜音铃",
            "镜音铃",
            platform="xiaohongshu",
            term_type="both",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_explicit_terms_merge_same_note_and_repeat_is_idempotent(self) -> None:
        provider = FakeDiscoveryProvider()
        crawl_service = FakeCrawlService()
        service = XhsAutoCrawlService(
            db=self.db,
            crawl_service=crawl_service,
            config={
                "xhs_auto_crawl_enabled": True,
                "xhs_provider_timeout_seconds": 10,
                "xhs_auto_crawl_max_subscriptions_per_cycle": 3,
                "xhs_auto_crawl_max_queries_per_cycle": 5,
                "xhs_auto_crawl_max_details_per_cycle": 10,
                "xhs_auto_crawl_max_new_jobs_per_cycle": 10,
                "xhs_auto_crawl_seed_max_notes": 3,
            },
            provider_client=provider,
        )

        first = await service.run_once(force=True)
        self.assertEqual(1, first["queued"], first)
        self.assertEqual(1, len(crawl_service.jobs))
        job = next(iter(crawl_service.jobs.values()))
        self.assertEqual({"初音未来", "镜音铃"}, set(job["tags"]))
        self.assertEqual("same-note", job["source_context"]["note_id"])
        enabled_tags = {
            str(row["tag_name"])
            for row in self.db.list_crawl_subscriptions(
                platform="xiaohongshu",
                enabled_only=True,
            )
        }
        self.assertEqual({"初音未来", "镜音铃"}, enabled_tags)

        detail_count = len(provider.detail_calls)
        second = await service.run_once(force=True)
        self.assertEqual(0, second["queued"])
        self.assertEqual(detail_count, len(provider.detail_calls))
        self.assertEqual(1, len(crawl_service.jobs))

    async def test_retryable_provider_failure_stops_the_rest_of_the_cycle(self) -> None:
        provider = RetryableFailureProvider()
        service = XhsAutoCrawlService(
            db=self.db,
            crawl_service=FakeCrawlService(),
            config={
                "xhs_auto_crawl_enabled": True,
                "xhs_provider_timeout_seconds": 10,
                "xhs_auto_crawl_max_subscriptions_per_cycle": 3,
                "xhs_auto_crawl_max_queries_per_cycle": 5,
                "xhs_auto_crawl_max_details_per_cycle": 10,
            },
            provider_client=provider,
        )

        summary = await service.run_once(force=True)

        self.assertEqual(1, summary["checked"])
        self.assertEqual(1, summary["errors"])
        self.assertEqual(1, len(provider.search_calls))
        self.assertFalse(service.paused())
        state = service.state()
        self.assertIsNotNone(state)
        self.assertIn("timed out", str(state["last_error"]))


if __name__ == "__main__":
    unittest.main()
