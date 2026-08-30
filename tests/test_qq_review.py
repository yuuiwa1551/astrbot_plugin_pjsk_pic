from __future__ import annotations

import importlib
import gc
import sys
import tempfile
import types
import unittest
import warnings
from pathlib import Path


CORE_DIR = Path(__file__).resolve().parents[1] / "core"
PACKAGE_NAME = "pjsk_pic_test_core"
warnings.filterwarnings("ignore", message="unclosed database", category=ResourceWarning)
if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(CORE_DIR)]
    sys.modules[PACKAGE_NAME] = package

ImageIndexDB = importlib.import_module(f"{PACKAGE_NAME}.db").ImageIndexDB
QQReviewSessionService = importlib.import_module(
    f"{PACKAGE_NAME}.qq_review_service"
).QQReviewSessionService


class QQReviewTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        warnings.simplefilter("ignore", ResourceWarning)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = ImageIndexDB(self.root / "image_index.db")
        self.clock_value = 1000.0

    def tearDown(self) -> None:
        gc.collect()
        self.temp_dir.cleanup()

    def _clock(self) -> float:
        return self.clock_value

    def _add_pending_image(self, name: str, tags: list[str], *, platform: str = "pixiv") -> int:
        file_path = self.root / f"{name}.jpg"
        file_path.write_bytes(f"image-{name}".encode("utf-8"))
        image_id = self.db.upsert_image(
            file_path=str(file_path),
            file_name=file_path.name,
            sha256=f"sha-{name}",
            width=100,
            height=100,
            format_="jpeg",
        )
        post_url = (
            f"https://www.xiaohongshu.com/explore/{image_id}"
            if platform == "xiaohongshu"
            else f"https://www.pixiv.net/artworks/{image_id}"
        )
        image_url = (
            f"https://sns-webpic-qc.xhscdn.com/{image_id}.webp"
            if platform == "xiaohongshu"
            else f"https://i.pximg.net/{image_id}.jpg"
        )
        self.db.upsert_source(
            image_id,
            platform,
            post_url,
            image_url,
            author="tester",
            raw_tags=tags,
            extra_json={"translated_tags": tags},
        )
        for tag_name in tags:
            tag_id = self.db.get_or_create_tag(tag_name, is_character=True)
            self.db.link_image_tag(
                image_id,
                tag_id,
                source_type=f"crawl:{platform}",
                review_status="pending",
            )
            self.db.create_review_task(image_id, tag_id, "pending", reason="test")
        return image_id

    def _service(self, **config):
        return QQReviewSessionService(
            self.db,
            {
                "qq_review_claim_ttl_seconds": 60,
                "qq_review_recent_count": 10,
                **config,
            },
            clock=self._clock,
        )

    def test_random_query_supports_tag_filter_and_exclusion(self) -> None:
        miku_id = self._add_pending_image("miku", ["初音未来"])
        mizuki_id = self._add_pending_image("mizuki", ["晓山瑞希"])
        mizuki_tag = self.db.get_tag_row("晓山瑞希")
        self.assertIsNotNone(mizuki_tag)

        row = self.db.get_random_pixiv_review_image(candidate_tag_id=int(mizuki_tag["id"]))
        self.assertEqual(mizuki_id, int(row["image_id"]))
        row = self.db.get_random_pixiv_review_image(exclude_image_ids=[mizuki_id])
        self.assertEqual(miku_id, int(row["image_id"]))
        self.assertEqual(2, self.db.count_open_pixiv_review_images())
        self.assertEqual(
            1,
            self.db.count_open_pixiv_review_images(candidate_tag_id=int(mizuki_tag["id"])),
        )

    async def test_claims_are_isolated_and_expire(self) -> None:
        image_id = self._add_pending_image("only", ["初音未来"])
        service = self._service()
        first, _ = await service.claim_next(origin="group:1", reviewer_id="10001")
        second, _ = await service.claim_next(origin="group:1", reviewer_id="10002")
        self.assertEqual(image_id, first.image_id)
        self.assertIsNone(second)

        self.clock_value += 61
        second, _ = await service.claim_next(origin="group:1", reviewer_id="10002")
        self.assertEqual(image_id, second.image_id)

    async def test_approve_current_applies_image_level_review(self) -> None:
        image_id = self._add_pending_image("approve", ["初音未来", "镜音铃"])
        service = self._service()
        session, _ = await service.claim_next(origin="group:1", reviewer_id="10001")
        self.assertEqual(image_id, session.image_id)

        ok, result = await service.approve_current(
            origin="group:1",
            reviewer_id="10001",
            tag_name="初音未来",
        )
        self.assertTrue(ok, result)
        statuses = {
            str(row["tag_name"]): str(row["status"])
            for row in self.db.get_review_tasks_for_image(image_id)
        }
        self.assertEqual("manual_approved", statuses["初音未来"])
        self.assertEqual("manual_rejected", statuses["镜音铃"])
        self.assertFalse(self.db.is_open_pixiv_review_image(image_id))
        self.assertIsNone(
            await service.get_current(origin="group:1", reviewer_id="10001")
        )

    async def test_reject_current_marks_source_and_all_tasks(self) -> None:
        image_id = self._add_pending_image("reject", ["晓山瑞希", "东云绘名"])
        service = self._service()
        await service.claim_next(origin="group:1", reviewer_id="10001")

        ok, result = await service.reject_current(
            origin="group:1",
            reviewer_id="10001",
            reason="质量不足",
        )
        self.assertTrue(ok, result)
        statuses = {
            str(row["status"])
            for row in self.db.get_review_tasks_for_image(image_id)
        }
        self.assertEqual({"manual_rejected"}, statuses)
        self.assertTrue(
            self.db.is_rejected_source_post_url(
                f"https://www.pixiv.net/artworks/{image_id}",
                platform="pixiv",
            )
        )

    async def test_stale_claim_cannot_overwrite_completed_review(self) -> None:
        image_id = self._add_pending_image("stale", ["初音未来"])
        service = self._service()
        await service.claim_next(origin="group:1", reviewer_id="10001")
        ok, _ = self.db.apply_image_review(
            image_id,
            selected_tag_names=["初音未来"],
            source_terms=[],
            platform="pixiv",
        )
        self.assertTrue(ok)

        ok, result = await service.approve_current(
            origin="group:1",
            reviewer_id="10001",
            tag_name="初音未来",
        )
        self.assertFalse(ok)
        self.assertEqual("stale_session", result["code"])

    async def test_xiaohongshu_review_is_claimed_and_approved_in_qq_flow(self) -> None:
        image_id = self._add_pending_image(
            "xhs",
            ["初音未来"],
            platform="xiaohongshu",
        )
        service = self._service()
        session, remaining = await service.claim_next(
            origin="group:1",
            reviewer_id="10001",
            platform="xiaohongshu",
        )
        self.assertIsNotNone(session)
        self.assertEqual(image_id, session.image_id)
        self.assertEqual("xiaohongshu", session.platform)
        self.assertEqual(1, remaining)

        ok, result = await service.approve_current(
            origin="group:1",
            reviewer_id="10001",
            tag_name="初音未来",
        )
        self.assertTrue(ok, result)
        self.assertEqual("xiaohongshu", result["platform"])
        self.assertFalse(
            self.db.is_open_review_image(
                image_id,
                platform="xiaohongshu",
            )
        )


if __name__ == "__main__":
    unittest.main()
