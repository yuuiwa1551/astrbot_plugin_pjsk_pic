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
PACKAGE_NAME = "pjsk_pic_reliability_test_core"


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
pixiv_api_module = importlib.import_module(f"{PACKAGE_NAME}.pixiv_app_api")
search_module = importlib.import_module(f"{PACKAGE_NAME}.pixiv_search_service")
auto_module = importlib.import_module(f"{PACKAGE_NAME}.auto_crawl_service")

ImageIndexDB = db_module.ImageIndexDB
PixivAppClient = pixiv_api_module.PixivAppClient
PixivSearchHit = search_module.PixivSearchHit
AutoCrawlService = auto_module.AutoCrawlService


class FakeResponse:
    def __init__(self, payload: dict, *, status_code: int = 200, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = json.dumps(payload, ensure_ascii=False)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def request(self, method: str, url: str, **_kwargs) -> FakeResponse:
        self.calls.append((method, url))
        if "oauth.secure.pixiv.net" in url:
            return FakeResponse(
                {
                    "response": {
                        "access_token": "access-1",
                        "refresh_token": "refresh-2",
                        "expires_in": 3600,
                        "user": {"id": "1"},
                    }
                }
            )
        if "/v1/search/illust" in url:
            return FakeResponse({"illusts": [], "next_url": None})
        if "/v1/illust/detail" in url:
            return FakeResponse({"illust": {"id": 123}})
        raise AssertionError(f"unexpected URL: {url}")

    def close(self) -> None:
        self.closed = True


class FailingSession(FakeSession):
    def request(self, method: str, url: str, **_kwargs) -> FakeResponse:
        self.calls.append((method, url))
        raise requests.ConnectionError("tls eof")


class ExpiredAccessTokenSession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.auth_count = 0
        self.search_count = 0

    def request(self, method: str, url: str, **_kwargs) -> FakeResponse:
        self.calls.append((method, url))
        if "oauth.secure.pixiv.net" in url:
            self.auth_count += 1
            return FakeResponse(
                {
                    "response": {
                        "access_token": f"access-{self.auth_count}",
                        "refresh_token": f"refresh-{self.auth_count + 1}",
                        "expires_in": 3600,
                    }
                }
            )
        if "/v1/search/illust" in url:
            self.search_count += 1
            if self.search_count == 1:
                return FakeResponse({"error": "expired"}, status_code=401)
            return FakeResponse({"illusts": [], "next_url": None})
        raise AssertionError(f"unexpected URL: {url}")


class FakeSearchService:
    def __init__(self, responses: dict[str, list[PixivSearchHit]]) -> None:
        self.responses = responses

    def refresh_token(self) -> str:
        return "configured"

    @staticmethod
    def build_query(tag_name: str, *, suffix: str | None = None) -> str:
        del suffix
        return str(tag_name)

    async def search_tag(self, tag_name: str, **_kwargs) -> list[PixivSearchHit]:
        return list(self.responses.get(tag_name, []))


class FakeCrawlService:
    def __init__(self) -> None:
        self.jobs: dict[str, tuple[int, list[str]]] = {}

    async def submit_job_once(
        self,
        _platform: str,
        source_url: str,
        tags: list[str],
        **_kwargs,
    ) -> tuple[int, bool]:
        existing = self.jobs.get(source_url)
        if existing:
            job_id, existing_tags = existing
            for tag in tags:
                if tag not in existing_tags:
                    existing_tags.append(tag)
            return job_id, False
        job_id = len(self.jobs) + 1
        self.jobs[source_url] = (job_id, list(tags))
        return job_id, True


def hit(illust_id: str, *raw_tags: str) -> PixivSearchHit:
    return PixivSearchHit(
        illust_id=illust_id,
        post_url=f"https://www.pixiv.net/artworks/{illust_id}",
        raw_tags=list(raw_tags),
        translated_tags=[],
    )


class PixivClientTests(unittest.TestCase):
    def test_access_token_and_session_are_reused(self) -> None:
        session = FakeSession()
        client = PixivAppClient(
            {"pixiv_refresh_token": "refresh-1"},
            session=session,
            sleep_func=lambda _seconds: None,
            random_func=lambda _start, _end: 0.0,
            clock=lambda: 1_000.0,
        )

        client.search_illusts("初音ミク")
        client.search_illusts("鏡音リン")
        client.fetch_illust_detail("123")

        auth_calls = [url for _method, url in session.calls if "oauth.secure.pixiv.net" in url]
        self.assertEqual(1, len(auth_calls))
        self.assertEqual(4, len(session.calls))
        client.close()
        self.assertTrue(session.closed)

    def test_auth_circuit_stops_repeated_tls_failures(self) -> None:
        session = FailingSession()
        client = PixivAppClient(
            {"pixiv_refresh_token": "refresh-1", "platform_retry_times": 1},
            session=session,
            sleep_func=lambda _seconds: None,
            random_func=lambda _start, _end: 0.0,
            clock=lambda: 1_000.0,
        )

        with self.assertRaisesRegex(Exception, "tls eof"):
            client.search_illusts("初音ミク")
        request_count = len(session.calls)
        self.assertEqual(1, request_count)
        with self.assertRaisesRegex(Exception, "熔断"):
            client.search_illusts("镜音铃")
        self.assertEqual(request_count, len(session.calls))
        client.close()

    def test_unauthorized_response_refreshes_token_once(self) -> None:
        session = ExpiredAccessTokenSession()
        client = PixivAppClient(
            {"pixiv_refresh_token": "refresh-1"},
            session=session,
            sleep_func=lambda _seconds: None,
            random_func=lambda _start, _end: 0.0,
            clock=lambda: 1_000.0,
        )

        client.search_illusts("初音ミク")

        self.assertEqual(2, session.auth_count)
        self.assertEqual(2, session.search_count)
        client.close()


class AutoCrawlReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = ImageIndexDB(Path(self.temp_dir.name) / "image_index.db")
        self.config = {
            "pixiv_refresh_token": "configured",
            "pixiv_auto_crawl_enabled": True,
            "pixiv_auto_crawl_character_only": True,
            "pixiv_auto_crawl_interval_minutes": 60,
            "pixiv_auto_crawl_query_suffix": "",
            "pixiv_auto_crawl_max_results_per_tag": 30,
            "pixiv_auto_crawl_max_pages_per_tag": 3,
            "pixiv_auto_crawl_max_new_jobs_per_cycle": 1,
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _service(self, responses: dict[str, list[PixivSearchHit]]) -> tuple[AutoCrawlService, FakeCrawlService]:
        crawl_service = FakeCrawlService()
        service = AutoCrawlService(db=self.db, crawl_service=crawl_service, config=self.config)
        service.search_service = FakeSearchService(responses)
        return service, crawl_service

    def _create_character(self, name: str = "初音未来") -> int:
        return self.db.get_or_create_tag(name, is_character=True)

    async def test_multiple_query_terms_keep_independent_cursors(self) -> None:
        self._create_character()
        ok, message = self.db.add_platform_term("初音未来", "hatsune", platform="pixiv", term_type="both")
        self.assertTrue(ok, message)
        service, _crawl_service = self._service(
            {
                "初音ミク": [hit("new-primary", "初音ミク"), hit("old-primary", "初音ミク")],
                "hatsune": [hit("new-secondary", "hatsune"), hit("old-secondary", "hatsune")],
            }
        )
        service._sync_subscriptions()
        subscription = self.db.list_crawl_subscriptions(platform="pixiv", enabled_only=True)[0]
        terms = self.db.list_crawl_subscription_terms(int(subscription["id"]))
        self.assertEqual(["初音ミク", "hatsune"], [str(row["query_term"]) for row in terms])
        self.db.update_crawl_subscription_term_state(int(terms[0]["id"]), last_seen_source_uid="old-primary")
        self.db.update_crawl_subscription_term_state(int(terms[1]["id"]), last_seen_source_uid="old-secondary")

        result = await service._process_subscription(subscription)

        self.assertEqual(2, result["discovered"])
        updated_terms = self.db.list_crawl_subscription_terms(int(subscription["id"]))
        self.assertEqual(
            ["new-primary", "new-secondary"],
            [str(row["last_seen_source_uid"]) for row in updated_terms],
        )
        self.assertEqual({"new-primary", "new-secondary"}, {
            str(row["source_uid"])
            for row in self.db.list_pending_crawl_discoveries(platform="pixiv", limit=10)
        })

    async def test_job_quota_keeps_remaining_discoveries_for_next_cycle(self) -> None:
        self._create_character()
        responses = {
            "初音ミク": [
                hit("300", "初音ミク"),
                hit("200", "初音ミク"),
                hit("100", "初音ミク"),
            ]
        }
        service, crawl_service = self._service(responses)

        first = await service.run_once(force=True)
        self.assertEqual(3, first["discovered"])
        self.assertEqual(1, first["queued"])
        self.assertEqual(2, len(self.db.list_pending_crawl_discoveries(platform="pixiv", limit=10)))

        second = await service.run_once(force=True)
        self.assertEqual(0, second["discovered"])
        self.assertEqual(1, second["queued"])
        self.assertEqual(1, len(self.db.list_pending_crawl_discoveries(platform="pixiv", limit=10)))

        third = await service.run_once(force=True)
        self.assertEqual(1, third["queued"])
        self.assertEqual(0, len(self.db.list_pending_crawl_discoveries(platform="pixiv", limit=10)))
        self.assertEqual(3, len(crawl_service.jobs))

    async def test_legacy_cursor_seeds_only_primary_term(self) -> None:
        tag_id = self._create_character()
        subscription_id = self.db.upsert_crawl_subscription(
            platform="pixiv",
            tag_id=tag_id,
            tag_name="初音未来",
            query_text="初音ミク",
            enabled=True,
        )
        self.db.update_crawl_subscription_state(subscription_id, last_seen_source_uid="legacy")

        terms = self.db.sync_crawl_subscription_terms(
            subscription_id,
            [("初音ミク", "初音ミク"), ("hatsune", "hatsune")],
        )

        self.assertEqual("legacy", str(terms[0]["last_seen_source_uid"]))
        self.assertEqual("", str(terms[1]["last_seen_source_uid"]))


class CrawlJobIdempotencyTests(unittest.TestCase):
    def test_same_pixiv_url_reuses_job_and_merges_tags(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = ImageIndexDB(Path(temp_dir) / "image_index.db")
            first_id, first_created = db.get_or_create_crawl_job(
                "pixiv",
                "https://www.pixiv.net/artworks/123",
                ["初音未来"],
            )
            second_id, second_created = db.get_or_create_crawl_job(
                "pixiv",
                "https://www.pixiv.net/artworks/123/",
                ["镜音铃"],
            )

            self.assertTrue(first_created)
            self.assertFalse(second_created)
            self.assertEqual(first_id, second_id)
            row = db.get_crawl_job(first_id)
            self.assertEqual({"初音未来", "镜音铃"}, set(str(row["tags_text"]).split(',')))
            self.assertEqual(1, sum(db.count_crawl_jobs_by_status().values()))


if __name__ == "__main__":
    unittest.main()
