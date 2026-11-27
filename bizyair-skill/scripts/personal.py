#!/usr/bin/env python3
"""personal.py — personal center (my-profile), new in v1.12.0.

Consolidates the previously scattered my-works / my-favorites / my-models / profile
into one clear hierarchy:

  my-profile published   what I published: works / AI apps / workflows / models
  my-profile forked      what I forked: works / AI apps / workflows / models
  my-profile generated   what I generated: outputs (published / unpublished)
  my-profile show        profile lookup
  my-profile edit        profile editing (emits a confirmation card by default, --confirm submits)
  my-profile share       share link for the personal page

Endpoints (all on meta.bizyair.ai):
  GET  /v1/creations/my                     works I published
  GET  /v1/creations/my_fork                works I forked
  GET  /v1/userspace/{uid}/bizy_models      models / apps / workflows I published
  GET  /v1/bizy_models/my_fork              models / apps / workflows I forked
  GET  /v1/user/info                        profile
  PUT  /v1/userspace/update_user            profile editing

Write operations (edit) exit with code 3 and emit a confirmation card by default;
this changes account data, so it is never submitted automatically.
"""
from __future__ import annotations

import concurrent.futures
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import account
import api
import gallery
import hub

META_BASE = 'https://meta.bizyair.ai'
CREATIONS_MY_URL = META_BASE + '/v1/creations/my'
CREATIONS_FORK_URL = META_BASE + '/v1/creations/my_fork'
FORKED_ASSETS_URL = META_BASE + '/v1/bizy_models/my_fork'
UPDATE_USER_URL = META_BASE + '/v1/userspace/update_user'
PROFILE_SHARE_URL = 'https://www.bizyair.ai/userspace/{id}'

KIND_ALIASES = {
    'all': 'all',
    'creation': 'creation', 'creations': 'creation', 'work': 'creation', 'works': 'creation',
    'app': 'app', 'apps': 'app', 'application': 'app', 'applications': 'app',
    'workflow': 'workflow', 'workflows': 'workflow',
    'model': 'model', 'models': 'model',
}
GROUP_ORDER = ['creation', 'app', 'workflow', 'model']
GROUP_LABELS = {'creation': 'Works', 'app': 'AI apps', 'workflow': 'Workflow', 'model': 'Models'}

# Platform allowlist matching the site's profile-edit dialog; other accepts any https link.
HOME_PLATFORMS = {
    'discord': ('Discord', {'discord.com', 'discord.gg'}),
    'github': ('GitHub', {'github.com'}),
    'x': ('X', {'x.com', 'twitter.com'}),
    'youtube': ('YouTube', {'youtube.com', 'youtu.be'}),
    'tiktok': ('TikTok', {'tiktok.com'}),
    'other': ('Other', None),
}
AVATAR_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}
AVATAR_MAX_BYTES = 10 * 1024 * 1024
NICKNAME_MAX = 40
DESCRIPTION_MAX = 2000


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

def _take_value(rest: list[str], index: int, flag: str) -> str:
    if index + 1 >= len(rest) or rest[index + 1].startswith('--'):
        print(f'{flag} requires a value', file=sys.stderr)
        raise SystemExit(2)
    return rest[index + 1]


def _parse_kinds(value: str) -> set[str]:
    kinds = set()
    for raw in str(value or '').split(','):
        key = raw.strip().lower()
        if not key:
            continue
        if key not in KIND_ALIASES:
            print(f'--kind does not support {raw!r}; choose creation / app / workflow / model / all', file=sys.stderr)
            raise SystemExit(2)
        kinds.add(KIND_ALIASES[key])
    return kinds or {'all'}


