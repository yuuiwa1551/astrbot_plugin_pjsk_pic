from __future__ import annotations

import asyncio
import copy
import hashlib
import re
import time
from collections import OrderedDict
from pathlib import Path

from astrbot.api import logger
from astrbot.api.message_components import Image
from astrbot.core.agent.message import ImageURLPart, TextPart
from astrbot.core.utils.media_utils import MediaResolver

from .message_images import (
    MessageImage,
    direct_message_images,
    quoted_message_images,
)

REF_PATTERN = re.compile(r'\[gallery_image:(g[0-9a-f]{16})\]')
ANNOTATION_PATTERN = re.compile(
    r'^(?:下面这张图片的 image_ref|上下文图片原图，image_ref)=(g[0-9a-f]{16})$'
)
PROMPT_ANNOTATION_PATTERN = re.compile(r'\n?本次请求附图按顺序对应 image_ref：[g0-9a-f、]*')

CACHE_RETENTION_SECONDS = 72 * 3600
FAILURE_TTL_SECONDS = 6 * 3600
FAILURE_MEMORY_LIMIT = 500
ALIAS_MEMORY_LIMIT = 400
RECORD_MEMORY_LIMIT = 200


class ChatImageContext:
    def __init__(self, cache_dir=None):
        self.sessions = {}
        self._aliases = {}
        self._failures = OrderedDict()
        self._cache_dir = Path(cache_dir).resolve() if cache_dir else None
        self._prefetch_by_ref: dict[str, asyncio.Task] = {}
        if self._cache_dir is not None:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    def capture(self, event):
        images = direct_message_images(event)
        if not images:
            return
        for item in images:
            item.metadata['pending'] = True
        records = self.sessions.setdefault(event.unified_msg_origin, OrderedDict())
        records[str(event.message_obj.message_id)] = images
        event.set_extra('pjsk_gallery_image_sources', images)
        while len(records) > RECORD_MEMORY_LIMIT:
            records.popitem(last=False)
        event.set_extra('pjsk_gallery_image_markers', ' '.join(
            f'[gallery_image:{item.ref}]' for item in images
        ))

    def start_prefetch(self, event):
        self.start_prefetch_items(event.get_extra('pjsk_gallery_image_sources') or [])

    def start_prefetch_items(self, items):
        if self._cache_dir is None:
            return
        for item in items:
            if item.ref in self._prefetch_by_ref:
                continue
            if item.metadata.get('platform_emoji'):
                continue
            if item.metadata.get('cache_path') or item.metadata.get('resolved_path'):
                continue
            if not item.location.startswith(('http://', 'https://')):
                continue
            if self._failure_reason(item.location):
                continue
            task = asyncio.get_running_loop().create_task(self._prefetch(item))
            self._prefetch_by_ref[item.ref] = task
            task.add_done_callback(
                lambda _, ref=item.ref: self._prefetch_by_ref.pop(ref, None))

    def cleanup_cache(self):
        if self._cache_dir is None or not self._cache_dir.is_dir():
            return 0
        deadline = time.time() - CACHE_RETENTION_SECONDS
        removed = 0
        for path in self._cache_dir.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < deadline:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        if removed:
            logger.info('[PJSKPic] 清理聊天图片缓存 %s 个', removed)
        return removed

    async def _prefetch(self, item):
        try:
            path = await MediaResolver(item.location, media_type='image').to_path()
        except Exception as exc:
            self._record_failure(item.location, exc, ref=item.ref)
            return
        try:
            await asyncio.to_thread(self._remember_local, item, path)
        except Exception as exc:
            logger.warning('[PJSKPic] 写入聊天图片缓存失败 ref=%s error=%s',
                           item.ref, type(exc).__name__)

    def _remember_local(self, item, path):
        source = Path(path)
        data = source.read_bytes()
        sha256 = hashlib.sha256(data).hexdigest()
        if self._cache_dir is not None:
            target = self._cache_dir / f'{sha256}{source.suffix or ".bin"}'
            if target.resolve() != source.resolve() and not target.exists():
                target.write_bytes(data)
            item.metadata['cache_path'] = str(target)
        item.metadata['content_sha256'] = sha256
        self._forget_failure(item.location)

    async def prepare(self, event, req, *, attach_originals):
        session = event.unified_msg_origin
        records = self.sessions.setdefault(session, OrderedDict())
        aliases = self._aliases.setdefault(session, OrderedDict())
        by_ref = {}
        locations = {}
        for bucket in records.values():
            for item in bucket:
                by_ref.setdefault(item.ref, item)
                if item.location:
                    locations.setdefault(item.location, item)
                local = item.metadata.get('cache_path') or item.metadata.get('resolved_path')
                if local:
                    locations.setdefault(local, item)

        def find(location):
            item = locations.get(location)
            if item is not None:
                return item
            ref = aliases.get(self._alias_key(location))
            return by_ref.get(ref) if ref else None

        def remember(item, key):
            bucket = records.setdefault(key, [])
            if all(existing.ref != item.ref for existing in bucket):
                bucket.append(item)

        def register_alias(value, item):
            key = self._alias_key(value)
            aliases.pop(key, None)
            aliases[key] = item.ref
            while len(aliases) > ALIAS_MEMORY_LIMIT:
                aliases.popitem(last=False)

        def create(location, pending):
            item = find(location)
            if item is not None:
                return item
            item = MessageImage(Image(file=location), {
                'session_id': session, 'source_message_id': '', 'source_sender_id': '',
                'source_sender_name': '', 'source_origin': 'historical_unknown',
                'pending': pending})
            locations.setdefault(location, item)
            by_ref.setdefault(item.ref, item)
            remember(item, 'history:' + self._alias_key(location))
            return item

        chosen = {}
        visible_refs = set()
        annotated_refs = set()
        prepared_locations = {}

        async def prepare_original(location, item=None):
            if location.startswith('data:'):
                return location
            if location in prepared_locations:
                return prepared_locations[location]
            item = item or find(location)
            identity = item.location if item is not None else location
            task = self._prefetch_by_ref.get(item.ref) if item is not None else None
            if task is not None and not task.done():
                try:
                    await task
                except Exception:
                    pass
            if self._failure_reason(identity):
                if item is not None:
                    item.metadata['pending'] = False
                prepared_locations[location] = None
                return None
            source = location
            if item is not None:
                source = (item.metadata.get('cache_path')
                          or item.metadata.get('resolved_path')
                          or item.location or location)
            try:
                path = await MediaResolver(source, media_type='image').to_path()
                data = await MediaResolver(path, media_type='image').to_base64_data()
                result = data.to_data_url()
            except Exception as exc:
                # An unavailable optional history image must not break normal chat.
                logger.warning('[PJSKPic] 跳过不可用补图 ref=%s error=%s',
                               item.ref if item is not None else 'historical_unknown', type(exc).__name__)
                if item is not None:
                    item.metadata['pending'] = False
                self._record_failure(identity, exc, ref=item.ref if item is not None else '')
                result = None
            else:
                if item is not None:
                    item.metadata['resolved_path'] = path
                    if self._cache_dir is not None:
                        try:
                            await asyncio.to_thread(self._remember_local, item, path)
                        except Exception as exc:
                            logger.warning('[PJSKPic] 写入聊天图片缓存失败 ref=%s error=%s',
                                           item.ref, type(exc).__name__)
                    self._forget_failure(identity)
                    register_alias(result, item)
            prepared_locations[location] = result
            return result

        req.prompt = PROMPT_ANNOTATION_PATTERN.sub('', req.prompt or '')
        current_refs = []
        for url in list(req.image_urls or []):
            location = str(url)
            item = find(location) or create(location, pending=True)
            visible_refs.add(item.ref)
            if item.metadata.get('pending'):
                chosen[item.ref] = item
            current_refs.append(item.ref)
        if current_refs:
            req.prompt += '\n本次请求附图按顺序对应 image_ref：' + '、'.join(current_refs)

        quoted = await quoted_message_images(event)
        for item in quoted:
            item.metadata['pending'] = True
            by_ref.setdefault(item.ref, item)
            if item.location:
                locations.setdefault(item.location, item)
            chosen[item.ref] = item
            visible_refs.add(item.ref)
        if quoted:
            key = 'quoted:' + str(quoted[0].metadata.get('source_message_id', ''))
            for item in quoted:
                remember(item, key)

        req.contexts = copy.deepcopy(req.contexts or [])
        context_texts = [req.prompt or '']
        for message in req.contexts:
            if message.get('role') != 'user':
                continue
            content = message.get('content', '')
            if isinstance(content, str):
                context_texts.append(content)
                continue
            normalized = []
            for part in content:
                kind = part.get('type')
                if kind == 'text':
                    text = PROMPT_ANNOTATION_PATTERN.sub('', str(part.get('text', '')))
                    if not text.strip() or ANNOTATION_PATTERN.match(text.strip()):
                        continue
                    context_texts.append(text)
                    normalized.append(part if text == part.get('text', '') else {**part, 'text': text})
                    continue
                if kind != 'image_url':
                    normalized.append(part)
                    continue
                value = part['image_url']
                location = value['url'] if isinstance(value, dict) else value
                item = find(location)
                original = await prepare_original(location, item)
                if original is None:
                    normalized.append({'type': 'text', 'text': '[历史图片已不可用]'})
                    continue
                part['image_url'] = (
                    {**value, 'url': original} if isinstance(value, dict) else {'url': original}
                )
                if item is None:
                    item = create(location, pending=False)
                    if original != location:
                        register_alias(original, item)
                visible_refs.add(item.ref)
                if item.ref in annotated_refs:
                    normalized.append(part)
                elif item.metadata.get('pending'):
                    annotated_refs.add(item.ref)
                    chosen[item.ref] = item
                    normalized.extend([
                        {'type': 'text', 'text': f'下面这张图片的 image_ref={item.ref}'},
                        part,
                    ])
                else:
                    normalized.append(part)
            message['content'] = normalized

        extra_parts = list(req.extra_user_content_parts or [])
        req.extra_user_content_parts = []
        for part in extra_parts:
            if isinstance(part, TextPart):
                text = PROMPT_ANNOTATION_PATTERN.sub('', str(part.text))
                if not text.strip() or ANNOTATION_PATTERN.match(text.strip()):
                    continue
                part.text = text
                context_texts.append(text)
                req.extra_user_content_parts.append(part)
            elif isinstance(part, ImageURLPart):
                location = part.image_url.url
                item = find(location) or create(location, pending=False)
                visible_refs.add(item.ref)
                if item.metadata.get('pending'):
                    part.image_url.id = item.ref
                    annotated_refs.add(item.ref)
                    chosen[item.ref] = item
                    req.extra_user_content_parts.append(
                        TextPart(text=f'下面这张图片的 image_ref={item.ref}'))
                req.extra_user_content_parts.append(part)
            else:
                req.extra_user_content_parts.append(part)

        if attach_originals:
            refs = set(REF_PATTERN.findall('\n'.join(context_texts)))
            for ref, item in by_ref.items():
                if ref not in refs or ref in visible_refs:
                    continue
                original = await prepare_original(item.location or ref, item)
                if original is None:
                    continue
                visible_refs.add(ref)
                if item.metadata.get('pending'):
                    chosen[ref] = item
                    annotated_refs.add(ref)
                    req.extra_user_content_parts.extend([
                        TextPart(text=f'上下文图片原图，image_ref={ref}'),
                        ImageURLPart(image_url=ImageURLPart.ImageURL(url=original, id=ref)),
                    ])
                else:
                    req.extra_user_content_parts.append(
                        ImageURLPart(image_url=ImageURLPart.ImageURL(url=original, id=ref)))

        for item in chosen.values():
            item.metadata['pending'] = False
        return list(chosen.values())

    @staticmethod
    def _alias_key(value):
        return hashlib.sha256(str(value).encode()).hexdigest()[:32]

    def _record_failure(self, location, exc, *, ref=''):
        key = str(location)
        reason = f'{type(exc).__name__}: {str(exc)[:120]}'
        self._failures.pop(key, None)
        self._failures[key] = (time.monotonic(), reason)
        while len(self._failures) > FAILURE_MEMORY_LIMIT:
            self._failures.popitem(last=False)
        logger.warning('[PJSKPic] 图片来源解析失败，记录一次并跳过 ref=%s error=%s',
                       ref or 'unknown', type(exc).__name__)

    def _failure_reason(self, location):
        key = str(location)
        entry = self._failures.get(key)
        if entry is None:
            return None
        recorded_at, reason = entry
        if time.monotonic() - recorded_at > FAILURE_TTL_SECONDS:
            self._failures.pop(key, None)
            return None
        return reason

    def _forget_failure(self, location):
        self._failures.pop(str(location), None)
