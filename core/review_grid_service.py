from __future__ import annotations

import asyncio
import base64
import re
import time
import unicodedata
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger
from astrbot.api.message_components import At
from astrbot.core.message.message_event_result import MessageChain

from .message_images import component_kind, original_chain
from .review_grid_renderer import file_stamp, preview_image, render_review_grid

GRID_HEADER = '【待审九宫格】'
TOKEN_PATTERN = re.compile(r'九宫格批次：(grid:[0-9a-f]{32})\s*$')
PLATFORMS = {'pixiv': 'pixiv', 'p站': 'pixiv', '小红书': 'xiaohongshu',
             'xhs': 'xiaohongshu', 'xiaohongshu': 'xiaohongshu', 'rednote': 'xiaohongshu'}


def parse_grid_action(text: str):
    text = unicodedata.normalize('NFKC', str(text or '')).strip(' \t\n。！!')
    simple = {'下一页': 'next', '上一页': 'prev', '刷新本页': 'refresh', '结束审图列表': 'end'}
    if text in simple:
        return simple[text], 0, ''
    match = re.fullmatch(r'看第\s*([0-9]+)\s*张', text)
    if match:
        return 'view', int(match[1]), ''
    match = re.fullmatch(r'(通过|拒绝|跳过)\s*([0-9]+)(?:\s+(.+))?', text)
    if match:
        return {'通过': 'approve', '拒绝': 'reject', '跳过': 'skip'}[match[1]], int(match[2]), match[3] or ''
    if text in {'通过', '拒绝', '全通过', '全部通过'}:
        return 'invalid', 0, ''
    return None


@dataclass
class GridBrowse:
    origin: str
    user: str
    platform: str
    tag_id: int
    tag_name: str
    upper_id: int
    expires: float
    pages: OrderedDict = field(default_factory=OrderedDict)
    skipped: set[int] = field(default_factory=set)


@dataclass
class GridPage:
    browse: GridBrowse
    index: int
    rows: list[dict]
    readable: set[int]
    stamps: dict