def _parse_list_args(rest: list[str]) -> dict[str, Any]:
    options: dict[str, Any] = {
        'kinds': {'all'}, 'limit': 10, 'page': 1, 'keyword': None, 'sort': None,
        'reply_format': 'markdown', 'api_key': None,
    }
    index = 0
    while index < len(rest):
        flag = rest[index]
        if flag in {'--kind', '--type', '--limit', '--page', '--keyword', '--sort', '--reply-format', '--api-key'}:
            value = _take_value(rest, index, flag)
            if flag in {'--kind', '--type'}:
                options['kinds'] = _parse_kinds(value)
            elif flag == '--limit':
                options['limit'] = min(100, max(1, int(value))) if value.isdigit() else 10
            elif flag == '--page':
                options['page'] = max(1, int(value)) if value.isdigit() else 1
            elif flag == '--keyword':
                options['keyword'] = value
            elif flag == '--sort':
                options['sort'] = value
            elif flag == '--reply-format':
                options['reply_format'] = value
            elif flag == '--api-key':
                options['api_key'] = value
            index += 2
            continue
        print(f'Unknown parameter: {flag}', file=sys.stderr)
        raise SystemExit(2)
    if options['reply_format'] not in {'json', 'markdown'}:
        print('--reply-format only supports json or markdown', file=sys.stderr)
        raise SystemExit(2)
    return options


# ---------------------------------------------------------------------------
# Published / forked aggregation
# ---------------------------------------------------------------------------

def _asset_kind(item: dict[str, Any]) -> str:
    # Classify strictly by the platform type. v1.12.2 used "workflow" in the name to group
    # Application under Workflow, so the title said Workflow while the object was an Application /
    # app link; type filtering is only consistent when it uses the actual type.
    kind = str(item.get('type') or '')
    if kind == 'Workflow':
        return 'workflow'
    if kind == 'Application':
        return 'app'
    return 'model'


def _asset_summary(item: dict[str, Any]) -> dict[str, Any]:
    summary = hub.summarize_model(item, page_variant='my')
    summary['kind'] = _asset_kind(item)
    summary['asset_type'] = summary.get('type')
    return summary


def _creation_summary(item: dict[str, Any], owner_fallback: str | None=None) -> dict[str, Any]:
    summary = gallery.summarize_work(item, owner_fallback=owner_fallback)
    summary['kind'] = 'creation'
    return summary


def _safe_page(result: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None, str | None]:
    if not api.result_is_ok(result):
        return [], None, api.request_error_message(result)
    data = api.extract_result_data(result) or {}
    items = [item for item in (data.get('list') or []) if isinstance(item, dict)]
    total = data.get('total')
    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = None
    return items, total, None


def _fetch_forked_assets(api_key: str, *, page: int, page_size: int, keyword: str | None, sort: str | None) -> dict[str, Any]:
    params = {'current': page, 'page_size': page_size, 'sort': hub.resolve_sort(sort)}
    if keyword:
        params['keyword'] = keyword
    return api.safe_request_json('GET', FORKED_ASSETS_URL, api_key, params=params)


def _load_bundle(api_key: str, user_id: str, *, forked: bool, kinds: set[str], page: int, page_size: int,
                 keyword: str | None, sort: str | None, owner_name: str | None=None) -> dict[str, Any]:
    want_creations = 'all' in kinds or 'creation' in kinds
    want_assets = 'all' in kinds or bool(kinds & {'app', 'workflow', 'model'})
    creation_result = asset_result = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        creation_future = asset_future = None
        if want_creations:
            creation_future = pool.submit(
                gallery.list_works, api_key, keyword=keyword, sort=sort, page=page, page_size=page_size,
                mine=not forked, favorites=forked)
        if want_assets:
            if forked:
                asset_future = pool.submit(
                    _fetch_forked_assets, api_key, page=page, page_size=page_size, keyword=keyword, sort=sort)
            else:
                asset_future = pool.submit(
                    hub.my_models, api_key, user_id, keyword=keyword, sort=sort, page=page, page_size=page_size)
        if creation_future is not None:
            creation_result = creation_future.result()
        if asset_future is not None:
            asset_result = asset_future.result()

    creation_items, creation_total, creation_error = _safe_page(creation_result) if creation_result is not None else ([], None, None)
    asset_items, asset_total, asset_error = _safe_page(asset_result) if asset_result is not None else ([], None, None)

    groups: dict[str, list[dict[str, Any]]] = {key: [] for key in GROUP_ORDER}
    if want_creations:
        groups['creation'] = [_creation_summary(item, owner_fallback=owner_name) for item in creation_items]
    if want_assets:
        for item in asset_items:
            groups[_asset_kind(item)].append(_asset_summary(item))
    if 'all' not in kinds:
        for key in GROUP_ORDER:
            if key not in kinds:
                groups[key] = []
    return {
        'groups': groups,
        'sources': {
            'creations': {'requested': want_creations, 'total': creation_total, 'count': len(creation_items), 'error': creation_error},
            'assets': {'requested': want_assets, 'total': asset_total, 'count': len(asset_items), 'error': asset_error},
        },
    }


