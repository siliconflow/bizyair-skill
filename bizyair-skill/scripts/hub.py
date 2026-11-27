#!/usr/bin/env python3
"""hub.py — model hub capabilities, new in v1.9.

The model hub and ModelZoo are two different things; do not mix them up:
  - **Model hub (bizy_models)**: downloadable/loadable model assets with versions
    (LoRA / VAE / UNet / Workflow, etc.), each with an author, cover and file size,
    published by community users.
  - **ModelZoo**: per-call low-level endpoints (image/video/audio tasks), billed via modelzoo-run.

This module covers model hub browsing, search, detail and your own published models.
Write operations (publish/edit/delete a model) are out of scope for this version.

Endpoints (all on meta.bizyair.ai):
  GET /v1/bizy_models/community         community model list (public; supports keyword / model_types / base_models)
  GET /v1/bizy_models/official          official model list
  GET /v1/bizy_models/{id}/detail       model detail (including all versions)
  GET /v1/userspace/{user_id}/bizy_models  models published by a user (your own published models go here)

Verified: list sort accepts `Most Used` / `Recently` / `Auto`; any other value returns 400.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from typing import Any

import api
import common

META_BASE = 'https://meta.bizyair.ai'
MODELS_URL = META_BASE + '/v1/bizy_models'
COMMUNITY_URL = MODELS_URL + '/community'
OFFICIAL_URL = MODELS_URL + '/official'
DETAIL_URL = MODELS_URL + '/{model_id}/detail'
USER_MODELS_URL = META_BASE + '/v1/userspace/{user_id}/bizy_models'
# Verified: /community/models/{id} returns 404; the public model page is
# /community/models/publicity/{id} and your own published model is /community/models/my/{id}.
# The international site must not use the /zh prefix.
MODEL_PAGE_URL = 'https://www.bizyair.ai/community/models/publicity/{id}'
MY_MODEL_PAGE_URL = 'https://www.bizyair.ai/community/models/my/{id}'
APP_PAGE_URL = 'https://www.bizyair.ai/community/app/{id}'

# These two types are not "model assets": Application is an AI app, Workflow is a workflow.
# my-models lists only real models by default; models also excludes Application by default
# (Workflow still counts as an asset in the model hub).
NON_MODEL_TYPES = {'Application', 'Workflow'}

SUPPORTED_SORTS = {'Most Used', 'Recently', 'Auto'}
DEFAULT_SORT = 'Most Used'

# Types that can appear in the model hub (display and hints only, no hard validation --
# the server is the source of truth).
KNOWN_TYPES = [
    'Checkpoint', 'LoRA', 'VAE', 'UNet', 'CLIP', 'CLIP Vision', 'ControlNet',
    'Upscaler', 'Embedding', 'Workflow', 'Application', 'Other',
]

MAX_MARKDOWN_IMAGES = 6


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

def resolve_sort(value: str | None, *, source: str='community') -> str:
    raw = str(value or '').strip()
    if not raw:
        return 'Auto' if common.normalized_text(source) == 'official' else DEFAULT_SORT
    lowered = raw.lower()
    aliases = {'recent': 'Recently', 'recently': 'Recently', 'new': 'Recently',
               'most used': 'Most Used', 'popular': 'Most Used', 'auto': 'Auto'}
    return aliases.get(lowered, raw)


def normalize_split(value: Any) -> list[str]:
    """Normalize "LoRA,VAE" / ["LoRA","VAE"] into a list."""
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        parts = str(value or '').replace('，', ',').split(',')  # internal input alias; never displayed
    return [p.strip() for p in parts if p.strip()]


def list_models(keyword: str | None=None, *, source: str='community', model_types: Any=None, base_models: Any=None, sort: str | None=None, page: int=1, page_size: int=20) -> dict[str, Any]:
    """Listing uses public requests: the model list itself needs no auth, and an invalid key may cause a 401."""
    # Verified in v1.12.3: `/v1/bizy_models/community?mode=official` is ignored by the server
    # and still returns all community models; filtering to official must use
    # `/v1/bizy_models/official` (currently only 1 public object).
    url = OFFICIAL_URL if common.normalized_text(source) == 'official' else COMMUNITY_URL
    params: list[tuple[str, Any]] = [
        ('current', max(1, int(page))),
        ('page_size', max(1, min(100, int(page_size)))),
        ('sort', resolve_sort(sort, source=source)),
    ]
    keyword = str(keyword or '').strip()
    if keyword:
        params.append(('keyword', keyword))
    for model_type in normalize_split(model_types):
        params.append(('model_types', model_type))
    for base_model in normalize_split(base_models):
        params.append(('base_models', base_model))
    return api.safe_request_json_public('GET', url, params=params)


def model_detail(api_key: str, model_id: str | int) -> dict[str, Any]:
    return api.safe_request_json('GET', DETAIL_URL.format(model_id=model_id), api_key)


def my_models(api_key: str, user_id: str, *, keyword: str | None=None, sort: str | None=None, page: int=1, page_size: int=20) -> dict[str, Any]:
    params: list[tuple[str, Any]] = [
        ('current', max(1, int(page))),
        ('page_size', max(1, min(100, int(page_size)))),
        ('sort', resolve_sort(sort)),
    ]
    keyword = str(keyword or '').strip()
    if keyword:
        params.append(('keyword', keyword))
    return api.safe_request_json('GET', USER_MODELS_URL.format(user_id=user_id), api_key, params=params)


def current_user_id(api_key: str) -> str | None:
    data = api.extract_result_data(api.safe_request_json('GET', META_BASE + '/v1/user/info', api_key)) or {}
    return data.get('id')


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------

def summarize_version(version: dict[str, Any]) -> dict[str, Any]:
    return {
        'version_id': version.get('id'),
        'version': version.get('version'),
        'base_model': version.get('base_model'),
        'file_name': version.get('file_name') or version.get('path'),
        'file_size': version.get('file_size'),
        'available': version.get('available'),
        'public': version.get('public'),
        'created_at': version.get('created_at'),
        'updated_at': version.get('updated_at'),
        'cover_url': (version.get('cover_urls') or [None])[0],
        'sign': version.get('sign'),
        'description': version.get('description'),
    }


def page_url_for(item: dict[str, Any], *, page_variant: str='publicity') -> str | None:
    """Return the page that actually opens, keyed by type.

    Verified (v1.9.1):
      Application -> /community/app/{id} (/community/models/* only renders the generic Model Details)
      Workflow    -> /community/models/publicity/{id} (the title correctly says Workflow)
      Other models -> /community/models/publicity/{id}; your own published ones use /community/models/my/{id}
    """
    model_id = item.get('id')
    if model_id is None:
        return None
    kind = str(item.get('type') or '')
    if kind == 'Application':
        versions = item.get('versions') or []
        version_id = versions[0].get('id') if versions and isinstance(versions[0], dict) else None
        return APP_PAGE_URL.format(id=version_id or model_id)
    if kind == 'Workflow':
        return MODEL_PAGE_URL.format(id=model_id)
    return (MY_MODEL_PAGE_URL if page_variant == 'my' else MODEL_PAGE_URL).format(id=model_id)


def summarize_model(item: dict[str, Any], *, page_variant: str='publicity') -> dict[str, Any]:
    versions = item.get('versions') or []
    first = versions[0] if versions and isinstance(versions[0], dict) else {}
    covers = first.get('cover_urls') or item.get('cover_urls') or []
    return {
        'id': item.get('id'),
        'name': item.get('name'),
        'type': item.get('type'),
        'owner': item.get('user_nick_name'),
        'owner_id': item.get('user_id'),
        'base_models': sorted({str(v.get('base_model')) for v in versions if isinstance(v, dict) and v.get('base_model')}),
        'version_count': len(versions),
        'latest_version': first.get('version'),
        'file_size': first.get('file_size'),
        'created_at': first.get('created_at') or item.get('created_at'),
        'cover_url': covers[0] if covers else None,
        'page_url': page_url_for(item, page_variant=page_variant),
    }


def summarize_models(items: list[Any], *, page_variant: str='publicity') -> list[dict[str, Any]]:
    return [summarize_model(i, page_variant=page_variant) for i in items or [] if isinstance(i, dict)]


def extract_page(result: dict[str, Any]) -> dict[str, Any]:
    data = api.extract_result_data(result) or {}
    if isinstance(data.get('data'), dict):
        data = data['data']
    items = data.get('list') or []
    return {'list': items if isinstance(items, list) else [], 'total': data.get('total')}


def filter_model_assets(items: list[Any], *, keep_types: list[str] | None=None, include_non_models: bool=False) -> list[Any]:
    """Keep only real model assets by default.

    - With an explicit `--type Application` / `--type Workflow`, respect the user's choice
      (keep whatever matches keep_types);
    - `--include-non-models` keeps everything.
    """
    keep = {str(t) for t in (keep_types or [])}
    results = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get('type') or '')
        if include_non_models or kind not in NON_MODEL_TYPES or kind in keep:
            results.append(item)
    return results


def _fmt_size(size: Any) -> str:
    try:
        value = int(size)
    except Exception:
        return ''
    if value <= 0:
        return ''
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            return f'{value:.1f}{unit}' if unit != 'B' else f'{value}B'
        value /= 1024
    return ''


# ---------------------------------------------------------------------------
# markdown
# ---------------------------------------------------------------------------

def build_list_markdown(payload: dict[str, Any]) -> str:
    head = f"**Model hub{' (official)' if payload.get('source') == 'official' else ''}**"
    query = payload.get('query') or {}
    if query.get('keyword'):
        head += f" · keyword: {query['keyword']}"
    if query.get('model_types'):
        head += f" · type {'/'.join(query['model_types'])}"
    if query.get('base_models'):
        head += f" · base model {'/'.join(query['base_models'])}"
    if payload.get('total') is not None:
        head += f" · {payload['total']} total"
    lines = [head, '']

    if payload.get('error'):
        lines.append(f"Lookup failed: {payload.get('message') or 'unknown error'}")
        return '\n'.join(lines)

    results = payload.get('results') or []
    if not results:
        lines.append('No models found. Try another keyword or loosen the type filter.')
        return '\n'.join(lines)

    for index, model in enumerate(results, start=1):
        bits = [model.get('type') or 'unknown type']
        if model.get('base_models'):
            bits.append('base model ' + '/'.join(model['base_models']))
        if model.get('owner'):
            bits.append(model['owner'])
        size = _fmt_size(model.get('file_size'))
        if size:
            bits.append(size)
        lines.append(f"{index}. **{model.get('name') or '(untitled)'}** — {' · '.join(bits)}")
        meta = []
        if model.get('version_count'):
            meta.append(f"versions: {model['version_count']}")
        if model.get('latest_version'):
            meta.append(f"latest {model['latest_version']}")
        if meta:
            lines.append(f"   {' · '.join(meta)}")
        if model.get('page_url'):
            lines.append(f"   {model['page_url']}")
        if model.get('cover_url') and index <= MAX_MARKDOWN_IMAGES:
            lines.append(f"   ![{model.get('id')}]({model['cover_url']})")
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def build_detail_markdown(payload: dict[str, Any]) -> str:
    if payload.get('error'):
        return f"Lookup failed: {payload.get('message') or 'unknown error'}"
    model = payload.get('model') or {}
    lines = [f"**{model.get('name')}**", '']
    lines.append(f"Type: {model.get('type') or 'unknown'} · Author: {model.get('owner') or 'unknown'}")
    if model.get('base_models'):
        lines.append(f"Base model: {'/'.join(model['base_models'])}")
    if model.get('version_count'):
        lines.append(f"Version count: {model['version_count']}")
    if model.get('page_url'):
        lines.append(f"Page: {model['page_url']}")
    versions = model.get('versions') or []
    if versions:
        lines.extend(['', '**Versions**'])
        for index, version in enumerate(versions[:10], start=1):
            bits = [version.get('version') or '(no version name)']
            if version.get('base_model'):
                bits.append(version['base_model'])
            size = _fmt_size(version.get('file_size'))
            if size:
                bits.append(size)
            bits.append('available' if version.get('available') else 'unavailable')
            lines.append(f"{index}. {' · '.join(bits)}")
    if model.get('cover_url'):
        lines.extend(['', f"![{model.get('id')}]({model['cover_url']})"])
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _take_value(rest: list[str], index: int, flag: str) -> str:
    if index + 1 >= len(rest):
        print(f'Parameter {flag} is missing a value', file=sys.stderr)
        raise SystemExit(2)
    return rest[index + 1]


def _positive_int_option(value: str, flag: str) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        print(f'{flag} must be an integer >= 1', file=sys.stderr)
        raise SystemExit(2)
    if number < 1:
        print(f'{flag} must be an integer >= 1', file=sys.stderr)
        raise SystemExit(2)
    return number

def _parse_list_args(rest: list[str]) -> dict[str, Any]:
    options: dict[str, Any] = {
        'keyword': '', 'source': 'community', 'model_types': [], 'base_models': [],
        'sort': None, 'page': 1, 'page_size': 20, 'limit': 20,
        'reply_format': 'markdown', 'api_key': None, 'include_non_models': False,
    }
    positionals: list[str] = []
    index = 0
    while index < len(rest):
        flag = rest[index]
        if flag in {'--keyword', '--source', '--type', '--base-model', '--sort', '--page',
                    '--page-size', '--limit', '--reply-format', '--api-key'}:
            value = _take_value(rest, index, flag)
            if flag == '--keyword':
                options['keyword'] = value
            elif flag == '--source':
                options['source'] = value
            elif flag == '--type':
                options['model_types'] = normalize_split(value)
            elif flag == '--base-model':
                options['base_models'] = normalize_split(value)
            elif flag == '--sort':
                options['sort'] = value
            elif flag == '--page':
                options['page'] = _positive_int_option(value, flag)
            elif flag == '--page-size':
                options['page_size'] = min(100, _positive_int_option(value, flag))
            elif flag == '--limit':
                options['limit'] = min(100, _positive_int_option(value, flag))
            elif flag == '--reply-format':
                options['reply_format'] = value
            elif flag == '--api-key':
                options['api_key'] = value
            index += 2
            continue
        if flag == '--include-non-models':
            options['include_non_models'] = True
            index += 1
            continue
        if not flag.startswith('--'):
            positionals.append(flag)
        index += 1
    if positionals and not options['keyword']:
        options['keyword'] = positionals[0]
    if options['source'] not in {'community', 'official'}:
        print('--source only supports community or official', file=sys.stderr)
        raise SystemExit(2)
    if options['reply_format'] not in {'json', 'markdown'}:
        print('--reply-format only supports json or markdown', file=sys.stderr)
        raise SystemExit(2)
    return options


def cmd_models(rest: list[str]) -> None:
    options = _parse_list_args(rest)
    sort = resolve_sort(options['sort'], source=options['source'])
    if sort not in SUPPORTED_SORTS:
        print(json.dumps({'error': 'SORT_NOT_SUPPORTED', 'message': 'The model hub only supports --sort "Most Used" / Recently / Auto.',
                          'given': options['sort'], 'supported': sorted(SUPPORTED_SORTS)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(2)

    result = list_models(options['keyword'] or None, source=options['source'], model_types=options['model_types'],
                         base_models=options['base_models'], sort=sort, page=options['page'], page_size=options['page_size'])
    page = extract_page(result) if api.result_is_ok(result) else {'list': [], 'total': None}
    payload: dict[str, Any] = {
        'source': options['source'],
        'query': {'keyword': options['keyword'] or None, 'model_types': options['model_types'],
                  'base_models': options['base_models'], 'sort': sort, 'page': options['page'], 'page_size': options['page_size']},
        'count': min(len(page['list']), options['limit']),
        'total': page['total'],
        'results': summarize_models(page['list'])[: options['limit']],
    }
    if not api.result_is_ok(result):
        payload['error'] = 'REMOTE_REQUEST_FAILED'
        payload['message'] = api.request_error_message(result)
        payload['http_status'] = result.get('status')
        payload['reply_markdown'] = build_list_markdown(payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1)

    payload['reply_markdown'] = build_list_markdown(payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2) if options['reply_format'] == 'json' else payload['reply_markdown'])


def cmd_model_detail(rest: list[str]) -> None:
    options = _parse_list_args(rest)
    args = [r for r in rest if not r.startswith('--')]
    if not args:
        print('Usage: cli.py model-detail <model ID or link> [--api-key KEY] [--reply-format json]', file=sys.stderr)
        raise SystemExit(2)
    key = api.require_api_key(options['api_key'])
    model_id = api.extract_remote_target_id(args[0]) or args[0]

    result = model_detail(key, model_id)
    payload: dict[str, Any] = {'model_id': model_id}
    if not api.result_is_ok(result):
        payload.update({'error': 'REMOTE_REQUEST_FAILED', 'message': api.request_error_message(result),
                        'http_status': result.get('status')})
        payload['reply_markdown'] = build_detail_markdown(payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1)

    data = api.extract_result_data(result) or {}
    model = summarize_model(data)
    model['versions'] = [summarize_version(v) for v in (data.get('versions') or []) if isinstance(v, dict)]
    model['version_count'] = len(model['versions'])
    model['description'] = data.get('user_description')
    payload['model'] = model
    payload['reply_markdown'] = build_detail_markdown(payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2) if options['reply_format'] == 'json' else payload['reply_markdown'])


def cmd_my_models(rest: list[str]) -> None:
    options = _parse_list_args(rest)
    key = api.require_api_key(options['api_key'])
    user_id = current_user_id(key)
    if not user_id:
        print('Cannot read the current user info; check that the API key is valid', file=sys.stderr)
        raise SystemExit(1)

    result = my_models(key, user_id, keyword=options['keyword'] or None, sort=options['sort'],
                       page=options['page'], page_size=options['page_size'])
    page = extract_page(result) if api.result_is_ok(result) else {'list': [], 'total': None}
    server_total = page['total']
    if api.result_is_ok(result):
        page['list'] = filter_model_assets(page['list'], keep_types=options['model_types'],
                                            include_non_models=options['include_non_models'])
        page['total'] = len(page['list'])
    payload: dict[str, Any] = {
        'scope': 'my-models',
        'user_id': user_id,
        'query': {'keyword': options['keyword'] or None, 'sort': resolve_sort(options['sort']), 'page': options['page']},
        'count': min(len(page['list']), options['limit']),
        'total': page['total'],
        'server_total': server_total,
        'results': summarize_models(page['list'], page_variant='my')[: options['limit']],
    }
    if not api.result_is_ok(result):
        payload.update({'error': 'REMOTE_REQUEST_FAILED', 'message': api.request_error_message(result),
                        'http_status': result.get('status')})
        payload['reply_markdown'] = build_list_markdown(payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1)

    markdown = build_list_markdown(payload).replace('**Model hub**', '**Models I published**', 1)
    hidden = (server_total or 0) - len(page['list']) if server_total is not None else 0
    if hidden > 0 and not options['include_non_models']:
        markdown = markdown.rstrip() + f"\n\n({hidden} non-model objects -- AI apps or workflows -- are excluded; `--include-non-models` lists them too.)\n"
    payload['reply_markdown'] = markdown
    print(json.dumps(payload, ensure_ascii=False, indent=2) if options['reply_format'] == 'json' else payload['reply_markdown'])


def cmd_model_types(rest: list[str]) -> None:
    """Sample the types present in community models to give users usable --type values."""
    options = _parse_list_args(rest)
    result = list_models(None, source=options['source'], sort='Most Used', page=1, page_size=100)
    page = extract_page(result)
    if not api.result_is_ok(result):
        print(json.dumps({'error': 'REMOTE_REQUEST_FAILED', 'message': api.request_error_message(result)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1)
    counts = Counter(str(item.get('type') or 'Unknown') for item in page['list'])
    payload = {
        'source': options['source'],
        'sampled': len(page['list']),
        'types': [{'type': t, 'count': c} for t, c in counts.most_common()],
        'known_types': KNOWN_TYPES,
        'note': 'Types come from a sample (first page, 100 rows), not a full enumeration; pass a concrete type to --type.',
    }
    payload['reply_markdown'] = ('**Model types (sample of %d)**\n\n' % payload['sampled']) + '\n'.join(
        f"- {item['type']} ({item['count']})" for item in payload['types'])
    print(json.dumps(payload, ensure_ascii=False, indent=2) if options['reply_format'] == 'json' else payload['reply_markdown'])
