from __future__ import annotations

import asyncio
import base64
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from astrbot.api import logger

from .message_images import component_kind, original_chain

CONFIRM_HEADER = '【收图确认】'
CONFIRM_TOKEN_PATTERN = re.compile(r'确认批次：(local:[0-9a-f]{32})\s*$')
SELECT_PATTERN = re.compile(r'^(?:收|确认)\s*第\s*([0-9０-９一二三四五六七八九十两]+)\s*张$')
CORRECTION_PATTERN = re.compile(
    r'^第\s*([0-9０-９一二三四五六七八九十两]+)\s*张\s*(?:是|改成|改为|换成|[:：])\s*(.+)$'
)
CORRECTION_HINT_PATTERN = re.compile(r'^第\s*.{1,6}?\s*张')
DEFAULT_ACCEPT_WORDS = ('确认', '对', '可以', '没问题', '收', '全部收', '收全部', '全部确认')
DEFAULT_REJECT_WORDS = ('不要', '不对', '拒绝', '不收')
_STRIP_CHARS = ' \t\u3000。．.!！~～?？,，、;；:：'
_NUMBER_CHARS = {
    '零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
    '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
}


def _normalize_text(text: str) -> str:
    value = ''.join(str(text or '').split())
    return value.strip(_STRIP_CHARS).casefold()


def _parse_index(raw: str) -> int | None:
    value = str(raw or '').strip()
    if not value:
        return None
    digits = ''.join(
        chr(ord(char) - 0xFEE0) if '\uff10' <= char <= '\uff19' else char
        for char in value
    )
    if digits.isdigit():
        return int(digits)
    if len(digits) == 1:
        return _NUMBER_CHARS.get(digits)
    if digits == '十':
        return 10
    if digits.startswith('十'):
        tail = _NUMBER_CHARS.get(digits[1:], -1)
        return 10 + tail if tail >= 0 else None
    if '十' in digits:
        head, _, tail = digits.partition('十')
        tens = _NUMBER_CHARS.get(head, -1)
        if tens < 0:
            return None
        ones = _NUMBER_CHARS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    return None


def _split_tag_names(raw: str) -> list[str]:
    parts = re.split(r'[、，,／/|;；和]+', str(raw or ''))
    names = []
    for part in parts:
        name = part.strip()
        if name:
            names.append(name)
    return names


def parse_correction(text: str) -> tuple[int, list[str]] | None:
    text = re.sub(r'[，,、\s]*(?:收了|收录|收吧|收)$', '', str(text or '').strip(_STRIP_CHARS))
    match = CORRECTION_PATTERN.match(text)
    if not match:
        return None
    index = _parse_index(match.group(1))
    names = _split_tag_names(match.group(2))
    if index is None or not names:
        return None
    return index, names


def looks_like_correction(text: str) -> bool:
    return bool(CORRECTION_HINT_PATTERN.match(str(text or '').strip()))


def parse_selection(text: str) -> tuple[str, list[int]] | None:
    """Only explicit lists; validate all indices before mutating any candidate."""
    match = re.fullmatch(r'(收|确认|跳过|不收)\s*((?:第?\s*[0-9０-９一二三四五六七八九十两]+\s*张?)(?:\s*[、，,]\s*第?\s*[0-9０-９一二三四五六七八九十两]+\s*张?)*)', text.strip(_STRIP_CHARS))
    if not match:
        return None
    indices = [_parse_index(re.sub(r'[第张\s]', '', part)) or 0
               for part in re.split(r'[、，,]', match.group(2))]
    return ('skip' if match.group(1) in {'跳过', '不收'} else 'collect', list(dict.fromkeys(indices)))