def _build_list_payload(scope: str, user_id: str, bundle: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    groups = bundle['groups']
    shown = {key: values[: options['limit']] for key, values in groups.items()}
    flat: list[dict[str, Any]] = []
    for key in GROUP_ORDER:
        flat.extend(shown[key])
    sources = bundle['sources']
    counts = {
        'creations': sources['creations']['total'],
        'assets': sources['assets']['total'],
        'groups_on_page': {key: len(values) for key, values in shown.items() if values},
        'shown': len(flat),
    }
    payload: dict[str, Any] = {
        'scope': scope,
        'user_id': user_id,
        'query': {
            'kinds': sorted(options['kinds']),
            'keyword': options['keyword'],
            'sort': grid_sort(options['sort']),
            'page': options['page'],
            'limit_per_group': options['limit'],
        },
        'counts': counts,
        'sources': sources,
        'groups': shown,
        'results': flat,
        'count': len(flat),
    }
    errors = [name for name, source in sources.items() if source.get('error')]
    if errors:
        payload['error'] = 'PARTIAL_REMOTE_FAILURE'
        payload['message'] = '; '.join(f"{name}: {sources[name]['error']}" for name in errors)
    payload['reply_markdown'] = build_list_markdown(payload)
    return payload


def grid_sort(value: str | None) -> str:
    return gallery.resolve_sort(value)


def build_list_markdown(payload: dict[str, Any]) -> str:
    title = 'My published content' if payload.get('scope') == 'my-published' else 'My forked content'
    sources = payload.get('sources') or {}
    counts = payload.get('counts') or {}
    bits = []
    if sources.get('creations', {}).get('requested'):
        bits.append(f"Works {sources['creations'].get('total', '?')}")
    if sources.get('assets', {}).get('requested'):
        bits.append(f"Models/AI apps/workflows {sources['assets'].get('total', '?')}")
    lines = [f"**{title}**" + (' · ' + ' · '.join(bits) if bits else ''), '']
    groups = payload.get('groups') or {}
    emitted = False
    for key in GROUP_ORDER:
        items = groups.get(key) or []
        if not items:
            continue
        emitted = True
        lines.append(f"### {GROUP_LABELS[key]} ({len(items)} on this page)")
        for item in items:
            name = item.get('name') or '(untitled)'
            if key == 'creation':
                meta = []
                if item.get('created_at'):
                    meta.append(str(item['created_at'])[:10])
                counter = item.get('counter') or {}
                if counter.get('liked_count') is not None or counter.get('forked_count') is not None:
                    meta.append(f"Likes {counter.get('liked_count', 0)} / Favorites {counter.get('forked_count', 0)}")
                suffix = (' · ' + ' · '.join(meta)) if meta else ''
                lines.append(f"- **{name}**{suffix}")
            else:
                meta = []
                if item.get('asset_type'):
                    meta.append(str(item['asset_type']))
                if item.get('base_models'):
                    meta.append('/'.join(item['base_models']))
                if item.get('latest_version'):
                    meta.append(str(item['latest_version']))
                suffix = (' · ' + ' · '.join(meta)) if meta else ''
                lines.append(f"- **{name}**{suffix}")
            if item.get('page_url'):
                lines.append(f"  {item['page_url']}")
        lines.append('')
    if not emitted:
        lines.append('(no matching content)')
    sources = payload.get('sources') or {}
    hidden = []
    for name in ('creations', 'assets'):
        source = sources.get(name) or {}
        if source.get('requested') and source.get('total') is not None and source.get('total') > source.get('count', 0):
            hidden.append(f"{'Works' if name == 'creations' else 'Models/AI apps'}: {source['total']} in total")
    if hidden:
        lines.append(f"({'; '.join(hidden)}; only page {payload.get('query', {}).get('page', 1)} was fetched; use --page to page through.)")
    if payload.get('error'):
        lines.append(f"\nSome data failed to load: {payload.get('message')}")
    return '\n'.join(lines).rstrip() + '\n'


def cmd_published(rest: list[str]) -> None:
    _cmd_grouped(rest, forked=False)


def cmd_forked(rest: list[str]) -> None:
    _cmd_grouped(rest, forked=True)


def _cmd_grouped(rest: list[str], *, forked: bool) -> None:
    options = _parse_list_args(rest)
    key = api.require_api_key(options['api_key'])
    profile = _profile_or_fail(key, options['reply_format'])
    user_id = str(profile.get('id') or '')
    if not user_id:
        print('Cannot read the current user info; check that the API key is valid', file=sys.stderr)
        raise SystemExit(1)
    page_size = max(20, options['limit'])
    bundle = _load_bundle(
        key, user_id, forked=forked, kinds=options['kinds'], page=options['page'], page_size=page_size,
        keyword=options['keyword'], sort=options['sort'], owner_name=profile.get('nick_name'))
    payload = _build_list_payload('my-forked' if forked else 'my-published', user_id, bundle, options)
    _emit(payload, options)
    if payload.get('error'):
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

def _profile_or_fail(api_key: str, reply_format: str='markdown') -> dict[str, Any]:
    result = account.get_profile(api_key)
    if not api.result_is_ok(result):
        payload = {
            'error': 'REMOTE_REQUEST_FAILED', 'message': api.request_error_message(result),
            'http_status': result.get('status'), 'reply_markdown': f"Lookup failed: {api.request_error_message(result)}",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2) if reply_format == 'json' else payload['reply_markdown'])
        raise SystemExit(1)
    return summarize_profile_full(api.extract_result_data(result) or {})


