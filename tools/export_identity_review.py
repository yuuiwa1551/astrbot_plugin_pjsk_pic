"""Export a private, read-only review pack. All labels start unverified."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import types
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps

pkg = types.ModuleType('pjsk_review_export')
pkg.__path__ = [str(Path(__file__).resolve().parents[1] / 'core')]
sys.modules[pkg.__name__] = pkg
from pjsk_review_export.chat_image_identity_profiles import load_profiles
from pjsk_review_export.identity_evaluation import assign_sample_groups
from pjsk_review_export.matcher import normalize_tag_name


def export_pack(db_path: Path, output: Path, *, limit: int = 200) -> dict:
    db_path = db_path.resolve()
    output = output.resolve()
    if not 1 <= limit <= 500:
        raise ValueError('样本数量须为 1～500')
    if output.exists() and any(output.iterdir()):
        raise ValueError('输出目录必须为空，避免覆盖已有人工标注')
    profiles = load_profiles()
    roster = [{'key': r['key'], 'name': r['name'], 'name_ja': r['name_ja'],
               'name_en': r['name_en']} for r in profiles['characters']]
    by_name = {normalize_tag_name(r[n]): r['key'] for r in roster for n in ('name', 'name_ja', 'name_en')}
    db = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('BEGIN')
    tag_keys = {}
    for row in db.execute('select id,name from tags where is_character=1'):
        if normalize_tag_name(row['name']) in by_name:
            tag_keys[row['id']] = by_name[normalize_tag_name(row['name'])]
    image_tags = {}
    for row in db.execute("select image_id,tag_id from image_tags where review_status not in ('rejected','manual_rejected')"):
        if row['tag_id'] in tag_keys:
            image_tags.setdefault(row['image_id'], set()).add(tag_keys[row['tag_id']])
    image_origins = {}
    for row in db.execute("select image_id,post_url from sources where post_url!=''"):
        # Keep related pages in one family without exporting private URLs/tokens.
        digest = hashlib.sha256(str(row['post_url']).split('?')[0].encode()).hexdigest()
        image_origins.setdefault(row['image_id'], set()).add('post:' + digest)
    pool = {}
    for row in db.execute('select id,file_path,sha256,phash from images where is_active=1 order by id desc'):
        if row['id'] in image_tags:
            pool['image:' + str(row['id'])] = {
                'image_id': row['id'], 'candidate_id': None, '_path': row['file_path'],
                '_phash': row['phash'] or '', '_origins': list(image_origins.get(row['id'], set())),
                'suggested_character_keys': sorted(image_tags[row['id']]),
                'provenance': 'gallery_tags_unverified', 'category': 'gallery',
            }
    for row in db.execute("select * from chat_image_candidates where status not in ('duplicate','download_failed') order by id desc"):
        known_key = 'image:' + str(row['image_id'])
        key = known_key if row['image_id'] and known_key in pool else 'candidate:' + str(row['id'])
        human = bool(row['confirm_user_id'] and row['confirm_user_id'] not in ('auto', 'backfill'))
        try:
            proposed = set(json.loads(row['proposed_tag_ids_json'] or '[]'))
            confirmed = set(json.loads(row['confirmed_tag_ids_json'] or '[]'))
        except (TypeError, ValueError):
            continue
        if key in pool and pool[key].get('provenance', '').startswith('human_'):
            continue
        selected = confirmed if human and row['status'] == 'approved_written' else proposed
        names = sorted({tag_keys[t] for t in selected if t in tag_keys})
        if key in pool and not human:
            continue
        original = pool.get(key, {})
        origin = hashlib.sha256((str(row['session_id']) + ':' + str(row['source_message_id'])).encode()).hexdigest()
        pool[key] = {
            'image_id': row['image_id'] or None, 'candidate_id': row['id'],
            '_path': original.get('_path') or row['file_path'], '_phash': original.get('_phash', ''),
            '_origins': original.get('_origins', []) + (['message:' + origin] if row['source_message_id'] else []),
            'suggested_character_keys': names or original.get('suggested_character_keys', []),
            'provenance': ('human_correction_unverified' if confirmed != proposed else 'human_confirmation_unverified')
                          if human else 'model_candidate_unverified',
            'category': 'feedback' if human else ('negative_candidate' if row['audit_decision'] == 'reject' else 'uncertain'),
        }
    db.close()
    output.mkdir(parents=True, exist_ok=True)
    (output / 'images').mkdir(exist_ok=True)
    samples = []
    attempted = set()
    seen_sha = set()
    skipped = Counter()

    def take(key):
        if key in attempted or len(samples) >= limit:
            return False
        attempted.add(key)
        record = dict(pool[key])
        path = Path(record.pop('_path'))
        try:
            if not path.is_file():
                skipped['missing_file'] += 1
                return False
            if path.stat().st_size > 30 * 1024 * 1024:
                skipped['oversized'] += 1
                return False
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest in seen_sha:
                skipped['duplicate_sha'] += 1
                return False
            with Image.open(path) as opened:
                image = ImageOps.exif_transpose(opened)
                image.thumbnail((1536, 1536), Image.Resampling.LANCZOS)
                if image.mode in ('RGBA', 'LA'):
                    rgba = image.convert('RGBA')
                    background = Image.new('RGBA', rgba.size, 'white')
                    background.alpha_composite(rgba)
                    image = background.convert('RGB')
                else:
                    image = image.convert('RGB')
                sid = f'sample-{len(samples) + 1:04d}'
                image_path = output / 'images' / (sid + '.jpg')
                image.save(image_path, format='JPEG', quality=90, optimize=True)
            record.update(sample_id=sid, image='images/' + image_path.name, original_sha256=digest,
                          input_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
                          label={'status': 'pending', 'character_keys': [], 'is_target': None,
                                 'complete': False, 'reviewer': '', 'notes': ''})
            samples.append(record)
            seen_sha.add(digest)
            return True
        except (OSError, ValueError, Image.DecompressionBombError):
            skipped['unreadable'] += 1
            return False

    # Balance character coverage before filling with the most common characters.
    for _ in range(max(1, int(limit * .7) // len(roster))):
        for role in roster:
            for key, record in pool.items():
                if role['key'] in record['suggested_character_keys'] and take(key):
                    break
    for category, quota in [('feedback', max(1, limit // 5)), ('negative_candidate', max(1, limit // 10))]:
        taken = 0
        for key, record in pool.items():
            if record['category'] == category and take(key):
                taken += 1
                if taken >= quota:
                    break
    for key in pool:
        take(key)
        if len(samples) >= limit:
            break
    if not samples:
        raise ValueError('没有可导出的图片，请核对数据库与图片目录是否在同一运行环境')
    assign_sample_groups(samples)
    coverage = Counter(k for sample in samples for k in sample['suggested_character_keys'])
    manifest = {
        'schema_version': 1, 'dataset_id': 'pjsk-identity-' + hashlib.sha256(
            '|'.join(s['original_sha256'] for s in samples).encode()).hexdigest()[:12],
        'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'profile_version': profiles['version'], 'split_reviewed': False,
        'roster': roster, 'samples': samples,
        'summary': {'requested': limit, 'exported': len(samples), 'verified': 0,
                    'suggested_coverage': dict(coverage), 'missing_roles': [r['key'] for r in roster if not coverage[r['key']]],
                    'skipped': dict(skipped), 'categories': dict(Counter(s['category'] for s in samples)),
                    'splits': dict(Counter(s['split'] for s in samples))},
        'notes': ['所有预填标签均未核实，不能作为准确率标准答案。',
                  '开发/验收集按同源、SHA 和已有 pHash 粗分组，正式评测前需要人工检查。',
                  '图片为最长边1536的本地预览，未上传，不包含群号、发图人账号或来源访问令牌。'],
    }
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    template = (Path(__file__).parent / 'identity_review.html').read_text(encoding='utf-8')
    payload = json.dumps(manifest, ensure_ascii=False).replace('<', '\\u003c')
    (output / 'review.html').write_text(template.replace('__MANIFEST_JSON__', payload), encoding='utf-8')
    return manifest['summary']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--limit', type=int, default=200)
    args = parser.parse_args()
    print(json.dumps(export_pack(args.db, args.output, limit=args.limit), ensure_ascii=False, indent=2))
