"""Opt-in text profiles; never load reference images or assign new tag IDs."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .matcher import normalize_tag_name

PROFILE_PATH = Path(__file__).parent / 'data' / 'pjsk_identity_profiles.json'


@lru_cache(maxsize=1)
def load_profiles() -> dict[str, Any]:
    body = PROFILE_PATH.read_bytes()
    data = json.loads(body)
    records = data.get('characters', [])
    if len(records) != 26 or len({r['key'] for r in records}) != 26:
        raise ValueError('角色档案必须包含 26 个唯一角色')
    keys = {r['key'] for r in records}
    if any(set(r['confusable_with']) - keys for r in records):
        raise ValueError('角色档案包含未知的易混淆对象')
    data['content_sha256'] = hashlib.sha256(body).hexdigest()
    return data


def resolve_profile(candidate: dict[str, Any]) -> dict[str, Any] | None:
    names = {normalize_tag_name(str(candidate.get(k) or ''))
             for k in ('standard_name', 'name', 'name_ja', 'name_en')}
    names.discard('')
    for row in load_profiles()['characters']:
        if names & {normalize_tag_name(row[k]) for k in ('name', 'name_ja', 'name_en')}:
            return row
    return None


def build_profile_context(candidates: list[dict[str, Any]], *, mode: str = 'off') -> tuple[str, str]:
    if mode != 'text':
        return '', 'none'
    data = load_profiles()
    resolved = [(c, resolve_profile(c)) for c in candidates if c.get('tag_type') == 'character']
    ids_by_key = {r['key']: int(c['tag_id']) for c, r in resolved if r}
    records = []
    for candidate, row in resolved:
        if row:
            records.append({'tag_id': int(candidate['tag_id']), 'features': row['features'],
                            'cautions': row['exceptions'],
                            'confusable_tag_ids': [ids_by_key[k] for k in row['confusable_with'] if k in ids_by_key]})
    if not records:
        return '', 'none'
    context = '\n常见外观参考（不是必须满足的条件）：' + json.dumps(
        {'cautions': data['common_cautions'], 'characters': records}, ensure_ascii=False, separators=(',', ':')) + '\n'
    return context, str(data['version']) + ':' + data['content_sha256'][:12]