def summarize_profile_full(data: dict[str, Any]) -> dict[str, Any]:
    summary = account.summarize_profile(data)
    summary['description'] = data.get('description')
    summary['homepages'] = [
        {'platform': item.get('social_icon'), 'url': item.get('social_url')}
        for item in (data.get('homepages') or []) if isinstance(item, dict)
    ]
    return summary


def share_url_for(profile: dict[str, Any]) -> str | None:
    user_id = profile.get('id')
    return PROFILE_SHARE_URL.format(id=user_id) if user_id else None


def build_profile_markdown(payload: dict[str, Any]) -> str:
    if payload.get('error'):
        return f"Lookup failed: {payload.get('message') or 'unknown error'}"
    profile = payload.get('profile') or {}
    lines = [f"**{profile.get('nick_name') or '(no nickname)'}**", '']
    lines.append(f"Membership level: {profile.get('level_name') or 'unknown'} (level {profile.get('level')})")
    if profile.get('email'):
        lines.append(f"Email: {profile['email']}")
    if profile.get('language'):
        lines.append(f"Language: {profile['language']}")
    if profile.get('description'):
        lines.extend(['', f"Description: {profile['description']}"])
    homepages = profile.get('homepages') or []
    if homepages:
        lines.extend(['', 'Homepages:'])
        for item in homepages:
            platform = HOME_PLATFORMS.get(str(item.get('platform')), (str(item.get('platform')), None))[0]
            lines.append(f"- {platform}: {item.get('url')}")
    shares = payload.get('share_url')
    if shares:
        lines.extend(['', f"Profile page: {shares}"])
    if payload.get('navigation'):
        lines.extend(['', '**My content**'])
        for label, command in payload['navigation']:
            lines.append(f"- {label}: `{command}`")
    if profile.get('avatar'):
        lines.extend(['', f"![avatar]({profile['avatar']})"])
    return '\n'.join(lines) + '\n'