class ChatImageConfirmService:
    """群内确认 worker：把审核通过的候选整批发到群里，按发图人回复决定收录或更正。"""

    def __init__(
        self,
        *,
        db,
        context,
        config: Mapping[str, Any],
        importer,
        candidate_tags_provider: Callable[[], list[dict[str, Any]]],
    ) -> None:
        self.db = db
        self.context = context
        self.config = config
        self.importer = importer
        self.candidate_tags_provider = candidate_tags_provider
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._reply_lock = asyncio.Lock()

    def enabled(self) -> bool:
        return bool(self.config.get("chat_image_collection_enabled", False))

    def interval_seconds(self) -> int:
        return min(max(15, int(self.config.get("chat_image_confirm_interval_seconds", 60) or 60)), 3600)

    def timeout_hours(self) -> int:
        return min(max(1, int(self.config.get("chat_image_confirm_timeout_hours", 24) or 24)), 168)

    def max_batches_per_cycle(self) -> int:
        return min(max(1, int(self.config.get("chat_image_confirm_max_per_cycle", 3) or 3)), 20)

    def batch_size(self) -> int:
        return min(max(1, int(self.config.get("chat_image_confirm_batch_size", 10) or 10)), 20)

    def receipt_enabled(self) -> bool:
        return bool(self.config.get("chat_image_confirm_receipt_enabled", True))

    def auto_approve_enabled(self) -> bool:
        return bool(self.config.get("chat_image_auto_approve_enabled", False))

    def _word_set(self, key: str, default: tuple[str, ...]) -> set[str]:
        raw = self.config.get(key, '')
        if isinstance(raw, (list, tuple)):
            values = [str(item) for item in raw]
        else:
            values = str(raw or '').replace('，', ',').split(',')
        words = {_normalize_text(value) for value in values if _normalize_text(value)}
        return words or {_normalize_text(value) for value in default}

    def accept_words(self) -> set[str]:
        return self._word_set("chat_image_confirm_accept_words", DEFAULT_ACCEPT_WORDS)

    def reject_words(self) -> set[str]:
        return self._word_set("chat_image_confirm_reject_words", DEFAULT_REJECT_WORDS)

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        self._stop_event.clear()
        if not self.enabled():
            logger.info("[PJSKPic] 群聊收图确认未启用")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="pjsk-pic-chat-image-confirm")
            logger.info(
                "[PJSKPic] 群聊收图确认已启动："
                f"interval={self.interval_seconds()}s timeout={self.timeout_hours()}h"
            )

    async def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def trigger(self) -> None:
        self._wake_event.set()

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.clear()
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[PJSKPic] 群聊收图确认循环失败：{type(exc).__name__}: {exc}", exc_info=True)
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=self.interval_seconds())
            except asyncio.TimeoutError:
                continue

    async def run_once(self, max_batches: int | None = None) -> dict[str, int]:
        summary = {"batches": 0, "sent": 0, "expired": 0, "failed": 0, "auto": 0}
        if not self.enabled():
            return summary
        summary["expired"] = int(self.db.expire_asked_chat_image_candidates(
            datetime.now(timezone.utc).isoformat(timespec='seconds')))
        limit = self.max_batches_per_cycle() if max_batches is None else min(max(1, int(max_batches)), 20)
        rows = self.db.list_chat_image_candidates_for_notify(limit=limit * self.batch_size())
        if self.auto_approve_enabled():
            for offset in range(0, len(rows), self.batch_size()):
                chunk = rows[offset:offset + self.batch_size()]
                if not chunk:
                    continue
                ids = [int(row['id']) for row in chunk]
                self.db.mark_chat_image_candidates_confirmed(ids, user_id='auto', statuses=('audited',))
                fresh = [self.db.get_chat_image_candidate(candidate_id) for candidate_id in ids]
                await self._write_batch([row for row in fresh if row], user_id='auto', notify=False)
                summary["auto"] += len(chunk)
            return summary
        for batch in self._group_batches(rows)[:limit]:
            summary["batches"] += 1
            try:
                sent = await self._announce(batch)
            except Exception as exc:
                summary["failed"] += 1
                logger.error(
                    f"[PJSKPic] 群聊收图确认发送失败：{type(exc).__name__}: {exc}",
                    exc_info=True,
                )
                continue
            if sent:
                summary["sent"] += 1
        return summary

    def _group_batches(self, rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        order: list[tuple[str, str]] = []
        for row in rows or []:
            key = (str(row.get('session_id') or ''), str(row.get('sender_id') or ''))
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(row)
        size = self.batch_size()
        return [grouped[key][:size] for key in order]

    def _tag_lookup(self) -> dict[int, dict[str, Any]]:
        result: dict[int, dict[str, Any]] = {}
        for item in self.candidate_tags_provider():
            try:
                result[int(item['tag_id'])] = item
            except (KeyError, TypeError, ValueError):
                continue
        return result

    def _tag_names(self, tag_ids: list[int], lookup: dict[int, dict[str, Any]]) -> list[str]:
        return [str(lookup.get(int(tag_id), {}).get('name') or f'tag{tag_id}') for tag_id in tag_ids]

    async def _announce(self, batch: list[dict[str, Any]]) -> bool:
        if not batch:
            return False
        batch = sorted(batch, key=lambda row: int(row['id']))
        previews: list[tuple[dict[str, Any], str]] = []
        for candidate in batch:
            try:
                path = Path(str(candidate.get('file_path') or ''))
                body = await asyncio.to_thread(path.read_bytes)
                if not body:
                    raise ValueError('empty preview')
                previews.append((candidate, base64.b64encode(body).decode('ascii')))
            except (OSError, ValueError):
                self.db.mark_chat_image_candidates_pending_webui(
                    [int(candidate['id'])], error='确认图片缺失或不可读取，转入待审')
        if not previews:
            return False
        batch = [row for row, _ in previews]
        lookup = self._tag_lookup()
        sender_name = str(batch[0].get('sender_name') or batch[0].get('sender_id') or '')
        token = f"local:{uuid.uuid4().hex}"
        text = f"{CONFIRM_HEADER}本批 {len(batch)} 张，请 {sender_name} 引用本条确认：\n"
        segments: list[dict[str, Any]] = [{'type': 'text', 'data': {'text': text}}]
        for index, (candidate, encoded) in enumerate(previews, 1):
            tag_ids = [int(x) for x in (candidate.get('proposed_tag_ids_json') or [])]
            label = '、'.join(self._tag_names(tag_ids, lookup)) or '（无标签）'
            segments.extend([
                {'type': 'text', 'data': {'text': f'\n第{index}张：{label}\n'}},
                {'type': 'image', 'data': {'file': f'base64://{encoded}'}},
            ])
        segments.append({'type': 'text', 'data': {'text': (
            '\n回复「收全部」收录剩余图片；「收1、3」多选；「跳过2」忽略指定图片。\n'
            '更正并只收该张：「第2张是角色A和角色B，收了」；单图可说「这张是…，收了」。回复「不要」取消剩余图片。\n'
            f'{self.timeout_hours()} 小时内未回复会自动转入待审。\n确认批次：{token}'
        )}})
        ok, message_id = await self._send_group(
            str(batch[0].get('session_id') or ''), text,
            at_qq=str(batch[0].get('sender_id') or ''), message_segments=segments)
        if not ok:
            return False
        token = message_id or token
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=self.timeout_hours())
        ).isoformat(timespec='seconds')
        updated = self.db.mark_chat_image_candidates_asked(
            [int(row['id']) for row in batch],
            confirm_message_id=token,
            expires_at=expires_at,
        )
        return bool(updated)

    @staticmethod
    def _is_backfill_candidate(candidate: Mapping[str, Any]) -> bool:
        return str(candidate.get('audit_provider') or '') == 'backfill'

    def _confirm_backfill_candidate(self, candidate: Mapping[str, Any]) -> None:
        candidate_id = int(candidate['id'])
        image_id = int(candidate.get('image_id') or 0)
        if image_id <= 0 or not self.db.get_image_file_path(image_id):
            self.db.mark_chat_image_candidate_write_failed(
                candidate_id, error='回填候选对应图片已不在图库')
            return
        tag_ids = [int(value) for value in (candidate.get('proposed_tag_ids_json') or [])]
        self.db.mark_chat_image_candidate_written(
            candidate_id, image_id=image_id, tag_ids=tag_ids)

    def _revert_backfill_candidate(self, candidate: Mapping[str, Any]) -> None:
        try:
            image_id = int(candidate.get('image_id') or 0)
        except (TypeError, ValueError):
            return
        if image_id <= 0:
            return
        for value in candidate.get('proposed_tag_ids_json') or []:
            try:
                tag_id = int(value)
            except (TypeError, ValueError):
                continue
            self.db.update_image_tag_review(
                image_id, tag_id, 'manual_rejected',
                reason='群聊候选人工忽略',
                source_type_prefix='chat_auto_collection',
            )

    async def approve_candidates(self, candidate_ids: list[int], *, user_id: str = 'webui') -> dict[str, int]:
        ids = [int(x) for x in candidate_ids]
        if not ids:
            return {'approved': 0, 'failed': 0}
        operator = str(user_id or 'webui')
        self.db.mark_chat_image_candidates_confirmed(
            ids, user_id=operator, statuses=('pending_webui',))
        batch = [
            row for row in (self.db.get_chat_image_candidate(candidate_id) for candidate_id in ids)
            if row and str(row.get('status') or '') == 'confirmed'
        ]
        fresh = [row for row in batch if not self._is_backfill_candidate(row)]
        await self._write_batch(fresh, user_id=operator, notify=False)
        for candidate in batch:
            if self._is_backfill_candidate(candidate):
                self._confirm_backfill_candidate(candidate)
        approved = 0
        failed = 0
        for candidate in batch:
            row = self.db.get_chat_image_candidate(int(candidate['id']))
            if not row:
                continue
            status = str(row.get('status') or '')
            if status == 'approved_written':
                approved += 1
            elif status == 'write_failed':
                failed += 1
        return {'approved': approved, 'failed': failed}

    def reject_candidates(self, candidate_ids: list[int], *, user_id: str = 'webui') -> int:
        ids = [int(x) for x in candidate_ids]
        if not ids:
            return 0
        rows_before = [self.db.get_chat_image_candidate(candidate_id) for candidate_id in ids]
        rejected = self.db.mark_chat_image_candidates_rejected(
            ids, user_id=str(user_id or 'webui'), statuses=('pending_webui',))
        if rejected:
            for row in rows_before:
                if not row or str(row.get('status') or '') != 'pending_webui':
                    continue
                if self._is_backfill_candidate(row):
                    self._revert_backfill_candidate(row)
        return rejected

    async def handle_reply(self, event) -> bool:
        async with self._reply_lock:
            return await self._handle_reply(event)

    async def _handle_reply(self, event) -> bool:
        if not self.enabled() or event.is_private_chat():
            return False
        self_id = str(event.get_self_id() or '')
        sender_id = str(event.get_sender_id() or '')
        if not sender_id or sender_id == self_id:
            return False
        if not self._group_allowed(event):
            return False
        reply_id = self._reply_id(event)
        at_self = self_id in self._at_targets(event)
        if not reply_id and not at_self:
            return False
        text = self._message_text(event)
        if not text:
            return False
        session_id = str(event.unified_msg_origin)
        batch: list[dict[str, Any]] = []
        if reply_id:
            batch = self.db.find_chat_image_candidates_by_confirm(
                reply_id, sender_id=sender_id, session_id=session_id, include_resolved=True)
            if not batch:
                token = await self._confirm_token_from_reply(event, reply_id, self_id, sender_id)
                if token:
                    batch = self.db.find_chat_image_candidates_by_confirm(
                        token, sender_id=sender_id, session_id=session_id, include_resolved=True)
        elif at_self:
            batch = self.db.find_single_asked_chat_image_batch(session_id, sender_id)
            if not batch and (self.db.find_latest_asked_chat_image_candidates(session_id, sender_id)):
                if (_normalize_text(text) in self.accept_words() | self.reject_words() | {'收全部'}
                        or parse_selection(text) or looks_like_correction(text) or text.startswith('这张')):
                    await self._send_group(session_id, f'{CONFIRM_HEADER}有多批图片待确认，请引用对应的带图确认消息回复。')
                    return True
        if not batch:
            return False
        if str(batch[0].get('session_id') or '') != session_id:
            return False
        if not any(row.get('status') == 'asked' for row in batch):
            return False
        for index, candidate in enumerate(batch, 1):
            candidate['_confirm_index'] = index
        return await self._apply_reply(event, batch, text, sender_id)

    async def _apply_reply(self, event, batch: list[dict[str, Any]], text: str,
                           sender_id: str) -> bool:
        if text.startswith('这张'):
            if len(batch) != 1:
                await self._send_group(str(event.unified_msg_origin), f'{CONFIRM_HEADER}本批有多张图片，请明确编号，例如「第2张是角色名，收了」。')
                return True
            text = '第1张' + text[2:]
        correction = parse_correction(text)
        if correction is not None:
            await self._apply_correction(event, batch, correction, sender_id)
            return True
        normalized = _normalize_text(text)
        if normalized in self.reject_words():
            count = self.db.mark_chat_image_candidates_rejected(
                [int(row['id']) for row in batch], user_id=sender_id)
            await self._send_group(str(event.unified_msg_origin), f'{CONFIRM_HEADER}已取消剩余 {count} 张。')
            return True
        selection = parse_selection(text)
        if normalized in self.accept_words() or normalized == '收全部' or selection:
            selected = batch
            if selection:
                action, indices = selection
                if any(index < 1 or index > len(batch) for index in indices):
                    await self._send_group(str(event.unified_msg_origin), f'{CONFIRM_HEADER}本批共 {len(batch)} 张，编号有误，本次未作修改。')
                    return True
                selected = [batch[index - 1] for index in indices]
                if action == 'skip':
                    pending = [row for row in selected if row.get('status') == 'asked']
                    self.db.mark_chat_image_candidates_rejected([int(row['id']) for row in pending], user_id=sender_id)
                    labels = '、'.join(str(row['_confirm_index']) for row in pending)
                    remaining = sum(row.get('status') == 'asked' for row in batch) - len(pending)
                    await self._send_group(str(event.unified_msg_origin), f'{CONFIRM_HEADER}' + (f'已跳过第{labels}张；本批剩余 {remaining} 张待确认。' if pending else '所选图片已处理。'))
                    return True
                if not any(row.get('status') == 'asked' for row in selected):
                    await self._send_group(str(event.unified_msg_origin), f'{CONFIRM_HEADER}所选图片已处理，无需重复确认。')
                    return True
            selected = [row for row in selected if row.get('status') == 'asked']
            ids = [int(row['id']) for row in selected]
            self.db.mark_chat_image_candidates_confirmed(ids, user_id=sender_id)
            await self._write_batch(self._refresh_selected(selected), user_id=sender_id)
            return True
        if looks_like_correction(text):
            await self._send_group(
                str(event.unified_msg_origin),
                f"{CONFIRM_HEADER}更正格式：第2张是<tag名>，可用「、」分隔多个，"
                "例如：第2张是东云彰人、青柳冬弥。",
            )
            return True
        return False

    async def _apply_correction(self, event, batch: list[dict[str, Any]],
                                correction: tuple[int, list[str]], sender_id: str) -> None:
        index, names = correction
        if index < 1 or index > len(batch):
            await self._send_group(
                str(event.unified_msg_origin),
                f"{CONFIRM_HEADER}本批共 {len(batch)} 张，没有第 {index} 张。",
            )
            return
        tag_ids, error = self._resolve_correction_tags(names)
        if error:
            await self._send_group(str(event.unified_msg_origin), f"{CONFIRM_HEADER}{error}")
            return
        target = batch[index - 1]
        if not self.db.mark_chat_image_candidate_corrected(
                int(target['id']), corrected_tag_ids=tag_ids, user_id=sender_id):
            await self._send_group(str(event.unified_msg_origin), f'{CONFIRM_HEADER}第 {index} 张已处理，无需重复更正。')
            return
        await self._write_batch(self._refresh_selected([target]), user_id=sender_id)

    def _refresh_selected(self, selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
        fresh = []
        for candidate in selected:
            row = self.db.get_chat_image_candidate(int(candidate['id']))
            if row and row.get('status') in {'confirmed', 'corrected'}:
                row['_confirm_index'] = candidate.get('_confirm_index', len(fresh) + 1)
                fresh.append(row)
        return fresh

    def _resolve_correction_tags(self, names: list[str]) -> tuple[list[int], str]:
        lookup = self._tag_lookup()
        ids = {tag_id: item for tag_id, item in lookup.items()}
        by_name: dict[str, int] = {}
        for tag_id, item in lookup.items():
            for key in ('name', 'standard_name', 'name_en', 'name_ja'):
                value = _normalize_text(str(item.get(key) or ''))
                if value:
                    by_name.setdefault(value, tag_id)
        selected: list[int] = []
        for name in names:
            tag_id = by_name.get(_normalize_text(name))
            if tag_id is None:
                match = self.db.resolve_tag(name, allow_fuzzy=False)
                if match.matched and int(match.tag_id) in ids:
                    tag_id = int(match.tag_id)
            if tag_id is None:
                return [], f"未识别到候选 tag「{name}」。更正格式：第2张是<tag名>。"
            if tag_id not in selected:
                selected.append(tag_id)
        if not any(str(ids[tag_id].get('tag_type') or '') == 'character' for tag_id in selected):
            return [], "更正至少要包含一个 PJSK 角色 tag。"
        for tag_id in selected:
            members = [int(value) for value in (ids[tag_id].get('member_ids') or [])]
            missing = [value for value in members if value not in selected]
            if missing:
                names_text = '、'.join(
                    str(ids[value].get('name') or value) for value in missing)
                return [], (
                    f"「{ids[tag_id].get('name')}」需要同时选中成员：{names_text}。"
                )
        return selected, ''

    async def _write_batch(self, batch: list[dict[str, Any]], *, user_id: str,
                           notify: bool = True) -> None:
        if not batch:
            return
        lookup = self._tag_lookup()
        written: list[tuple[dict[str, Any], list[str], int, bool]] = []
        failed: list[dict[str, Any]] = []
        for index, candidate in enumerate(batch, 1):
            candidate.setdefault('_confirm_index', index)
            if candidate.get('status') not in {'confirmed', 'corrected'}:
                continue
            candidate_id = int(candidate['id'])
            tag_ids = [
                int(value)
                for value in (candidate.get('confirmed_tag_ids_json')
                              or candidate.get('proposed_tag_ids_json') or [])
            ]
            tag_ids = [value for value in tag_ids if value in lookup]
            if not tag_ids or not any(
                str(lookup[value].get('tag_type') or '') == 'character' for value in tag_ids
            ):
                self.db.mark_chat_image_candidate_write_failed(
                    candidate_id, error='没有可写入的角色标签')
                failed.append(candidate)
                continue
            file_path = str(candidate.get('file_path') or '')
            if not file_path:
                self.db.mark_chat_image_candidate_write_failed(
                    candidate_id, error='缺少本地图片文件')
                failed.append(candidate)
                continue
            try:
                imported = await self.importer.import_local_file(Path(file_path), platform='chat')
                result = self.db.commit_chat_collection_image(
                    image_id=int(imported.image_id),
                    image_url=str(candidate.get('image_url') or file_path),
                    author=str(candidate.get('sender_name') or candidate.get('sender_id') or ''),
                    raw_tags=self._tag_names(tag_ids, lookup),
                    extra_json={
                        'source_kind': 'chat_group_collection',
                        'candidate_id': candidate_id,
                        'group_id': str(candidate.get('group_id') or ''),
                        'session_id': str(candidate.get('session_id') or ''),
                        'source_message_id': str(candidate.get('source_message_id') or ''),
                        'confirm_user_id': user_id,
                    },
                    tag_ids=tag_ids,
                    reason='群聊收图确认',
                    tag_members={
                        value: [int(x) for x in (lookup[value].get('member_ids') or [])]
                        for value in tag_ids
                    },
                )
            except Exception as exc:
                self.db.mark_chat_image_candidate_write_failed(
                    candidate_id, error=f'{type(exc).__name__}: {exc}')
                failed.append(candidate)
                continue
            accepted = [int(value) for value in (result.get('tag_ids_accepted') or [])]
            if not accepted:
                self.db.mark_chat_image_candidate_write_failed(
                    candidate_id, error='图库已有冲突标注，未写入')
                failed.append(candidate)
                continue
            self.db.mark_chat_image_candidate_written(
                candidate_id, image_id=int(imported.image_id), tag_ids=accepted)
            written.append((candidate, self._tag_names(accepted, lookup),
                            int(imported.image_id), bool(imported.is_new)))
        if notify:
            await self._send_receipt(batch[0], written, failed)

    async def _send_receipt(self, candidate: dict[str, Any],
                            written: list[tuple[dict[str, Any], list[str], int, bool]],
                            failed: list[dict[str, Any]]) -> None:
        if not self.receipt_enabled():
            return
        lines: list[str] = []
        if written:
            lines.append(f"【收图回执】已处理 {len(written)} 张：")
            for row, names, image_id, is_new in written:
                index = int(row.get('_confirm_index', 1))
                action = '已收录' if is_new else '已在图库，本次未新增图片'
                lines.append(f"第{index}张 → 图片 ID：#{image_id}（{action}）\n标签："
                             + ("、".join(names) if names else "（无标签）"))
        if failed:
            if not written:
                lines.append('【收图回执】本次未能完成收录：')
            for row in failed:
                lines.append(f"第{int(row.get('_confirm_index', 1))}张：收录失败，未确认入库；已记录原因，可在待审页处理。")
        if not lines:
            lines.append("【收图回执】本批没有可收录的图片。")
        confirm_id = str(candidate.get('confirm_message_id') or '')
        if written:
            lines.append(f'查看图片：看图{written[0][2]}（沿用图库查看权限）')
        if confirm_id:
            remaining = self.db.find_chat_image_candidates_by_confirm(confirm_id, sender_id=str(candidate.get('sender_id') or ''), session_id=str(candidate.get('session_id') or ''), include_resolved=True)
            pending = sum(row.get('status') == 'asked' for row in remaining)
            if pending:
                lines.append(f'本批还有 {pending} 张待确认；请继续引用原带图消息回复。')
        segments = [{'type': 'reply', 'data': {'id': confirm_id}}] if confirm_id.isdigit() else []
        segments.append({'type': 'text', 'data': {'text': '\n'.join(lines)}})
        await self._send_group(str(candidate.get('session_id') or ''), '\n'.join(lines),
                               message_segments=segments)

    @staticmethod
    def _reply_id(event) -> str:
        for component in original_chain(event):
            if component_kind(component) != 'reply':
                continue
            if isinstance(component, dict):
                return str((component.get('data') or {}).get('id') or '')
            return str(getattr(component, 'id', '') or '')
        return ''

    @staticmethod
    def _at_targets(event) -> list[str]:
        targets = []
        for component in original_chain(event):
            if component_kind(component) != 'at':
                continue
            if isinstance(component, dict):
                qq = (component.get('data') or {}).get('qq')
            else:
                qq = getattr(component, 'qq', None)
            if qq is not None:
                targets.append(str(qq))
        return targets

    @staticmethod
    def _component_text(component) -> str:
        if isinstance(component, dict):
            return str((component.get('data') or {}).get('text') or '')
        return str(getattr(component, 'text', '') or '')

    def _message_text(self, event) -> str:
        text = str(getattr(event, 'message_str', '') or '').strip()
        if text:
            return text
        chunks = []
        for component in original_chain(event):
            if component_kind(component) in {'text', 'plain'}:
                chunks.append(self._component_text(component))
        return ''.join(chunks).strip()

    async def _confirm_token_from_reply(self, event, reply_id: str, self_id: str,
                                        sender_id: str) -> str:
        bot = getattr(event, 'bot', None)
        if bot is None or not reply_id:
            return ''
        try:
            response = await bot.call_action('get_msg', message_id=int(reply_id))
            payload = response.get('data', response) if isinstance(response, dict) else response
        except Exception:
            return ''
        if not isinstance(payload, dict):
            return ''
        if payload.get('group_id') is not None and str(payload['group_id']) != str(event.get_group_id()):
            return ''
        sender = payload.get('sender') or {}
        if str(sender.get('user_id') or '') != self_id:
            return ''
        at_sender = False
        chunks = []
        for node in payload.get('message') or []:
            kind = component_kind(node)
            if kind == 'at':
                if isinstance(node, dict):
                    qq = (node.get('data') or {}).get('qq')
                else:
                    qq = getattr(node, 'qq', None)
                if str(qq) == sender_id:
                    at_sender = True
            elif kind in {'text', 'plain'}:
                chunks.append(self._component_text(node))
        text = ''.join(chunks)
        match = CONFIRM_TOKEN_PATTERN.search(text)
        return match.group(1) if at_sender and CONFIRM_HEADER in text and match else ''

    def _configured_groups(self) -> set[str]:
        raw = self.config.get("chat_image_collection_groups", "")
        if isinstance(raw, (list, tuple)):
            return {str(item).strip() for item in raw if str(item).strip()}
        return {
            item.strip()
            for item in str(raw or '').replace('，', ',').split(',')
            if item.strip()
        }

    def _group_allowed(self, event) -> bool:
        groups = self._configured_groups()
        return not groups or str(event.get_group_id() or '') in groups

    def _get_bot(self, platform_name: str):
        manager = getattr(self.context, 'platform_manager', None)
        for platform in getattr(manager, 'platform_insts', []) or []:
            try:
                meta = platform.meta()
            except Exception:
                continue
            if str(getattr(meta, 'id', '') or '') == platform_name:
                return getattr(platform, 'bot', None)
        return None

    async def _send_group(self, session_id: str, text: str, *, at_qq: str = '',
                          message_segments: list[dict[str, Any]] | None = None) -> tuple[bool, str]:
        session_text = str(session_id or '')
        parts = session_text.split(':')
        platform_name = parts[0] if parts else ''
        group_id = parts[-1] if len(parts) >= 3 else ''
        bot = self._get_bot(platform_name)
        send_group_msg = getattr(bot, 'send_group_msg', None) if bot is not None else None
        segments: list[dict[str, Any]] = []
        if at_qq:
            segments.append({'type': 'at', 'data': {'qq': str(at_qq)}})
        segments.extend(message_segments if message_segments is not None
                        else [{'type': 'text', 'data': {'text': text}}])
        if send_group_msg is not None and str(group_id).isdigit():
            try:
                result = await send_group_msg(group_id=int(group_id), message=segments)
            except Exception as exc:
                logger.warning(
                    f"[PJSKPic] 群聊确认消息直接发送失败：{type(exc).__name__}: {exc}")
            else:
                if isinstance(result, dict) and (result.get('status') == 'failed'
                        or result.get('retcode', 0) not in (0, '0', None)):
                    return False, ''
                message_id = ''
                if isinstance(result, dict):
                    payload = result.get('data')
                    if isinstance(payload, dict):
                        message_id = str(payload.get('message_id') or '')
                    if not message_id:
                        message_id = str(result.get('message_id') or '')
                return True, message_id
        try:
            from astrbot.core.message.message_event_result import MessageChain
            from astrbot.api.message_components import At, Image, Plain, Reply
            chain = MessageChain()
            for segment in segments:
                data = segment['data']
                if segment['type'] == 'text':
                    chain.chain.append(Plain(data['text']))
                elif segment['type'] == 'at':
                    chain.chain.append(At(qq=data['qq']))
                elif segment['type'] == 'image':
                    chain.chain.append(Image.fromBase64(data['file'].removeprefix('base64://')))
                elif segment['type'] == 'reply':
                    chain.chain.append(Reply(id=data['id']))
            ok = bool(await self.context.send_message(session_text, chain))
            return ok, ''
        except Exception as exc:
            logger.warning(f"[PJSKPic] 群聊确认消息发送失败：{type(exc).__name__}: {exc}")
            return False, ''
