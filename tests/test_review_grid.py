from __future__ import annotations

import asyncio
import ast
import base64
import importlib
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from PIL import Image

pkg = types.ModuleType('pjsk_grid_test_core')
pkg.__path__ = [str(Path(__file__).resolve().parents[1] / 'core')]
sys.modules[pkg.__name__] = pkg
db_module = importlib.import_module(pkg.__name__ + '.db')
qq = importlib.import_module(pkg.__name__ + '.qq_review_service')
grid = importlib.import_module(pkg.__name__ + '.review_grid_service')
renderer = importlib.import_module(pkg.__name__ + '.review_grid_renderer')


class Bot:
    def __init__(self, *, no_id=False):
        self.messages = {}
        self.counter = 100
        self.no_id = no_id
        self.fail = False

    async def call_action(self, action, **params):
        if action == 'get_msg':
            return self.messages[int(params['message_id'])]
        if self.fail:
            return {'status': 'failed', 'retcode': 1}
        assert action in ('send_group_msg', 'send_private_msg')
        self.counter += 1
        self.messages[self.counter] = {'sender': {'user_id': '999'}, 'message': params['message'],
                                     'group_id': params.get('group_id'),
                                     'message_type': 'group' if action == 'send_group_msg' else 'private'}
        return {} if self.no_id else {'data': {'message_id': self.counter}}


class Event:
    def __init__(self, bot, text='', reply=None, *, user='101', group='1', private=False):
        self.bot, self.message_str, self.user, self.group, self.private = bot, text, user, group, private
        self.unified_msg_origin = f'qq:{"FriendMessage" if private else "GroupMessage"}:{user if private else group}'
        self.message_obj = types.SimpleNamespace(raw_message={'message': [] if reply is None else [{'type': 'reply', 'data': {'id': reply}}]})
        self.sent, self.extra = [], {}

    def get_sender_id(self): return self.user
    def get_self_id(self): return '999'
    def get_group_id(self): return self.group
    def is_private_chat(self): return self.private
    def get_extra(self, key): return self.extra.get(key)
    def set_extra(self, key, value): self.extra[key] = value
    def get_messages(self): return []
    def stop_event(self): self.extra['stopped'] = True

    async def send(self, chain):
        self.sent.extend(chain.chain)

    def text(self):
        return '\n'.join(str(getattr(part, 'text', '')) for part in self.sent)


class GridTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = db_module.ImageIndexDB(self.root / 'db.sqlite')
        self.tag = self.db.get_or_create_tag('初音未来', tag_type='character')
        self.other = self.db.get_or_create_tag('镜音铃', tag_type='character')
        self.db.add_alias('初音未来', '初音')
        self.config = {'qq_review_enabled': True}
        self.now = 1000
        self.reviews = qq.QQReviewSessionService(self.db, self.config, clock=lambda: self.now)
        self.service = grid.ReviewGridService(self.db, self.reviews, self.config, self.root, self.resolve, clock=lambda: self.now)
        self.bot = Bot()

    def tearDown(self): self.temp.cleanup()

    def resolve(self, name, *, platform):
        match = self.db.resolve_tag(name, allow_fuzzy=False)
        return (match.tag_name, match.match_type, []) if match.matched else (None, '', [])

    def add(self, n, *, platform='pixiv', tag=None, status='pending'):
        path = self.root / f'{n}.png'
        Image.new('RGB', (48, 72), (n % 255, 30, 60)).save(path)
        image_id = self.db.upsert_image(file_path=str(path), file_name=path.name, sha256='sha' + str(n), width=48, height=72, format_='png')
        post = f'https://www.pixiv.net/artworks/{n}' if platform == 'pixiv' else f'https://www.xiaohongshu.com/explore/{n}'
        self.db.upsert_source(image_id, platform, post, f'https://images.test/{n}', raw_tags=[])
        tag = self.tag if tag is None else tag
        self.db.link_image_tag(image_id, tag, source_type='crawl:' + platform, review_status=status)
        self.db.create_review_task(image_id, tag, status, reason='initial')
        return image_id

    async def start(self, platform='Pixiv', query='', **kwargs):
        event = Event(self.bot, **kwargs)
        await self.service.start(event, platform, query)
        return event, self.bot.counter

    async def reply(self, message, text, **kwargs):
        event = Event(self.bot, text, message, **kwargs)
        result = await self.service.handle_reply(event)
        return result, event

    def page(self, message):
        token = self.service._messages.get(('qq:GroupMessage:1', str(message)))
        return self.service._pages[token]

    async def test_page_sizes_mixed_pool_and_tag_filter(self):
        for n in range(10): self.add(n)
        self.add(11, platform='xiaohongshu')
        self.add(12, tag=self.other)
        self.add(13, status='manual_rejected')
        self.add(14, status='approved')
        _, first = await self.start(query='初音')
        page = self.page(first)
        self.assertEqual(9, len(page.rows))
        self.assertEqual('初音未来', page.browse.tag_name)
        await self.reply(first, '下一页')
        self.assertEqual(2, len(self.page(self.bot.counter).rows))
        _, xhs = await self.start('小红书')
        self.assertEqual('all', self.page(xhs).browse.platform)
        self.assertEqual(9, len(self.page(xhs).rows))

    async def test_role_alias_mixes_sources_and_deduplicates_shared_image(self):
        self.db.add_alias('初音未来', 'miku')
        ena = self.db.get_or_create_tag('东云绘名', tag_type='character')
        self.db.add_alias('东云绘名', 'ena')
        first = self.add(1)
        second = self.add(2, platform='xiaohongshu')
        self.add(3, tag=ena)
        self.db.upsert_source(first, 'xiaohongshu', 'https://www.xiaohongshu.com/explore/shared', 'https://images.test/shared', raw_tags=[])
        _, message = await self.start('miku')
        self.assertEqual([first, second], [r['image_id'] for r in self.page(message).rows])
        self.assertEqual({'pixiv', 'xiaohongshu'}, set(self.page(message).rows[0]['review_platforms']))
        body = ''.join(s['data']['text'] for s in self.bot.messages[message]['message'] if s['type'] == 'text')
        self.assertNotIn('Pixiv', body)
        self.assertNotIn('小红书', body)
        self.assertNotIn('xiaohongshu', body)
        self.assertIn('初音未来', body)
        _, ena_message = await self.start('ena')
        self.assertEqual(1, len(self.page(ena_message).rows))
        self.assertEqual('东云绘名', self.page(ena_message).browse.tag_name)

    async def test_unified_rejection_blocks_both_sources_atomically(self):
        image_id = self.add(1)
        xhs_url = 'https://www.xiaohongshu.com/explore/shared'
        self.db.upsert_source(image_id, 'xiaohongshu', xhs_url, 'https://images.test/shared', raw_tags=[])
        _, message = await self.start()
        _, event = await self.reply(message, '拒绝1 质量不好')
        self.assertIn('已整图拒绝', event.text())
        self.assertNotIn('Pixiv', event.text())
        self.assertNotIn('小红书', event.text())
        self.assertTrue(self.db.is_rejected_source_post_url('https://www.pixiv.net/artworks/1', platform='pixiv'))
        self.assertTrue(self.db.is_rejected_source_post_url(xhs_url, platform='xiaohongshu'))
        self.assertFalse(self.db.get_review_grid_page(platform='all')['rows'])

    async def test_unified_approval_keeps_source_metadata(self):
        image_id = self.add(1, platform='xiaohongshu')
        self.db.upsert_source(image_id, 'pixiv', 'https://www.pixiv.net/artworks/100', 'https://images.test/100', raw_tags=['raw'])
        before = self.db.get_image_detail(image_id, sync_files=False)['sources']
        _, message = await self.start()
        _, event = await self.reply(message, '通过1 初音')
        self.assertIn('已通过', event.text())
        self.assertEqual(before, self.db.get_image_detail(image_id, sync_files=False)['sources'])
        self.assertFalse(self.db.is_open_review_image(image_id, platform='all'))

    def test_one_blocked_source_other_available_and_web_rejection_unchanged(self):
        image_id = self.add(1)
        xhs_url = 'https://www.xiaohongshu.com/explore/shared'
        self.db.upsert_source(image_id, 'xiaohongshu', xhs_url, 'https://images.test/shared', raw_tags=[])
        self.db.reject_image_source(image_id, platform='pixiv')
        self.assertFalse(self.db.is_rejected_source_post_url(xhs_url, platform='xiaohongshu'))
        self.db.create_review_task(image_id, self.other, 'pending')
        rows = self.db.get_review_grid_page(platform='all')['rows']
        self.assertEqual([image_id], [r['image_id'] for r in rows])
        self.assertEqual(['xiaohongshu'], rows[0]['review_platforms'])

    def test_second_source_failure_rolls_back_whole_rejection(self):
        image_id = self.add(1)
        xhs_url = 'https://www.xiaohongshu.com/explore/shared'
        self.db.upsert_source(image_id, 'xiaohongshu', xhs_url, 'https://images.test/shared', raw_tags=[])
        original = self.db._upsert_rejected_source_conn
        count = 0
        def fail_second(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError('simulated write failure')
            return original(*args, **kwargs)
        with patch.object(self.db, '_upsert_rejected_source_conn', side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.db.reject_image_source(image_id, platform='all', require_open_review=True)
        self.assertFalse(self.db.is_rejected_source_post_url('https://www.pixiv.net/artworks/1', platform='pixiv'))
        self.assertFalse(self.db.is_rejected_source_post_url(xhs_url, platform='xiaohongshu'))
        self.assertTrue(self.db.is_open_review_image(image_id, platform='all'))

    async def test_random_pool_and_real_main_card_hide_source(self):
        ids = {self.add(1), self.add(2, platform='xiaohongshu')}
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'main.py').read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PJSKPicPlugin')
        handler = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_send_qq_review_session')
        handler.decorator_list = []
        scope = {'MessageChain': grid.MessageChain, 'QQReviewSessionService': qq.QQReviewSessionService,
                 'AstrMessageEvent': Event, 'QQReviewSession': qq.QQReviewSession}
        exec(compile(ast.Module(body=[handler], type_ignores=[]), 'main_card', 'exec'), scope)
        plugin = types.SimpleNamespace(db=self.db,
            _review_image_path=lambda i: Path(self.db.get_image_file_path(i)),
            _qq_review_source_term_limit=lambda: 12,
            llm_image_review_service=types.SimpleNamespace(latest_suggestion=lambda i: None))
        session, total = await self.reviews.claim_next(origin='qq:GroupMessage:1', reviewer_id='101', platform='all')
        self.assertEqual(2, total)
        event = Event(self.bot)
        self.assertTrue(await scope[handler.name](plugin, event, session, remaining=total))
        for word in ('Pixiv', '小红书', 'xiaohongshu', '来源：', 'https://'):
            self.assertNotIn(word, event.text())
        self.assertIn('群友审核', event.text())
        ok, _ = await self.reviews.approve_current(origin='qq:GroupMessage:1', reviewer_id='101', tag_name='初音未来')
        self.assertTrue(ok)
        following, total = await self.reviews.claim_next(origin='qq:GroupMessage:1', reviewer_id='101', platform='all')
        self.assertEqual(ids - {session.image_id}, {following.image_id})
        self.assertEqual(1, total)

    async def test_zero_one_eight_nine_images_and_layout(self):
        event, _ = await self.start()
        self.assertIn('没有待审核', event.text())
        for n in range(9):
            self.add(n)
            if n in (0, 7, 8):
                _, message = await self.start()
                image_segment = next(s for s in self.bot.messages[message]['message'] if s['type'] == 'image')
                with Image.open(BytesIO(base64.b64decode(image_segment['data']['file'][9:]))) as image:
                    image.load()
                    self.assertEqual((1020, 1282), image.size)
                self.assertEqual(n + 1, len(self.page(message).rows))
        self.assertFalse(list((self.root / 'review_grid').glob('*.png')))
        self.assertFalse(self.reviews._claims)

    async def test_approve_alias_receipt_repeat_and_static_numbers(self):
        ids = [self.add(n) for n in range(9)]
        _, message = await self.start()
        ok, event = await self.reply(message, '通过3 初音')
        self.assertTrue(ok)
        self.assertIn(f'第3张 → #{ids[2]}', event.text())
        self.assertIn('还剩8张', event.text())
        before = [dict(r) for r in self.db.get_review_tasks_for_image(ids[2])]
        _, again = await self.reply(message, '通过3 初音')
        self.assertIn('变化', again.text())
        self.assertEqual(before, [dict(r) for r in self.db.get_review_tasks_for_image(ids[2])])
        await self.reply(message, '刷新本页')
        refreshed = self.page(self.bot.counter)
        self.assertEqual(ids, [r['image_id'] for r in refreshed.rows])
        self.assertFalse(refreshed.rows[2]['open'])

    async def test_cursor_after_mutation_and_new_image_bound(self):
        ids = [self.add(n) for n in range(10)]
        _, message = await self.start()
        await self.reply(message, '拒绝1 不适合收藏')
        self.add(50)
        await self.reply(message, '下一页')
        next_message = self.bot.counter
        self.assertEqual([ids[-1]], [r['image_id'] for r in self.page(next_message).rows])
        await self.reply(next_message, '上一页')
        self.assertEqual(ids[:9], [r['image_id'] for r in self.page(self.bot.counter).rows])
        self.assertFalse(self.db.is_open_review_image(ids[0], platform='pixiv'))

    async def test_partial_web_change_still_pending_cannot_be_overwritten(self):
        image_id = self.add(1)
        self.db.create_review_task(image_id, self.other, 'pending', reason='second')
        _, message = await self.start()
        # A Web operation changes one task, while another task is still pending.
        other_db = db_module.ImageIndexDB(self.db.db_path)
        with other_db._connect() as conn:
            conn.execute("UPDATE review_tasks SET status='uncertain',reason='web changed' WHERE image_id=? AND tag_id=?", (image_id, self.other))
        _, event = await self.reply(message, '通过1 初音')
        self.assertIn('变化', event.text())
        self.assertTrue(self.db.is_open_review_image(image_id, platform='pixiv'))
        self.assertTrue(all(r['manual_result'] == '' for r in self.db.get_review_tasks_for_image(image_id)))

    async def test_random_claim_conflict_and_own_claim_release(self):
        image_id = self.add(1)
        _, message = await self.start()
        await self.reviews.claim_next(origin='qq:GroupMessage:1', reviewer_id='202')
        _, event = await self.reply(message, '通过1 初音')
        self.assertIn('其他人', event.text())
        self.assertTrue(self.db.is_open_review_image(image_id, platform='pixiv'))
        await self.reviews.release_current(origin='qq:GroupMessage:1', reviewer_id='202')
        await self.reviews.claim_next(origin='qq:GroupMessage:1', reviewer_id='101')
        _, event = await self.reply(message, '通过1 初音')
        self.assertIn('已通过', event.text())
        self.assertFalse(self.reviews._claims)

    async def test_two_database_transactions_only_one_accepts_original_version(self):
        image_id = self.add(1)
        version = self.db.get_review_grid_snapshots([image_id], platform='pixiv')[0]['version']
        other_db = db_module.ImageIndexDB(self.db.db_path)
        results = await asyncio.gather(
            asyncio.to_thread(self.db.apply_image_review, image_id, platform='pixiv',
                              selected_tag_names=['初音未来'], require_open_review=True, expected_review_version=version),
            asyncio.to_thread(other_db.reject_image_source, image_id, platform='pixiv',
                              require_open_review=True, expected_review_version=version))
        self.assertEqual(1, sum(ok for ok, _ in results))
        self.assertEqual(1, sum(result.get('code') == 'stale_review' for _, result in results))

    async def test_parallel_different_reviewers_one_commit(self):
        self.add(1)
        _, one = await self.start()
        _, two = await self.start(user='202')
        results = await asyncio.gather(self.reply(one, '通过1 初音'), self.reply(two, '通过1 初音', user='202'))
        self.assertEqual(1, sum('已通过' in e.text() for _, e in results))
        self.assertEqual(1, sum('变化' in e.text() for _, e in results))

    async def test_foreign_cross_group_expired_and_restart(self):
        self.add(1)
        _, message = await self.start()
        _, event = await self.reply(message, '通过1 初音', user='202')
        self.assertIn('发起人', event.text())
        consumed, _ = await self.reply(message, '通过1 初音', group='2')
        self.assertFalse(consumed)
        self.now += 601
        _, event = await self.reply(message, '通过1 初音')
        self.assertIn('过期', event.text())
        _, message = await self.start()
        await self.service.clear()
        _, event = await self.reply(message, '通过1 初音')
        self.assertIn('过期', event.text())

    async def test_permission_disabled_and_bad_input_no_writes(self):
        image_id = self.add(1)
        _, message = await self.start()
        for text in ('通过99 初音', '通过1 未知', '通过1', '全通过'):
            consumed, _ = await self.reply(message, text)
            self.assertTrue(consumed)
            self.assertTrue(self.db.is_open_review_image(image_id, platform='pixiv'))
        self.config['qq_review_enabled'] = False
        _, event = await self.reply(message, '拒绝1')
        self.assertIn('未启用', event.text())
        self.assertTrue(self.db.is_open_review_image(image_id, platform='pixiv'))

    async def test_skip_no_db_write_and_view_no_management_details(self):
        image_id = self.add(1)
        _, message = await self.start()
        version = self.db.get_review_grid_snapshots([image_id], platform='pixiv')[0]['version']
        _, event = await self.reply(message, '跳过1')
        self.assertIn('跳过', event.text())
        self.assertEqual(version, self.db.get_review_grid_snapshots([image_id], platform='pixiv')[0]['version'])
        _, event = await self.reply(message, '看第1张')
        self.assertIn(f'#{image_id}', event.text())
        self.assertNotIn(str(self.root), event.text())
        self.assertNotIn('images.test', event.text())

    async def test_missing_file_placeholder_cannot_approve_and_repair(self):
        image_id = self.add(1)
        path = self.root / '1.png'
        path.unlink()
        _, message = await self.start()
        self.assertNotIn(image_id, self.page(message).readable)
        _, event = await self.reply(message, '通过1 初音')
        self.assertIn('文件不可读取', event.text())
        Image.new('RGB', (40, 60), 'blue').save(path)
        await self.reply(message, '刷新本页')
        _, event = await self.reply(self.bot.counter, '通过1 初音')
        self.assertIn('已通过', event.text())

    async def test_changed_file_requires_refresh(self):
        self.add(1)
        _, message = await self.start()
        Image.new('RGB', (90, 90), 'green').save(self.root / '1.png')
        _, event = await self.reply(message, '通过1 初音')
        self.assertIn('文件不可读取或已变化', event.text())

    async def test_fallback_token_private_and_forged_message(self):
        self.add(1)
        self.bot.no_id = True
        _, message = await self.start(private=True)
        consumed, event = await self.reply(message, '通过1 初音', private=True)
        self.assertTrue(consumed)
        self.assertIn('已通过', event.text())
        self.bot.messages[message]['sender'] = {'user_id': '101'}
        consumed, _ = await self.reply(message, '拒绝1', private=True)
        self.assertFalse(consumed)

    async def test_group_fallback_token_and_short_circuit_main_route(self):
        self.add(1)
        self.bot.no_id = True
        _, message = await self.start()
        consumed, event = await self.reply(message, '看第1张')
        self.assertTrue(consumed)
        self.assertIn('候选', event.text())
        # Exercise the real main handler body without instantiating live workers.
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'main.py').read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PJSKPicPlugin')
        handler = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'send_image_by_natural_language')
        handler.decorator_list = []
        handler.args.args[1].annotation = None
        scope = {}
        exec(compile(ast.Module(body=[handler], type_ignores=[]), 'main_handler', 'exec'), scope)
        plugin = types.SimpleNamespace(review_grid_service=self.service)
        event = Event(self.bot, '看第1张', message)
        await scope[handler.name](plugin, event)
        self.assertTrue(event.extra['stopped'])

    def test_corrupt_file_and_exif_rotation(self):
        image_id = self.add(1)
        (self.root / '1.png').write_bytes(b'invalid image')
        rows = self.db.get_review_grid_snapshots([image_id], platform='pixiv')
        result = renderer.render_review_grid(rows, self.root / 'corrupt.png', heading='测试')
        self.assertFalse(result['readable'])
        path = self.root / 'rotated.jpg'
        exif = Image.Exif()
        exif[274] = 6
        Image.new('RGB', (40, 80), 'red').save(path, exif=exif)
        image = renderer.preview_image(str(path))
        self.assertEqual((80, 40), image.size)
        image.close()

    async def test_failed_send_does_not_publish_and_cleans_output(self):
        self.add(1)
        self.bot.fail = True
        with self.assertRaises(RuntimeError):
            await self.start()
        self.assertFalse(self.service._sessions)
        self.assertFalse(self.service._pages)
        self.assertFalse(list((self.root / 'review_grid').glob('*.png')))

    async def test_ended_and_replaced_session_old_page_invalid(self):
        self.add(1)
        _, old = await self.start()
        await self.start()
        _, event = await self.reply(old, '通过1 初音')
        self.assertIn('过期', event.text())
        current = self.bot.counter
        await self.reply(current, '结束审图列表')
        _, event = await self.reply(current, '拒绝1')
        self.assertIn('过期', event.text())

    def test_blocked_source_and_inactive_not_in_page(self):
        image_id = self.add(1)
        self.db.reject_image_source(image_id, platform='pixiv')
        self.db.create_review_task(image_id, self.other, 'pending')
        other = self.add(2)
        with self.db._connect() as conn: conn.execute('UPDATE images SET is_active=0 WHERE id=?', (other,))
        self.assertFalse(self.db.get_review_grid_page(platform='pixiv')['rows'])


class ParserTests(unittest.TestCase):
    def test_only_explicit_actions(self):
        self.assertEqual(('approve', 3, '初音未来'), grid.parse_grid_action('通过３ 初音未来'))
        self.assertEqual(('view', 3, ''), grid.parse_grid_action('看第3张'))
        self.assertIsNone(grid.parse_grid_action('看图1234'))
        self.assertIsNone(grid.parse_grid_action('通过3初音未来'))
        self.assertIsNone(grid.parse_grid_action('收1、3'))