def cmd_show(rest: list[str]) -> None:
    options = account._parse_args(rest)
    key = api.require_api_key(options['api_key'])
    profile = _profile_or_fail(key, options['reply_format'])
    payload: dict[str, Any] = {
        'scope': 'profile',
        'profile': profile,
        'share_url': share_url_for(profile),
        'navigation': [
            ('Published', 'my-profile published'),
            ('Forked', 'my-profile forked'),
            ('Generation history', 'my-profile generated'),
            ('Edit profile', 'my-profile edit'),
        ],
    }
    payload['reply_markdown'] = build_profile_markdown(payload)
    _emit(payload, options)


def cmd_share(rest: list[str]) -> None:
    options = account._parse_args(rest)
    key = api.require_api_key(options['api_key'])
    profile = _profile_or_fail(key, options['reply_format'])
    url = share_url_for(profile)
    payload: dict[str, Any] = {
        'scope': 'profile-share',
        'user_id': profile.get('id'),
        'nick_name': profile.get('nick_name'),
        'share_url': url,
        'share_text': f"My BizyAir page: {url}" if url else None,
        'reply_markdown': f"**{profile.get('nick_name') or 'My'} BizyAir page**\n\n{url}\n\nShare the link with anyone, or open it in a browser.",
    }
    _emit(payload, options)


