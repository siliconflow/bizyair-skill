#!/usr/bin/env python3
"""account.py — personal center + membership benefits, new in v1.9.

Covers:
  profile        account profile (nickname / email / level / language / third-party binds)
  plan           membership benefits and level (current level, wallet balance and expiry, gap to next tier)
  plan-tiers     comparison of every level tier
  notifications  notification list + unread counts
  credits        credits history (recharge / gift records and expiry times)

Endpoints:
  GET meta.bizyair.ai/v1/user/info               account profile
  GET meta.bizyair.ai/v1/user/plan_overview      wallet + current level + next tier
  GET meta.bizyair.ai/v1/plan/compare_user_level all-tier comparison
  GET meta.bizyair.ai/v1/notifications?current&page_size
  GET meta.bizyair.ai/v1/notifications/unread_count
  GET finance.bizyair.ai/v1/credits?current&page_size

Read-only. Editing the profile, buying membership and recharging are account
changes/transactions, so this version does not do them.
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from typing import Any

import api
import common

META_BASE = 'https://meta.bizyair.ai'
FINANCE_BASE = 'https://finance.bizyair.ai'
USER_INFO_URL = META_BASE + '/v1/user/info'
PLAN_OVERVIEW_URL = META_BASE + '/v1/user/plan_overview'
PLAN_TIERS_URL = META_BASE + '/v1/plan/compare_user_level'
NOTIFICATIONS_URL = META_BASE + '/v1/notifications'
UNREAD_URL = META_BASE + '/v1/notifications/unread_count'
CREDITS_URL = FINANCE_BASE + '/v1/credits'

# Notification type id -> display label. Unknown types show the raw id; no guessing.
NOTIFICATION_TYPES = {
    1: 'System',
    2: 'Follow',
    3: 'Comment',
    4: 'Feed',
    101: 'Like',
    102: 'Favorite',
}

TIER_RELATIONS = {'current': 'Current', 'upgrade_available': 'Upgrade available', 'downgrade': 'Downgrade'}


def _get(url: str, api_key: str, params: dict[str, Any] | None=None) -> dict[str, Any]:
    return api.safe_request_json('GET', url, api_key, params=params)


def cents_to_usd(value: Any) -> str | None:
    """USD cents -> '$50.00'. The plan API returns cent strings; a dash is shown when an amount is missing."""
    if value is None or value == '':
        return '—'
    try:
        amount = Decimal(str(value)) / Decimal(100)
    except Exception:
        return None
    if not amount.is_finite():
        return None
    return f'${amount:,.2f}'


def _fmt_days(value: Any) -> str:
    try:
        days = int(value)
    except Exception:
        return ''
    return f'{days} days' if days >= 0 else ''


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def get_profile(api_key: str) -> dict[str, Any]:
    return _get(USER_INFO_URL, api_key)


def get_plan_overview(api_key: str) -> dict[str, Any]:
    return _get(PLAN_OVERVIEW_URL, api_key)


def get_plan_tiers(api_key: str) -> dict[str, Any]:
    return _get(PLAN_TIERS_URL, api_key)


def get_notifications(api_key: str, *, page: int=1, page_size: int=20) -> dict[str, Any]:
    return _get(NOTIFICATIONS_URL, api_key, {'current': max(1, int(page)), 'page_size': max(1, min(100, int(page_size)))})


def get_unread(api_key: str) -> dict[str, Any]:
    return _get(UNREAD_URL, api_key)


def get_credits(api_key: str, *, page: int=1, page_size: int=20) -> dict[str, Any]:
    return _get(CREDITS_URL, api_key, {'current': max(1, int(page)), 'page_size': max(1, min(100, int(page_size)))})


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------

def summarize_profile(data: dict[str, Any]) -> dict[str, Any]:
    binds = data.get('third_party_binds') or {}
    return {
        'id': data.get('id'),
        'nick_name': data.get('nick_name'),
        'email': data.get('email'),
        'avatar': data.get('avatar'),
        'level': data.get('level'),
        'level_name': data.get('level_display_name'),
        'language': data.get('language'),
        'status': data.get('status'),
        'binds': {k: bool(v) for k, v in binds.items()} if isinstance(binds, dict) else {},
    }


def summarize_plan(overview: dict[str, Any], tiers: dict[str, Any] | None=None) -> dict[str, Any]:
    wallet = overview.get('wallet') or {}
    plan = overview.get('plan') or {}
    next_tier = overview.get('next_tier') or {}
    summary: dict[str, Any] = {
        'level': plan.get('effective_level'),
        'level_name': plan.get('effective_display_name'),
        'auto_level': plan.get('auto_level'),
        'auto_level_name': plan.get('auto_display_name'),
        'wallet_total_credits': wallet.get('total_balance'),
        'wallet_gift_credits': wallet.get('gift_balance'),
        'wallet_total_usd': common.format_usd(wallet.get('total_balance')),
        'gift_expire_at': wallet.get('next_gift_expire_at'),
        'credits_expire_days': wallet.get('credits_expire_days'),
        'gift_expire_days': wallet.get('gift_credits_expire_days'),
        'next_tier': None,
    }
    if next_tier:
        summary['next_tier'] = {
            'level': next_tier.get('level'),
            'name': next_tier.get('display_name'),
            'min_amount_usd': cents_to_usd(next_tier.get('min_amount_usd_cents')),
            'need_recharge_usd': cents_to_usd(next_tier.get('need_recharge_usd_cents')),
        }
    if tiers:
        summary['tiers'] = [
            {
                'level': tier.get('level'),
                'name': tier.get('display_name'),
                'min_amount_usd': cents_to_usd(tier.get('min_amount_usd_cents')),
                'need_recharge_usd': cents_to_usd(tier.get('need_recharge_usd_cents')),
                'relation': tier.get('relation'),
                'relation_label': TIER_RELATIONS.get(str(tier.get('relation')), tier.get('relation')),
            }
            for tier in (tiers.get('tiers') or [])
            if isinstance(tier, dict)
        ]
    return summary


def summarize_notifications(data: dict[str, Any]) -> list[dict[str, Any]]:
    items = data.get('notifications') or data.get('list') or []
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        type_id = item.get('type')
        results.append({
            'id': item.get('id'),
            'type': type_id,
            'type_label': NOTIFICATION_TYPES.get(type_id, str(type_id)),
            'title': item.get('title'),
            'content': item.get('content'),
            'sender': item.get('sender_user_nick_name'),
            'related_type': item.get('related_type'),
            'related_ids': item.get('related_ids'),
            'created_at': item.get('created_at'),
        })
    return results


def summarize_unread(data: dict[str, Any]) -> dict[str, Any]:
    types = data.get('types') or []
    counts = data.get('counts') or []
    pairs = []
    for index, type_id in enumerate(types):
        count = counts[index] if index < len(counts) else 0
        pairs.append({'type': type_id, 'label': NOTIFICATION_TYPES.get(type_id, str(type_id)), 'count': count})
    return {'total': sum(int(p['count'] or 0) for p in pairs), 'by_type': pairs}


def summarize_credits(data: dict[str, Any]) -> dict[str, Any]:
    items = []
    for item in data.get('list') or []:
        if not isinstance(item, dict):
            continue
        items.append({
            'total_amount': item.get('total_amount'),
            'gift_amount': item.get('gift_amount'),
            'recharge_amount': item.get('recharge_amount'),
            'created_at': item.get('created_at'),
            'expired_at': item.get('expired_at'),
        })
    return {
        'total': data.get('total'),
        'total_recharge_amount': data.get('total_recharge_amount'),
        'gift_amount': data.get('gift_amount'),
        'items': items,
    }


# ---------------------------------------------------------------------------
# markdown
# ---------------------------------------------------------------------------

def build_profile_markdown(payload: dict[str, Any]) -> str:
    if payload.get('error'):
        return f"Lookup failed: {payload.get('message') or 'unknown error'}"
    p = payload.get('profile') or {}
    lines = [f"**{p.get('nick_name') or '(no nickname)'}**", '']
    lines.append(f"Membership level: {p.get('level_name') or 'unknown'} (level {p.get('level')})")
    if p.get('email'):
        lines.append(f"Email: {p['email']}")
    if p.get('language'):
        lines.append(f"Language: {p['language']}")
    binds = p.get('binds') or {}
    if binds:
        bound = [name for name, ok in binds.items() if ok]
        lines.append('Third-party bindings: ' + (', '.join(bound) if bound else 'none'))
    if p.get('avatar'):
        lines.extend(['', f"![avatar]({p['avatar']})"])
    return '\n'.join(lines) + '\n'


def build_plan_markdown(payload: dict[str, Any]) -> str:
    if payload.get('error'):
        return f"Lookup failed: {payload.get('message') or 'unknown error'}"
    plan = payload.get('plan') or {}
    lines = ['**Membership benefits**', '']
    lines.append(f"Current level: {plan.get('level_name') or 'unknown'} (level {plan.get('level')})")
    if plan.get('auto_level_name') and plan.get('auto_level_name') != plan.get('level_name'):
        lines.append(f"Auto level: {plan['auto_level_name']} (level {plan.get('auto_level')})")
    wallet_bits = []
    if plan.get('wallet_total_credits') is not None:
        wallet_bits.append(f"{plan.get('wallet_total_usd')} ({int(plan['wallet_total_credits']):,} credits)")
    if plan.get('gift_expire_days'):
        wallet_bits.append(f"gift balance expires in {_fmt_days(plan['gift_expire_days'])}")
    if wallet_bits:
        lines.append('Balance: ' + ' · '.join(wallet_bits))
    next_tier = plan.get('next_tier')
    if next_tier:
        lines.append(f"Next tier: {next_tier.get('name')} (threshold {next_tier.get('min_amount_usd')}, {next_tier.get('need_recharge_usd')} more to recharge)")
    else:
        lines.append('Already at the highest tier.')
    tiers = plan.get('tiers') or []
    if tiers:
        lines.extend(['', '**Tier comparison**'])
        for tier in tiers:
            mark = f" ({tier.get('relation_label')})" if tier.get('relation') else ''
            lines.append(f"- {tier.get('name')}: threshold {tier.get('min_amount_usd')}{mark}")
    return '\n'.join(lines) + '\n'


def build_tiers_markdown(payload: dict[str, Any]) -> str:
    if payload.get('error'):
        return f"Lookup failed: {payload.get('message') or 'unknown error'}"
    plan = payload.get('plan') or {}
    lines = [f"**Membership tiers** (current: {plan.get('level_name') or 'unknown'})", '']
    for tier in plan.get('tiers') or []:
        mark = f"  <- {tier.get('relation_label')}" if tier.get('relation') else ''
        lines.append(f"- **{tier.get('name')}** (level {tier.get('level')}): threshold {tier.get('min_amount_usd')}{mark}")
    return '\n'.join(lines) + '\n'


def build_notifications_markdown(payload: dict[str, Any]) -> str:
    if payload.get('error'):
        return f"Lookup failed: {payload.get('message') or 'unknown error'}"
    unread = payload.get('unread') or {}
    results = payload.get('results') or []
    lines = [f"**Notifications** · {unread.get('total', 0)} unread", '']
    if not results:
        lines.append('No notifications yet.')
        return '\n'.join(lines)
    for index, item in enumerate(results, start=1):
        lines.append(f"{index}. [{item.get('type_label')}] {item.get('title') or '(no title)'}")
        if item.get('created_at'):
            lines.append(f"   {item['created_at']}")
    return '\n'.join(lines) + '\n'


def build_credits_markdown(payload: dict[str, Any]) -> str:
    if payload.get('error'):
        return f"Lookup failed: {payload.get('message') or 'unknown error'}"
    credits = payload.get('credits') or {}
    lines = ['**Credits history**', '']
    if credits.get('total') is not None:
        lines.append(f"Total records: {credits['total']}.")
    items = credits.get('items') or []
    if not items:
        lines.append('No credit history records.')
        return '\n'.join(lines)
    for index, item in enumerate(items, start=1):
        bits = []
        if item.get('total_amount') is not None:
            bits.append(f"Total {int(item['total_amount']):,} credits")
        if item.get('gift_amount') is not None:
            bits.append(f"Gift {int(item['gift_amount']):,}")
        lines.append(f"{index}. {' · '.join(bits)}")
        if item.get('created_at'):
            lines.append(f"   Recharged at {item['created_at']}" + (f", expires {item['expired_at']}" if item.get('expired_at') else ''))
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _take_value(rest: list[str], index: int, flag: str) -> str:
    if index + 1 >= len(rest):
        print(f'Parameter {flag} is missing a value', file=sys.stderr)
        raise SystemExit(2)
    return rest[index + 1]


def _parse_args(rest: list[str]) -> dict[str, Any]:
    options: dict[str, Any] = {'page': 1, 'page_size': 20, 'limit': 20, 'reply_format': 'markdown', 'api_key': None, 'with_tiers': False}
    index = 0
    while index < len(rest):
        flag = rest[index]
        if flag in {'--page', '--page-size', '--limit', '--reply-format', '--api-key'}:
            value = _take_value(rest, index, flag)
            if flag == '--page':
                options['page'] = max(1, int(value)) if value.isdigit() else 1
            elif flag == '--page-size':
                options['page_size'] = min(100, max(1, int(value))) if value.isdigit() else 20
            elif flag == '--limit':
                options['limit'] = min(100, max(1, int(value))) if value.isdigit() else 20
            elif flag == '--reply-format':
                options['reply_format'] = value
            elif flag == '--api-key':
                options['api_key'] = value
            index += 2
            continue
        if flag == '--with-tiers':
            options['with_tiers'] = True
        index += 1
    if options['reply_format'] not in {'json', 'markdown'}:
        print('--reply-format only supports json or markdown', file=sys.stderr)
        raise SystemExit(2)
    return options


def _emit(payload: dict[str, Any], options: dict[str, Any], markdown_key: str='reply_markdown') -> None:
    if options['reply_format'] == 'json':
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload.get(markdown_key, ''))


def _fail(payload: dict[str, Any], options: dict[str, Any], markdown_builder) -> None:
    payload['reply_markdown'] = markdown_builder(payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
    raise SystemExit(1)


def cmd_profile(rest: list[str]) -> None:
    options = _parse_args(rest)
    key = api.require_api_key(options['api_key'])
    result = get_profile(key)
    payload: dict[str, Any] = {}
    if not api.result_is_ok(result):
        payload.update({'error': 'REMOTE_REQUEST_FAILED', 'message': api.request_error_message(result), 'http_status': result.get('status')})
        _fail(payload, options, build_profile_markdown)
    payload['profile'] = summarize_profile(api.extract_result_data(result) or {})
    payload['reply_markdown'] = build_profile_markdown(payload)
    _emit(payload, options)


def cmd_plan(rest: list[str]) -> None:
    options = _parse_args(rest)
    key = api.require_api_key(options['api_key'])
    overview = get_plan_overview(key)
    tiers = get_plan_tiers(key)
    payload: dict[str, Any] = {}
    if not api.result_is_ok(overview):
        payload.update({'error': 'REMOTE_REQUEST_FAILED', 'message': api.request_error_message(overview), 'http_status': overview.get('status')})
        _fail(payload, options, build_plan_markdown)
    tiers_data = api.extract_result_data(tiers) if api.result_is_ok(tiers) else None
    payload['plan'] = summarize_plan(api.extract_result_data(overview) or {}, tiers_data if options['with_tiers'] else None)
    payload['reply_markdown'] = build_plan_markdown(payload)
    _emit(payload, options)


def cmd_plan_tiers(rest: list[str]) -> None:
    options = _parse_args(rest)
    key = api.require_api_key(options['api_key'])
    tiers = get_plan_tiers(key)
    payload: dict[str, Any] = {}
    if not api.result_is_ok(tiers):
        payload.update({'error': 'REMOTE_REQUEST_FAILED', 'message': api.request_error_message(tiers), 'http_status': tiers.get('status')})
        _fail(payload, options, build_tiers_markdown)
    tiers_data = api.extract_result_data(tiers) or {}
    payload['plan'] = summarize_plan(
        {'plan': {'effective_level': tiers_data.get('effective_level'),
                  'effective_display_name': tiers_data.get('effective_display_name')}},
        tiers_data,
    )
    payload['reply_markdown'] = build_tiers_markdown(payload)
    _emit(payload, options)


def cmd_notifications(rest: list[str]) -> None:
    options = _parse_args(rest)
    key = api.require_api_key(options['api_key'])
    result = get_notifications(key, page=options['page'], page_size=options['page_size'])
    unread = get_unread(key)
    payload: dict[str, Any] = {}
    if not api.result_is_ok(result):
        payload.update({'error': 'REMOTE_REQUEST_FAILED', 'message': api.request_error_message(result), 'http_status': result.get('status')})
        _fail(payload, options, build_notifications_markdown)
    payload['results'] = summarize_notifications(api.extract_result_data(result) or {})[: options['limit']]
    payload['count'] = len(payload['results'])
    payload['unread'] = summarize_unread(api.extract_result_data(unread) or {}) if api.result_is_ok(unread) else None
    payload['reply_markdown'] = build_notifications_markdown(payload)
    _emit(payload, options)


def cmd_credits(rest: list[str]) -> None:
    options = _parse_args(rest)
    key = api.require_api_key(options['api_key'])
    result = get_credits(key, page=options['page'], page_size=options['page_size'])
    payload: dict[str, Any] = {}
    if not api.result_is_ok(result):
        payload.update({'error': 'REMOTE_REQUEST_FAILED', 'message': api.request_error_message(result), 'http_status': result.get('status')})
        _fail(payload, options, build_credits_markdown)
    credits = summarize_credits(api.extract_result_data(result) or {})
    credits['items'] = credits['items'][: options['limit']]
    payload['credits'] = credits
    payload['reply_markdown'] = build_credits_markdown(payload)
    _emit(payload, options)
