"""Bounded, non-authoritative observations for the group image audit pipeline."""
from __future__ import annotations

import asyncio
import json
import math
import re
from typing import Any

from .llm_image_review_service import LlmImageReviewContractError

IMPLEMENTATION_VERSION = 'a1'


class AuditContractError(LlmImageReviewContractError):
    def __init__(self, message: str, category: str = 'invalid_structure') -> None:
        super().__init__(message)
        self.category = category


def finite_number(value: Any, *, field: str, maximum: float, category: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AuditContractError(f'{field} 必须是数字', category)
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise AuditContractError(f'{field} 数值非法', category) from exc
    if not math.isfinite(result) or not 0 <= result <= maximum:
        raise AuditContractError(f'{field} 必须是 0～{maximum:g} 范围的有限数', category)
    return result


def load_audit_json(text: str) -> tuple[dict[str, Any], bool]:
    raw = str(text or '').strip()
    if len(raw) > 32768:
        raise AuditContractError('模型结果超过长度限制', 'invalid_json')
    fence = re.fullmatch(r'```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```', raw, re.IGNORECASE)
    if fence:
        raw = fence.group(1).strip()

    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AuditContractError('JSON 存在重复字段', 'duplicate_json_key')
            result[key] = value
        return result

    def bad_constant(_value):
        raise AuditContractError('JSON 包含非有限数', 'non_finite_number')

    try:
        data = json.loads(raw, object_pairs_hook=object_pairs, parse_constant=bad_constant)
    except AuditContractError:
        raise
    except (ValueError, RecursionError) as exc:
        raise AuditContractError('不是单个 JSON 对象', 'invalid_json') from exc
    if not isinstance(data, dict):
        raise AuditContractError('顶层必须是 JSON 对象')
    return data, bool(fence)


def classify_error(error: Exception) -> tuple[str, bool]:
    if isinstance(error, AuditContractError):
        return error.category, True
    if isinstance(error, LlmImageReviewContractError):
        return 'invalid_structure', True
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return 'timeout', True
    if 'invalidsubscription' in str(error).lower():
        return 'subscription_inactive', False
    status = getattr(error, 'status_code', None)
    if status is None:
        status = getattr(getattr(error, 'response', None), 'status_code', None)
    if status is None:
        match = re.search(r'\b(400|401|403|408|413|429|5\d\d)\b', str(error))
        status = int(match.group(1)) if match else None
    if str(status) == '429':
        return 'rate_limit', True
    if str(status) in {'401', '403'}:
        return 'authentication', False
    if str(status) in {'400', '413'}:
        return 'invalid_request', False
    if str(status) == '408':
        return 'timeout', True
    if status is not None and str(status).startswith('5'):
        return 'upstream', True
    name = type(error).__name__.lower()
    if 'timeout' in name:
        return 'timeout', True
    if 'connection' in name:
        return 'network', True
    return 'provider_error', True


def shadow_identity(parsed: dict[str, Any], *, threshold: float, mode: str) -> dict[str, Any]:
    records = parsed['identities']
    certain = [r['tag_id'] for r in records if r['confidence'] >= threshold]
    uncertain = [r['tag_id'] for r in records if r['confidence'] < threshold]
    result = {
        'mode': mode, 'applied': False, 'threshold': threshold,
        'certain_tag_ids': certain, 'uncertain_tag_ids': uncertain,
        'suggested_action': 'unknown',
    }
    if mode == 'shadow':
        if parsed['decision'] == 'reject':
            result['suggested_action'] = 'pending_review'
        elif parsed['decision'] == 'approve' and records and not uncertain and not parsed['quality']['flags']:
            result['suggested_action'] = 'ask_confirmation'
        else:
            result['suggested_action'] = 'pending_review'
    return result