def parse_homepages(pairs: list[str]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in pairs:
        if '=' not in raw:
            raise ValueError(f'--homepage expects platform=url, got {raw!r}')
        platform, url = raw.split('=', 1)
        platform = platform.strip().lower()
        url = url.strip()
        if platform not in HOME_PLATFORMS:
            raise ValueError(f'--homepage platform must be one of {", ".join(HOME_PLATFORMS)}, got {platform!r}')
        if platform in seen:
            raise ValueError(f'--homepage {platform} is duplicated')
        seen.add(platform)
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not parsed.hostname:
            raise ValueError(f'The {platform} homepage link must start with https://')
        domains = HOME_PLATFORMS[platform][1]
        if domains:
            host = parsed.hostname.lower()
            if not any(host == domain or host.endswith('.' + domain) for domain in domains):
                raise ValueError(f'{platform} link domain does not match: {host}')
        result.append({'social_icon': platform, 'social_url': url})
    return result


def _parse_edit_args(rest: list[str]) -> dict[str, Any]:
    options: dict[str, Any] = {
        'nickname': None, 'description': None, 'avatar': None, 'homepages': [],
        'homepages_set': False, 'clear_homepages': False, 'confirm': False,
        'api_key': None, 'reply_format': 'markdown',
    }
    index = 0
    while index < len(rest):
        flag = rest[index]
        if flag in {'--nickname', '--name', '--description', '--avatar', '--homepage', '--api-key', '--reply-format'}:
            value = _take_value(rest, index, flag)
            if flag in {'--nickname', '--name'}:
                options['nickname'] = value
            elif flag == '--description':
                options['description'] = value
            elif flag == '--avatar':
                options['avatar'] = value
            elif flag == '--homepage':
                options['homepages'].append(value)
                options['homepages_set'] = True
            elif flag == '--api-key':
                options['api_key'] = value
            elif flag == '--reply-format':
                options['reply_format'] = value
            index += 2
            continue
        if flag == '--clear-homepages':
            options['homepages_set'] = True
            options['clear_homepages'] = True
        elif flag in {'--confirm', '--confirm-edit'}:
            options['confirm'] = True
        else:
            print(f'Unknown parameter: {flag}', file=sys.stderr)
            raise SystemExit(2)
        index += 1
    if options['reply_format'] not in {'json', 'markdown'}:
        print('--reply-format only supports json or markdown', file=sys.stderr)
        raise SystemExit(2)
    return options


def _resolve_avatar(value: str | None) -> tuple[str | None, Path | None]:
    if not value:
        return None, None
    if value.startswith(('http://', 'https://')):
        parsed = urlparse(value)
        if not parsed.hostname:
            raise ValueError('--avatar URL is invalid')
        return value, None
    path = Path(value).expanduser()
    if not path.is_file():
        raise ValueError(f'--avatar file does not exist: {value}')
    if path.suffix.lower() not in AVATAR_EXTS:
        raise ValueError('--avatar only supports jpg / png / webp')
    if path.stat().st_size > AVATAR_MAX_BYTES:
        raise ValueError('--avatar must not exceed 10 MB')
    return None, path


def cmd_edit(rest: list[str]) -> None:
    options = _parse_edit_args(rest)
    if options['nickname'] is None and options['description'] is None and options['avatar'] is None and not options['homepages_set']:
        print('Provide at least one field to change: --nickname / --description / --avatar / --homepage / --clear-homepages', file=sys.stderr)
        raise SystemExit(2)
    try:
        homepages = parse_homepages(options['homepages']) if options['homepages_set'] else None
        avatar_url, avatar_path = _resolve_avatar(options['avatar'])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    if options['nickname'] is not None:
        nickname = options['nickname'].strip()
        if not 1 <= len(nickname) <= NICKNAME_MAX:
            print(f'Nickname length must be 1-{NICKNAME_MAX} characters', file=sys.stderr)
            raise SystemExit(2)
    else:
        nickname = None
    if options['description'] is not None and len(options['description']) > DESCRIPTION_MAX:
        print(f'Description must not exceed {DESCRIPTION_MAX} characters', file=sys.stderr)
        raise SystemExit(2)

    key = api.require_api_key(options['api_key'])
    current = _profile_or_fail(key, options['reply_format'])
    request_body = {
        'nick_name': nickname if nickname is not None else (current.get('nick_name') or ''),
        'description': options['description'] if options['description'] is not None else (current.get('description') or ''),
        'avatar': avatar_url if avatar_url is not None else (current.get('avatar') or ''),
        'homepages': homepages if homepages is not None else [
            {'social_icon': item.get('platform'), 'social_url': item.get('url')} for item in (current.get('homepages') or [])
        ],
    }
    changes = {
        'nick_name': {'from': current.get('nick_name'), 'to': request_body['nick_name']},
        'description': {'from': current.get('description'), 'to': request_body['description']},
        'avatar': {'from': current.get('avatar'), 'to': avatar_url if avatar_url else (str(avatar_path) if avatar_path else current.get('avatar'))},
        'homepages': {'from': current.get('homepages') or [], 'to': homepages if homepages is not None else (current.get('homepages') or [])},
    }
    payload: dict[str, Any] = {
        'scope': 'profile-edit',
        'status': 'pending_confirmation' if not options['confirm'] else 'submitting',
        'user_id': current.get('id'),
        'changes': changes,
        'request_body': request_body,
        'share_url': share_url_for(current),
        'confirm_required': not options['confirm'],
        'local_avatar': str(avatar_path) if avatar_path else None,
        'note': 'Profile changes appear on the public page immediately; by default only a preview is emitted, and --confirm submits.',
    }
    if not options['confirm']:
        payload['reply_markdown'] = build_edit_markdown(payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2) if options['reply_format'] == 'json' else payload['reply_markdown'])
        raise SystemExit(3)

    if avatar_path is not None:
        try:
            uploaded = api.upload_input_file(key, str(avatar_path))
        except Exception as exc:
            print(f'Avatar upload failed: {exc}', file=sys.stderr)
            raise SystemExit(1)
        if not uploaded:
            print('Avatar upload failed: no URL was returned', file=sys.stderr)
            raise SystemExit(1)
        request_body['avatar'] = uploaded
        changes['avatar']['to'] = uploaded
    result = api.safe_request_json('PUT', UPDATE_USER_URL, key, payload=request_body)
    payload['ok'] = api.result_is_ok(result)
    if not payload['ok']:
        payload['error'] = 'REMOTE_REQUEST_FAILED'
        payload['message'] = api.request_error_message(result)
        payload['http_status'] = result.get('status')
        payload['reply_markdown'] = f"Profile update failed: {payload['message']}"
        print(json.dumps(payload, ensure_ascii=False, indent=2) if options['reply_format'] == 'json' else payload['reply_markdown'])
        raise SystemExit(1)
    refreshed = _profile_or_fail(key, 'json')
    payload['status'] = 'updated'
    payload['profile'] = refreshed
    payload['reply_markdown'] = build_edit_success_markdown(payload, refreshed)
    print(json.dumps(payload, ensure_ascii=False, indent=2) if options['reply_format'] == 'json' else payload['reply_markdown'])