class ReviewGridService:
    """Bounded, user-scoped page snapshots; mutations reuse the QQ review service."""

    def __init__(self, db, reviews, config, data_dir: Path, resolve_tag, *, clock=time.monotonic):
        self.db, self.reviews, self.config = db, reviews, config
        self.data_dir, self.resolve_tag, self.clock = Path(data_dir), resolve_tag, clock
        self._sessions: OrderedDict = OrderedDict()
        self._pages: OrderedDict[str, GridPage] = OrderedDict()
        self._messages: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()

    def enabled(self):
        return bool(self.config.get('qq_review_enabled', True))

    def ttl(self):
        return min(max(int(self.config.get('qq_review_claim_ttl_seconds', 600) or 600), 60), 3600)

    @staticmethod
    def identity(event):
        return str(event.unified_msg_origin), str(event.get_sender_id())

    @staticmethod
    def platform_label(platform):
        return '小红书' if platform == 'xiaohongshu' else 'Pixiv'

    async def _notice(self, event, text):
        await event.send(MessageChain().message(text))

    def _trim(self):
        for key, browse in list(self._sessions.items()):
            if browse.expires <= self.clock():
                self._sessions.pop(key)
        while len(self._sessions) > 100:
            self._sessions.popitem(last=False)
        live = {id(s) for s in self._sessions.values()}
        for token, page in list(self._pages.items()):
            if id(page.browse) not in live or page.index not in page.browse.pages:
                self._pages.pop(token)
        while len(self._pages) > 256:
            self._pages.popitem(last=False)
        self._messages = {key: token for key, token in self._messages.items() if token in self._pages}

    async def clear(self):
        async with self._lock:
            self._sessions.clear()
            self._pages.clear()
            self._messages.clear()

    async def start(self, event, platform_or_tag='', candidate_tag=''):
        async with self._lock:
            if not self.enabled():
                await self._notice(event, '群友审图当前未启用。')
                return
            first = str(platform_or_tag or '').strip()
            platform = PLATFORMS.get(first.casefold(), 'pixiv')
            query = str(candidate_tag or '').strip() if first.casefold() in PLATFORMS else first
            tag_id, tag_name = 0, ''
            if query:
                tag_name, _, candidates = self.resolve_tag(query, platform=platform)
                if not tag_name:
                    hint = '；候选：' + '、'.join(candidates) if candidates else ''
                    await self._notice(event, f'没有精确找到角色或别名：{query}{hint}')
                    return
                tag = self.db.get_tag_row(tag_name)
                if not tag or tag['status'] != 'active':
                    await self._notice(event, '这个标签未启用。')
                    return
                tag_id = int(tag['id'])
            result = await asyncio.to_thread(self.db.get_review_grid_page, platform=platform, tag_id=tag_id)
            if not result['rows']:
                await self._notice(event, '当前筛选下没有待审核图片。')
                return
            origin, user = self.identity(event)
            browse = GridBrowse(origin, user, platform, tag_id, tag_name, result['upper_id'], self.clock() + self.ttl())
            browse.pages[1] = [row['image_id'] for row in result['rows']]
            # Only publish the new session after its first message was sent successfully.
            await self._send_page(event, browse, 1, result['rows'], result['total'])
            self._sessions[(origin, user)] = browse
            self._sessions.move_to_end((origin, user))
            self._trim()

    async def _send_page(self, event, browse, index, rows, total):
        rows = [dict(row) for row in rows]
        for row in rows:
            if row['image_id'] in browse.skipped and row['open']:
                row['status_label'] = '本轮跳过'
        heading = f'{self.platform_label(browse.platform)} · {browse.tag_name or "全部"} · 第{index}组 · 本页{len(rows)}张'
        path = self.data_dir / 'review_grid' / f'{uuid.uuid4().hex}.png'
        task = asyncio.create_task(asyncio.to_thread(render_review_grid, rows, path, heading=heading))
        try:
            try:
                rendered = await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            token = 'grid:' + uuid.uuid4().hex
            text = (GRID_HEADER + heading + f'；查询时待审约{total}张\n'
                    '引用本条：看第3张；通过3 角色名；拒绝3 [原因]；跳过3\n'
                    '下一页 / 上一页 / 刷新本页 / 结束审图列表。仅发起人可操作。\n'
                    '拒绝会屏蔽该平台来源；标签认错请指定正确角色通过。\n'
                    + f'九宫格批次：{token}')
            message_id = await self._send_grid_message(event, path, text)
            self._pages[token] = GridPage(browse, index, rows, set(rendered['readable']), rendered['stamps'])
            if message_id:
                self._messages[(browse.origin, message_id)] = token
            browse.expires = self.clock() + self.ttl()
        finally:
            path.unlink(missing_ok=True)

    async def _send_grid_message(self, event, path, text):
        bot = getattr(event, 'bot', None)
        if bot is not None:
            segments = [{'type': 'text', 'data': {'text': text}},
                        {'type': 'image', 'data': {'file': 'base64://' + base64.b64encode(path.read_bytes()).decode('ascii')}}]
            if event.is_private_chat():
                action, params = 'send_private_msg', {'user_id': int(event.get_sender_id())}
            else:
                segments.insert(0, {'type': 'at', 'data': {'qq': str(event.get_sender_id())}})
                action, params = 'send_group_msg', {'group_id': int(event.get_group_id())}
            result = await bot.call_action(action, message=segments, **params)
            if isinstance(result, dict) and (result.get('status') == 'failed' or result.get('retcode', 0) not in (0, '0', None)):
                raise RuntimeError('grid message send rejected')
            payload = result.get('data', result) if isinstance(result, dict) else {}
            return str(payload.get('message_id') or '') if isinstance(payload, dict) else ''
        # Framework fallback preserves image and token; quote lookup uses get_msg when available.
        chain = MessageChain()
        if not event.is_private_chat():
            chain.chain.append(At(qq=str(event.get_sender_id())))
        await event.send(chain.message(text).file_image(str(path)))
        return ''

    async def _quoted_token(self, event):
        reply_id = ''
        for part in original_chain(event):
            if component_kind(part) == 'reply':
                reply_id = str((part.get('data') or {}).get('id') or '') if isinstance(part, dict) else str(getattr(part, 'id', '') or '')
                break
        if not reply_id:
            return None
        token = self._messages.get((str(event.unified_msg_origin), reply_id))
        if token:
            return token
        bot = getattr(event, 'bot', None)
        if not bot:
            return None
        try:
            result = await bot.call_action('get_msg', message_id=int(reply_id))
            payload = result.get('data', result)
            if str((payload.get('sender') or {}).get('user_id') or '') != str(event.get_self_id()):
                return None
            if event.is_private_chat():
                if payload.get('message_type') != 'private':
                    return None
            elif str(payload.get('group_id') or '') != str(event.get_group_id()):
                return None
            chunks, ats = [], []
            for part in payload.get('message') or []:
                if isinstance(part, dict):
                    if part.get('type') == 'text':
                        chunks.append(str((part.get('data') or {}).get('text') or ''))
                    elif part.get('type') == 'at':
                        ats.append(str((part.get('data') or {}).get('qq') or ''))
            text = ''.join(chunks)
            match = TOKEN_PATTERN.search(text)
            if GRID_HEADER not in text or not match:
                return None
            if not event.is_private_chat() and str(event.get_sender_id()) not in ats:
                return 'foreign'
            return match[1]
        except Exception:
            return None

    async def handle_reply(self, event):
        if event.get_extra('pjsk_review_grid_handled'):
            return True
        action = parse_grid_action(event.message_str)
        if not action or str(event.get_sender_id()) == str(event.get_self_id()):
            return False
        async with self._lock:
            self._trim()
            token = await self._quoted_token(event)
            if token is None:
                return False
            event.set_extra('pjsk_review_grid_handled', True)
            page = self._pages.get(token)
            if not self.enabled():
                await self._notice(event, '群友审图当前未启用。')
                return True
            if token == 'foreign' or (page and (page.browse.origin, page.browse.user) != self.identity(event)):
                await self._notice(event, '这份九宫格只接受发起人在原会话中的操作。')
                return True
            if not page:
                await self._notice(event, '这份审图列表已过期或Bot已重启，请重新发送 .pp 审图列表。')
                return True
            try:
                await self._apply(event, page, action)
            except Exception:
                logger.error('[PJSKPic] 九宫格处理失败', exc_info=True)
                await self._notice(event, '九宫格处理失败，请刷新本页核对状态后再试。')
            return True

    async def _apply(self, event, page, command):
        action, number, argument = command
        browse = page.browse
        if action == 'end':
            self._sessions.pop((browse.origin, browse.user), None)
            self._trim()
            await self._notice(event, '已结束这份审图列表。')
            return
        if action in {'next', 'prev', 'refresh'}:
            index = page.index + (1 if action == 'next' else -1 if action == 'prev' else 0)
            if index < 1:
                await self._notice(event, '已经是第一组。')
                return
            if index in browse.pages:
                rows = await asyncio.to_thread(self.db.get_review_grid_snapshots, browse.pages[index], platform=browse.platform)
                total = await asyncio.to_thread(self.db.count_open_review_images, platform=browse.platform,
                                               candidate_tag_id=browse.tag_id or None)
            elif action == 'next' and page.index == max(browse.pages):
                result = await asyncio.to_thread(self.db.get_review_grid_page, platform=browse.platform,
                                                tag_id=browse.tag_id, upper_id=browse.upper_id,
                                                after_id=max(browse.pages[page.index]))
                rows, total = result['rows'], result['total']
                if not rows:
                    await self._notice(event, '本轮没有更多待审图片；新入图请重新打开列表。')
                    return
            else:
                await self._notice(event, '这组历史页已过期，请重新打开列表。')
                return
            await self._send_page(event, browse, index, rows, total)
            browse.pages[index] = [row['image_id'] for row in rows]
            while len(browse.pages) > 12:
                browse.pages.popitem(last=False)
            retained_ids = {image_id for ids in browse.pages.values() for image_id in ids}
            browse.skipped.intersection_update(retained_ids)
            self._trim()
            return
        if action == 'invalid':
            await self._notice(event, '请指定序号及最终标签，例如「通过3 初音未来」，不支持全部通过。')
            return
        if number < 1 or number > len(page.rows):
            await self._notice(event, f'本页只有 {len(page.rows)} 张，编号有误，未作修改。')
            return
        row = page.rows[number - 1]
        image_id = row['image_id']
        if action == 'skip':
            browse.skipped.add(image_id)
            browse.expires = self.clock() + self.ttl()
            await self._notice(event, f'第{number}张 → #{image_id}，本轮跳过，图库审核状态未改变。')
            return
        if action == 'view':
            current = (await asyncio.to_thread(self.db.get_review_grid_snapshots, [image_id], platform=browse.platform))[0]
            if not current['available']:
                await self._notice(event, '这张图片已不可用，请刷新本页。')
                return
            try:
                preview = await asyncio.to_thread(preview_image, current['file_path'])
                preview.close()
            except (OSError, ValueError):
                await self._notice(event, f'图片 #{image_id} 文件不可读取。')
                return
            await event.send(MessageChain().file_image(current['file_path']))
            await self._notice(event, f'第{number}张 → #{image_id} · {current["status_label"]}\n候选：'
                               + ('、'.join(current['candidate_names']) or '无待审候选标签'))
            browse.expires = self.clock() + self.ttl()
            return
        if not row['open']:
            await self._notice(event, '展示时这张图已经处理或不可用，请刷新本页。')
            return
        tag_name = ''
        if action == 'approve':
            if not argument:
                await self._notice(event, '请指定最终主标签，例如「通过3 初音未来」。')
                return
            tag_name, _, _ = self.resolve_tag(argument, platform=browse.platform)
            if not tag_name:
                await self._notice(event, '未精确识别最终主标签；首版每次指定一个主标签，多人多标签请用Web审核或跳过。')
                return
            target = self.db.get_tag_row(tag_name)
            if not target or target['status'] != 'active':
                await self._notice(event, '最终标签当前未启用。')
                return
            if image_id not in page.readable or file_stamp(row['file_path']) != page.stamps.get(image_id):
                await self._notice(event, '图片文件不可读取或已变化，请刷新本页后再审核。')
                return
        ok, result = await self.reviews.apply_grid_review(
            origin=browse.origin, reviewer_id=browse.user, image_id=image_id, platform=browse.platform,
            expected_version=row['version'], tag_name=tag_name, reject=action == 'reject', reason=argument if action == 'reject' else '')
        if not ok:
            await self._notice(event, result.get('message') or '审核未完成，请刷新本页。')
            return
        browse.expires = self.clock() + self.ttl()
        current = await asyncio.to_thread(self.db.get_review_grid_snapshots, [r['image_id'] for r in page.rows], platform=browse.platform)
        remaining = sum(r['open'] and r['image_id'] not in browse.skipped for r in current)
        outcome = f'已通过：{tag_name}' if action == 'approve' else f'已整图拒绝；该{self.platform_label(browse.platform)}来源将被屏蔽'
        await self._notice(event, f'第{number}张 → #{image_id}，{outcome}；本页还剩{remaining}张待审。')
