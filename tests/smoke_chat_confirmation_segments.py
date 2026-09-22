"""Run inside AstrBot: real components/importer, captured sends, isolated database."""
from __future__ import annotations

import asyncio
import base64
import importlib
import json
import sys
import tempfile
import types
from pathlib import Path

from PIL import Image as PillowImage
from astrbot.api.message_components import Image, Plain

pkg = types.ModuleType('pjsk_confirm_smoke_core')
pkg.__path__ = [str(Path(__file__).resolve().parents[1] / 'core')]
sys.modules[pkg.__name__] = pkg
DB = importlib.import_module(pkg.__name__ + '.db').ImageIndexDB
Importer = importlib.import_module(pkg.__name__ + '.importer').ImportedImageService
Confirm = importlib.import_module(pkg.__name__ + '.chat_image_confirm_service').ChatImageConfirmService
SESSION = 'aiocqhttp:GroupMessage:99999'


class BotCapture:
    def __init__(self):
        self.sent = []

    async def send_group_msg(self, **kwargs):
        self.sent.append(kwargs['message'])
        return {'message_id': 5000 + len(self.sent)}


class ContextCapture:
    def __init__(self, bot=None):
        self.sent = []
        self.platform_manager = types.SimpleNamespace(platform_insts=[] if bot is None else [
            types.SimpleNamespace(meta=lambda: types.SimpleNamespace(id='aiocqhttp'), bot=bot),
        ])

    async def send_message(self, session, chain):
        assert session == SESSION
        self.sent.append(chain)
        return True


class ReplyEvent:
    unified_msg_origin = SESSION
    message_obj = types.SimpleNamespace(raw_message={
        'message': [{'type': 'reply', 'data': {'id': '5001'}}],
    })
    message_str = '收第2张'
    bot = None

    def is_private_chat(self): return False
    def get_self_id(self): return '20000'
    def get_sender_id(self): return '10000'
    def get_group_id(self): return '99999'
    def get_messages(self): return [{'type': 'reply', 'data': {'id': '5001'}}]


async def main():
    with tempfile.TemporaryDirectory(prefix='pjsk-confirm-smoke-') as tmp:
        root = Path(tmp)
        db = DB(root / 'index.db')
        tag_id = db.get_or_create_tag('宵崎奏', tag_type='character')
        tags = lambda: [{'tag_id': tag_id, 'name': '宵崎奏', 'tag_type': 'character'}]
        importer = Importer(db, root, enable_phash_dedupe=False)
        paths = []
        for i in range(2):
            path = root / f'candidate-{i}.png'
            PillowImage.new('RGB', (20, 20), (50 + i * 70, 10, 20)).save(path)
            paths.append(path)
        # Existing image proves the duplicate result comes from the importer.
        seed = await importer.import_local_file(paths[0], platform='chat')
        # Shift the image sequence independently of the candidate sequence.
        spare = root / 'spare.png'
        PillowImage.new('RGB', (20, 20), (5, 6, 7)).save(spare)
        await importer.import_local_file(spare, platform='chat')
        candidates = []
        for index, path in enumerate(paths, 1):
            row = db.create_chat_image_candidate(
                ref=f'smoke:{index}', session_id=SESSION, group_id='99999',
                platform='aiocqhttp', sender_id='10000', sender_name='Tester',
                source_message_id=f'{index}', image_index=index,
                image_url='', file_path=str(path),
            )
            db.complete_chat_image_candidate_audit(row['id'], status='audited',
                decision='approve', provider='smoke', prompt_version='smoke',
                quality={}, flags=[], reason='test', proposed_tag_ids=[tag_id])
            candidates.append(db.get_chat_image_candidate(row['id']))
        bot = BotCapture()
        service = Confirm(db=db, context=ContextCapture(bot),
                          config={'chat_image_collection_enabled': True},
                          importer=importer, candidate_tags_provider=tags)
        assert (await service.run_once())['sent'] == 1
        images = [part for part in bot.sent[0] if part['type'] == 'image']
        assert len(images) == 2
        for path, part in zip(paths, images):
            assert base64.b64decode(part['data']['file'].removeprefix('base64://')) == path.read_bytes()
        event = ReplyEvent()
        assert await service.handle_reply(event)
        second = db.get_chat_image_candidate(candidates[1]['id'])
        assert second['status'] == 'approved_written'
        assert second['image_id'] != second['id']
        assert f"第2张 → 图片 ID：#{second['image_id']}" in bot.sent[-1][-1]['data']['text']
        event.message_str = '全部收'
        assert await service.handle_reply(event)
        receipt = bot.sent[-1][-1]['data']['text']
        assert f'第1张 → 图片 ID：#{seed.image_id}' in receipt
        assert '已在图库，本次未新增图片' in receipt
        assert not any(part['type'] == 'image' for part in bot.sent[-1])

        # Exercise the framework fallback with actual AstrBot message components.
        fallback = ContextCapture()
        service.context = fallback
        ok, _ = await service._send_group(SESSION, '', at_qq='10000', message_segments=[
            {'type': 'text', 'data': {'text': '第1张：宵崎奏'}}, images[0],
        ])
        assert ok and len(fallback.sent) == 1
        chain = fallback.sent[0].chain
        assert any(isinstance(part, Image) for part in chain)
        assert any(isinstance(part, Plain) and '宵崎奏' in part.text for part in chain)
        image = next(part for part in chain if isinstance(part, Image))
        assert image.file == images[0]['data']['file']
        print(json.dumps({'ok': True, 'preview_images': len(images),
            'direct_messages_captured': len(bot.sent), 'fallback_components': len(chain),
            'real_duplicate_id': seed.image_id, 'real_new_id': second['image_id'],
            'live_group_messages_sent': 0}))
        importer.close()


if __name__ == '__main__':
    asyncio.run(main())
