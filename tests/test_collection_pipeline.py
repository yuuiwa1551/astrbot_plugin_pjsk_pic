from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_xhs_auto_crawl import PACKAGE_NAME, ImageIndexDB


crawl_module = importlib.import_module(f"{PACKAGE_NAME}.crawl_service")
models_module = importlib.import_module(f"{PACKAGE_NAME}.models")

CrawlService = crawl_module.CrawlService
CrawlCandidate = models_module.CrawlCandidate
ImportedImage = models_module.ImportedImage
ReviewDecision = models_module.ReviewDecision


class CollectionDatabaseTests(unittest.TestCase):
    def test_collection_indexes_and_checkpoint_columns_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = ImageIndexDB(Path(temp_dir) / "image_index.db")
            with db._connect() as conn:
                source_plan = " ".join(
                    str(row["detail"])
                    for row in conn.execute(
                        "EXPLAIN QUERY PLAN SELECT 1 FROM sources WHERE platform = ? AND post_url = ?",
                        ("pixiv", "https://example.invalid"),
                    )
                )
                job_plan = " ".join(
                    str(row["detail"])
                    for row in conn.execute(
                        "EXPLAIN QUERY PLAN SELECT id FROM crawl_jobs WHERE platform = ? AND source_url = ?",
                        ("pixiv", "https://example.invalid"),
                    )
                )
                term_columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(crawl_subscription_terms)")
                }
                job_columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(crawl_jobs)")
                }

            self.assertIn("idx_sources_platform_post_url", source_plan)
            self.assertIn("idx_crawl_jobs_platform_source_url", job_plan)
            self.assertTrue(
                {"scan_offset", "scan_high_watermark", "scan_target_source_uid"}
                <= term_columns
            )
            self.assertTrue({"origin", "priority"} <= job_columns)

    def test_commit_crawl_image_writes_source_tags_and_reviews_in_one_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = ImageIndexDB(root / "image_index.db")
            image_path = root / "image.jpg"
            image_path.write_bytes(b"image")
            image_id = db.upsert_image(
                file_path=str(image_path),
                file_name=image_path.name,
                sha256="sha-image",
                width=100,
                height=100,
                format_="jpeg",
            )
            miku_id = db.get_or_create_tag("初音未来", is_character=True)
            rin_id = db.get_or_create_tag("镜音铃", is_character=True)

            db.commit_crawl_image(
                image_id=image_id,
                platform="pixiv",
                post_url="https://www.pixiv.net/artworks/1",
                image_url="https://i.pximg.net/1.jpg",
                author="tester",
                raw_tags=["初音ミク", "鏡音リン"],
                extra_json={"page_index": 1},
                tag_reviews=[
                    {
                        "tag_id": miku_id,
                        "source_type": "crawl:pixiv",
                        "status": "pending",
                        "score": 0.0,
                        "reason": "pending",
                        "model_result": "",
                        "create_review_task": True,
                    },
                    {
                        "tag_id": rin_id,
                        "source_type": "crawl:pixiv",
                        "status": "pending",
                        "score": 0.0,
                        "reason": "pending",
                        "model_result": "",
                        "create_review_task": True,
                    },
                ],
            )

            detail = db.get_image_detail(image_id, sync_files=False)
            tasks = db.get_review_tasks_for_image(image_id)
            self.assertEqual(1, len(detail["sources"]))
            self.assertEqual({miku_id, rin_id}, {int(row["tag_id"]) for row in tasks})
            self.assertEqual({"pending"}, {str(row["status"]) for row in tasks})

    def test_terminal_discoveries_and_completed_job_release_source_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = ImageIndexDB(Path(temp_dir) / "image_index.db")
            snapshot = {"detail_snapshot": {"id": "123", "image_urls": ["large.json"]}}
            discovery, _created = db.upsert_crawl_discovery(
                platform="pixiv",
                source_uid="123",
                post_url="https://www.pixiv.net/artworks/123",
                tags=["初音未来"],
                source_context=snapshot,
            )
            job_id = db.create_crawl_job(
                "pixiv",
                "https://www.pixiv.net/artworks/123",
                ["初音未来"],
                source_context=snapshot,
            )

            db.mark_crawl_discovery_submitted(int(discovery["id"]), job_id)
            db.update_crawl_job(job_id, status="completed", clear_source_context=True)
            resolved, _created = db.upsert_crawl_discovery(
                platform="pixiv",
                source_uid="124",
                post_url="https://www.pixiv.net/artworks/124",
                tags=["初音未来"],
                source_context=snapshot,
            )
            db.mark_crawl_discovery_resolved(int(resolved["id"]), status="rejected")

            with db._connect() as conn:
                discovery_row = conn.execute(
                    "SELECT source_context_json FROM crawl_discoveries WHERE id = ?",
                    (int(discovery["id"]),),
                ).fetchone()
                resolved_row = conn.execute(
                    "SELECT source_context_json FROM crawl_discoveries WHERE id = ?",
                    (int(resolved["id"]),),
                ).fetchone()
            self.assertEqual("{}", str(discovery_row["source_context_json"]))
            self.assertEqual("{}", str(resolved_row["source_context_json"]))
            self.assertEqual("{}", str(db.get_crawl_job(job_id)["source_context_json"]))


class CollectionPostProcessingTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_retry_discards_stale_detail_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = ImageIndexDB(Path(temp_dir) / "image_index.db")
            job_id = db.create_crawl_job(
                "xiaohongshu",
                "https://www.xiaohongshu.com/explore/note123",
                ["初音未来"],
                source_context={
                    "note_id": "note123",
                    "xsec_token": "context",
                    "filters_applied": True,
                    "detail_snapshot": {"images": [{"url": "https://sns-img.invalid/old"}]},
                },
            )
            db.update_crawl_job(job_id, status="failed", error_log="download failed")
            service = CrawlService(
                db=db,
                importer=None,
                reviewer=None,
                config={},
            )

            ok, _message = await service.retry_job(job_id)

            self.assertTrue(ok)
            context = json.loads(str(db.get_crawl_job(job_id)["source_context_json"]))
            self.assertNotIn("detail_snapshot", context)
            self.assertEqual("note123", context["note_id"])
            self.assertEqual("retry", str(db.get_crawl_job(job_id)["status"]))
            self.assertEqual(1, service.queue_size())

    async def test_multi_image_post_resolves_tags_once_and_batches_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = ImageIndexDB(root / "image_index.db")
            db.get_or_create_tag("初音未来", is_character=True)
            job_id = db.create_crawl_job(
                "pixiv",
                "https://www.pixiv.net/artworks/123",
                ["初音未来"],
                source_context={"filters_applied": True},
            )
            candidates = [
                CrawlCandidate(
                    platform="pixiv",
                    post_url="https://www.pixiv.net/artworks/123",
                    normalized_post_url="https://www.pixiv.net/artworks/123",
                    source_uid="123",
                    image_url=f"https://i.pximg.net/{index}.jpg",
                    raw_tags=["初音ミク"],
                    title="test",
                    extra={"page_index": index},
                )
                for index in range(1, 4)
            ]

            class Adapter:
                async def fetch_candidates(self, *_args, **_kwargs):
                    return candidates

            class Importer:
                def __init__(self) -> None:
                    self.calls = 0

                async def import_candidates(self, values, *, concurrency):
                    self.calls += 1
                    result = []
                    for index, _candidate in enumerate(values, start=1):
                        path = root / f"{index}.jpg"
                        path.write_bytes(f"image-{index}".encode())
                        image_id = db.upsert_image(
                            file_path=str(path),
                            file_name=path.name,
                            sha256=f"sha-{index}",
                            width=100,
                            height=100,
                            format_="jpeg",
                        )
                        result.append(
                            ImportedImage(
                                image_id=image_id,
                                file_path=path,
                                sha256=f"sha-{index}",
                                phash="",
                                width=100,
                                height=100,
                                format="jpeg",
                            )
                        )
                    return result

            class Reviewer:
                @staticmethod
                def is_character_tag(_tag_name: str) -> bool:
                    return True

                @staticmethod
                async def review_image_for_tag(_path, _tag_name, **_kwargs):
                    return ReviewDecision(status="pending", confidence=0.0, reason="pending")

            class Service(CrawlService):
                def __init__(self, **kwargs) -> None:
                    super().__init__(**kwargs)
                    self.canonicalize_calls = 0

                def _canonicalize_primary_tags(self, **_kwargs):
                    self.canonicalize_calls += 1
                    return ["初音未来"]

            importer = Importer()
            service = Service(
                db=db,
                importer=importer,
                reviewer=Reviewer(),
                config={"crawl_keep_primary_tags_only": True},
            )

            with patch.object(crawl_module.CrawlAdapterFactory, "create", return_value=Adapter()):
                await service._process_job(job_id)

            self.assertEqual(1, importer.calls)
            self.assertEqual(1, service.canonicalize_calls)
            self.assertEqual(3, len(db.list_review_tasks(status="pending", limit=20)))
            job = db.get_crawl_job(job_id)
            self.assertEqual("completed", str(job["status"]))
            self.assertIn("图片 3 张", str(job["result_summary"]))
            self.assertEqual("{}", str(job["source_context_json"]))


if __name__ == "__main__":
    unittest.main()
