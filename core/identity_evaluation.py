"""Offline identity metrics. Predictions and inherited tags never become gold labels."""
from __future__ import annotations

import hashlib
from typing import Any


def assign_sample_groups(samples: list[dict[str, Any]], *, seed: str = 'pjsk-b1') -> None:
    parent = list(range(len(samples)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        parent[find(b)] = find(a)

    for i, first in enumerate(samples):
        for j in range(i):
            second = samples[j]
            same_sha = bool(first.get('original_sha256') and first.get('original_sha256') == second.get('original_sha256'))
            same_origin = bool(set(first.get('_origins', [])) & set(second.get('_origins', [])))
            a, b = first.get('_phash', ''), second.get('_phash', '')
            near = False
            if isinstance(a, str) and isinstance(b, str) and len(a) == len(b) == 16:
                try:
                    near = (int(a, 16) ^ int(b, 16)).bit_count() <= 4
                except ValueError:
                    pass
            if same_sha or same_origin or near:
                union(j, i)
    groups: dict[int, list[dict[str, Any]]] = {}
    for i, sample in enumerate(samples):
        groups.setdefault(find(i), []).append(sample)
    for members in groups.values():
        key = min(str(s['sample_id']) for s in members)
        group_id = hashlib.sha256((seed + ':' + key).encode()).hexdigest()[:16]
        split = 'holdout' if int(group_id, 16) % 5 == 0 else 'development'
        for sample in members:
            sample['group_id'] = group_id
            sample['split'] = split
            sample.pop('_origins', None)
            sample.pop('_phash', None)


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 6) if b else None


def evaluate_identity(manifest: dict[str, Any], predictions: list[dict[str, Any]], *,
                      split: str = 'holdout') -> dict[str, Any]:
    if manifest.get('split_reviewed') is not True:
        raise ValueError('请先人工复核近重复/同源图片分组与开发/验收集划分')
    if split not in {'holdout', 'development'}:
        raise ValueError('未知评估分组')
    roster = {r['key'] for r in manifest['roster']}
    samples = manifest['samples']
    if len({s['sample_id'] for s in samples}) != len(samples):
        raise ValueError('样本 ID 重复')
    group_splits: dict[str, set[str]] = {}
    sha_splits: dict[str, set[str]] = {}
    for sample in samples:
        group_splits.setdefault(sample['group_id'], set()).add(sample['split'])
        if sample.get('original_sha256'):
            sha_splits.setdefault(sample['original_sha256'], set()).add(sample['split'])
    if any(len(values) > 1 for values in [*group_splits.values(), *sha_splits.values()]):
        raise ValueError('同源或同图样本跨越开发集和验收集')
    verified = []
    for sample in samples:
        label = sample.get('label') or {}
        if sample['split'] != split or label.get('status') != 'human_verified':
            continue
        truth = label.get('character_keys')
        if (label.get('complete') is not True or not str(label.get('reviewer') or '').strip()
                or not isinstance(truth, list) or any(not isinstance(k, str) for k in truth)
                or len(set(truth)) != len(truth) or set(truth) - roster
                or not isinstance(label.get('is_target'), bool)
                or bool(truth) != label['is_target']):
            raise ValueError(f"人工标签尚未完整核实：{sample['sample_id']}")
        verified.append(sample)
    if not verified:
        raise ValueError('没有人工核实的完整样本，不能计算识图准确率')
    by_id = {}
    for prediction in predictions:
        sid = prediction.get('sample_id')
        if sid in by_id:
            raise ValueError('预测样本 ID 重复')
        by_id[sid] = prediction
    missing = [s['sample_id'] for s in verified if s['sample_id'] not in by_id]
    if missing:
        raise ValueError(f'预测不完整，缺少 {len(missing)} 个已核实样本')
    counts = {k: {'tp': 0, 'fp': 0, 'fn': 0, 'truth_count': 0} for k in sorted(roster)}
    exact = negatives = false_positive_images = errors = 0
    for sample in verified:
        prediction = by_id[sample['sample_id']]
        status = prediction.get('status')
        if status not in {'ok', 'error'}:
            raise ValueError('预测必须明确记录 ok 或 error 状态')
        values = prediction.get('character_keys', [])
        if (not isinstance(values, list) or any(not isinstance(k, str) for k in values)
                or len(set(values)) != len(values) or set(values) - roster):
            raise ValueError('预测包含非法或未知角色')
        truth = set(sample['label']['character_keys'])
        predicted = set(values) if status == 'ok' else set()
        errors += int(status == 'error')
        exact += int(status == 'ok' and predicted == truth)
        negatives += int(not truth)
        false_positive_images += int(not truth and bool(predicted))
        for key in truth | predicted:
            counts[key]['tp'] += int(key in truth and key in predicted)
            counts[key]['fp'] += int(key not in truth and key in predicted)
            counts[key]['fn'] += int(key in truth and key not in predicted)
            counts[key]['truth_count'] += int(key in truth)
    tp = sum(c['tp'] for c in counts.values())
    fp = sum(c['fp'] for c in counts.values())
    fn = sum(c['fn'] for c in counts.values())
    per_role = {k: {**c, 'precision': _ratio(c['tp'], c['tp'] + c['fp']),
                   'recall': _ratio(c['tp'], c['tp'] + c['fn'])} for k, c in counts.items()}
    return {'split': split, 'verified_samples': len(verified),
            'unverified_excluded': sum(s['split'] == split for s in samples) - len(verified),
            'precision': _ratio(tp, tp + fp), 'recall': _ratio(tp, tp + fn),
            'exact_image_accuracy': _ratio(exact, len(verified)),
            'processing_errors': errors, 'negative_samples': negatives,
            'negative_false_positive_rate': _ratio(false_positive_images, negatives),
            'per_role': per_role, 'missing_roles': [k for k, c in counts.items() if not c['truth_count']],
            'note': '仅针对本份人工核实样本，不代表全量真实场景准确率；质量通过率另行统计。'}
