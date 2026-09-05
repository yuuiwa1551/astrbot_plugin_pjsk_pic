from __future__ import annotations

import unittest

from server import _normalize_detail, _normalize_search


class SidecarNormalizationTests(unittest.TestCase):
    def test_search_contract_includes_page_and_standard_feed_shape(self) -> None:
        result = _normalize_search(
            {
                "items": [
                    {
                        "id": "note-1",
                        "xsec_token": "context",
                        "model_type": "note",
                        "note_card": {
                            "display_title": "title",
                            "type": "normal",
                            "user": {"nickname": "author"},
                        },
                    }
                ],
                "has_more": True,
            },
            page=2,
            page_size=20,
        )
        self.assertEqual(2, result["page"])
        self.assertTrue(result["hasMore"])
        self.assertEqual("note-1", result["feeds"][0]["id"])
        self.assertEqual("context", result["feeds"][0]["xsecToken"])

    def test_detail_contract_preserves_all_images_and_topics(self) -> None:
        result = _normalize_detail(
            {
                "items": [
                    {
                        "note_card": {
                            "note_id": "note-1",
                            "type": "normal",
                            "title": "title",
                            "desc": "body",
                            "user": {"nickname": "author"},
                            "time": 123,
                            "image_list": [
                                {"url_default": "https://sns-img.example/1", "width": 10, "height": 20},
                                {"url_pre": "https://sns-img.example/2", "width": 30, "height": 40},
                            ],
                            "tag_list": [{"name": "初音未来"}],
                        }
                    }
                ]
            },
            note_id="note-1",
        )
        self.assertEqual(2, len(result["imageList"]))
        self.assertEqual([{"name": "初音未来"}], result["tagList"])


if __name__ == "__main__":
    unittest.main()