def _homepage_label(item: dict[str, Any]) -> str:
    platform = str(item.get('social_icon') or item.get('platform') or '')
    url = item.get('social_url') or item.get('url') or ''
    label = HOME_PLATFORMS.get(platform, (platform, None))[0]
    return f"{label} {url}".strip()


def build_edit_markdown(payload: dict[str, Any]) -> str:
    changes = payload.get('changes') or {}
    lines = ['**Edit personal profile (pending confirmation)**', '']
    for key, label in [('nick_name', 'Nickname'), ('description', 'Description'), ('avatar', 'Avatar')]:
        change = changes.get(key) or {}
        lines.append(f"{label}: {change.get('from') or '(empty)'} -> {change.get('to') or '(empty)'}")
    homepages = (changes.get('homepages') or {}).get('to') or []
    lines.append('Homepages: ' + (', '.join(_homepage_label(item) for item in homepages) if homepages else '(cleared / not set)'))
    if payload.get('share_url'):
        lines.extend(['', f"Profile page: {payload['share_url']}"])
    lines.extend(['', 'If everything looks right, append `--confirm` to the end of the same command; without it nothing is changed.'])
    return '\n'.join(lines) + '\n'


def build_edit_success_markdown(payload: dict[str, Any], profile: dict[str, Any]) -> str:
    lines = [f"Profile updated: **{profile.get('nick_name') or '(no nickname)'}**", '']
    if profile.get('description'):
        lines.append(f"Description: {profile['description']}")
    if payload.get('share_url'):
        lines.append(f"Profile page: {payload['share_url']}")
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def cmd_my_profile(rest: list[str]) -> None:
    if not rest or rest[0].startswith('--'):
        cmd_show(rest)
        return
    sub = rest[0].lower()
    tail = rest[1:]
    if sub in {'published', 'publish'}:
        cmd_published(tail)
    elif sub in {'forked', 'fork', 'favorites'}:
        cmd_forked(tail)
    elif sub in {'generated', 'outputs'}:
        _cmd_generated(tail)
    elif sub in {'show', 'profile', 'info'}:
        cmd_show(tail)
    elif sub in {'edit', 'update'}:
        cmd_edit(tail)
    elif sub in {'share', 'link'}:
        cmd_share(tail)
    else:
        print(f'Unknown subcommand: {sub}; choose published / forked / generated / show / edit / share', file=sys.stderr)
        raise SystemExit(2)


def cmd_profile(rest: list[str]) -> None:
    """Legacy entry point: profile shows the profile by default; profile edit/share use the new logic."""
    if rest and rest[0].lower() in {'edit', 'update', 'share', 'link', 'show', 'info'}:
        sub = rest[0].lower()
        tail = rest[1:]
        if sub in {'edit', 'update'}:
            cmd_edit(tail)
        elif sub in {'share', 'link'}:
            cmd_share(tail)
        else:
            cmd_show(tail)
        return
    cmd_show(rest)


def _cmd_generated(rest: list[str]) -> None:
    # Publishing is not available; cmd_my_generated rejects the retired flags explicitly.
    gallery.cmd_my_generated(rest)


def _emit(payload: dict[str, Any], options: dict[str, Any]) -> None:
    if options.get('reply_format') == 'json':
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload.get('reply_markdown', ''))
