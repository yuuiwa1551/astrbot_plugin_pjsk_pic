from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

from .db import utcnow_str
from .message_images import (
    MessageImage,
    component_kind,
    original_chain,
    quoted_message_images,
)


class ChatImageIntakeService:
    """把白名单群中新出现的图片登记为待审候选（先审后入库）。"""

    def __init__(self, db, importer, chat_context):
        self.db = db
        self.importer = importer
        self.chat_context = chat_context
        self._tasks: set[asyncio.Task] = set()

    async def enroll(self, event, items: list[MessageImage]) -> list[dict[str, Any]]:
        enrolled: list[dict[str, Any]] = []
        for item in items or []:
            if not item.location:
                continue
            metadata = item.metadata or {}
            if metadata.get('platform_emoji'):
                continue
            if str(metadata.get('source_sender_id', '')) == str(event.get_self_id()):
                continue
            fields = self._candidate_fields(event, item)
            candidate = self.db.create_chat_image_candidate(**fields)
            candidate_id = int(candidate.get('id') or 0)
            if not candidate_id:
                continue
            if str(candidate.get('status')) != 'captured':
                self.db.attach_chat_image_candidate_occurrence(candidate_id, {
                    'at': utcnow_str(),
                    'session_id': event.unified_msg_origin,
                    'sender_id': str(event.get_sender_id()),
                    'source_message_id': fields['source_message_id'],
                })
                continue
            self._spawn(self._hydrate(candidate_id, item))
            enrolled.append(candidate)
        return enrolled

    def schedule_quoted(self, event) -> None:
        if not any(component_kind(component) == 'reply' for component in original_chain(event)):
            return
        self._spawn(self._collect_quoted(event))

    async def _collect_quoted(self, event) -> None:
        try:
            items = await quoted_message_images(event)
        except Exception as exc:
            logger.warning('[PJSKPic] 引用图解析失败 error=%s', type(exc).__name__)
            return
        items = [item for item in items if item.location]
        if not items:
            return
        self.chat_context.start_prefetch_items(items)
        await self.enroll(event, items)

    async def _hydrate(self, candidate_id: int, item: MessageImage) -> None:
        try:
            imported = await item.import_into(self.importer)
        except Exception as exc:
            logger.warning('[PJSKPic] 群聊图片下载失败 candidate=%s error=%s',
                           candidate_id, type(exc).__name__)
            self.db.mark_chat_image_candidate_download_failed(
                candidate_id, f'{type(exc).__name__}: {exc}')
            return
        if self.db.has_approved_character_tags(int(imported.image_id)):
            self._append_library_source(candidate_id, item, int(imported.image_id))
            return
        existing = self.db.find_chat_image_candidate_by_sha(imported.sha256, exclude_id=candidate_id)
        if existing is not None:
            self.db.mark_chat_image_candidate_duplicate(
                candidate_id, duplicate_of=int(existing['id']))
            return
        self.db.update_chat_image_candidate_local(
            candidate_id,
            file_path=str(imported.file_path),
            content_sha256=imported.sha256,
        )

    def _append_library_source(self, candidate_id: int, item: MessageImage, image_id: int) -> None:
        metadata = item.metadata or {}
        try:
            self.db.upsert_source(
                image_id,
                platform='chat',
                post_url='chat://' + str(metadata.get('source_message_id', '')),
                image_url=item.location,
                author=str(metadata.get('source_sender_name', '')),
                raw_tags=[],
                extra_json={
                    'source_kind': 'chat_auto_collection',
                    'candidate_id': candidate_id,
                    **metadata,
                },
            )
        except Exception as exc:
            self.db.mark_chat_image_candidate_write_failed(
                candidate_id, error=f'补来源失败：{type(exc).__name__}')
            return
        self.db.mark_chat_image_candidate_duplicate(
            candidate_id, duplicate_of=0,
            reason=f'图库已有同一图片 #{image_id}，只补来源',
        )

    @staticmethod
    def _candidate_fields(event, item: MessageImage) -> dict[str, Any]:
        metadata = item.metadata or {}
        return {
            'ref': item.ref,
            'session_id': str(metadata.get('session_id') or event.unified_msg_origin),
            'group_id': str(event.get_group_id() or ''),
            'platform': str(event.get_platform_name() or ''),
            'sender_id': str(metadata.get('source_sender_id') or ''),
            'sender_name': str(metadata.get('source_sender_name') or ''),
            'source_message_id': str(metadata.get('source_message_id') or ''),
            'image_index': int(metadata.get('image_index') or 0),
            'image_url': item.location,
        }

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
