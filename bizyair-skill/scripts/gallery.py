#!/usr/bin/env python3
"""gallery.py — Work gallery (Gallery / creations) capabilities, new in v1.8.2.

Covers four things:
  1. Gallery browse    list_works()            POST /v1/creations/community
  2. Work search       list_works(keyword=..)  same endpoint, keyword does literal matching
  3. Like              like_work()             PUT  /v1/creations/{id}/like (additive, no unlike endpoint)
  4. Favorite          fork_work()/unfork_work() POST/DELETE /v1/creations/{id}/fork

Publishing generated outputs to the gallery is not available in this skill;
`gallery-publish` and `--publish` now return an explicit GALLERY_PUBLISH_REMOVED error and never write.

Design constraints (aligned with existing skill conventions):
  - Read operations run directly; like/favorite cost nothing and are reversible, so run directly when the
    user explicitly asks and report the new state.
  - Every output provides both a structured payload and reply_markdown, selected by --reply-format.
  - All endpoints live on meta.bizyair.ai and use the same API Key as generation tasks.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import api
import common
import paths

META_BASE = 'https://meta.bizyair.ai'
CREATIONS_URL = META_BASE + '/v1/creations'
COMMUNITY_URL = CREATIONS_URL + '/community'
MY_URL = CREATIONS_URL + '/my'
MY_FORK_URL = CREATIONS_URL + '/my_fork'
# The international site does not want a /zh prefix; /creation/{id} returns 200 in practice.
CREATION_PAGE_URL = 'https://www.bizyair.ai/creation/{id}'
USER_INFO_URL = META_BASE + '/v1/user/info'
USER_OUTPUTS_URL = META_BASE + '/v1/userspace/{user_id}/task/outputs'

# The gallery list only accepts these two sort values; anything else makes the server return 400 invalid parameter: Sort.
SORT_ALIASES = {
    'recent': 'Recently',
    'recently': 'Recently',
    'new': 'Recently',
    'latest': 'Recently',
    'recommend': 'Recommend',
    'recommended': 'Recommend',
}
DEFAULT_SORT = 'Recently'
# In practice the gallery accepts only these two sort enums; other values get a uniform 400 invalid parameter: Sort from the server.
SUPPORTED_SORTS = {'Recently', 'Recommend'}

IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'}
VIDEO_EXTS = {'.mp4', '.webm', '.mov', '.m4v'}

MAX_MARKDOWN_IMAGES = 12


# ---------------------------------------------------------------------------
# Base requests
# ---------------------------------------------------------------------------

def request(method: str, url: str, api_key: str, *, payload: dict[str, Any] | None=None, params: dict[str, Any] | None=None) -> dict[str, Any]:
    """Go through api.safe_request_json uniformly and return the {ok, status, data|error} envelope."""
    return api.safe_request_json(method, url, api_key, payload=payload, params=params)


def resolve_sort(value: str | None) -> str:
    """Normalize the sort word from the user/LLM into the server enum; unknown values pass through for the server to reject."""
    raw = str(value or '').strip()
    if not raw:
        return DEFAULT_SORT
    return SORT_ALIASES.get(raw.lower(), raw)


def normalize_page(value: Any, default: int=1) -> int:
    try:
        return max(1, int(value))
    except Exception:
        return default


def normalize_page_size(value: Any, default: int=20, maximum: int=50) -> int:
    try:
        return max(1, min(maximum, int(value)))
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Data shaping
# ---------------------------------------------------------------------------

def pick_object(item: dict[str, Any]) -> dict[str, Any]:
    """Take a work's first display object (images first), normalized to url/ext/width/height/size."""
    objects = item.get('objects') or []
    if not isinstance(objects, list):
        return {}
    images = [o for o in objects if isinstance(o, dict) and str(o.get('ext') or '').lower() in IMAGE_EXTS]
    picked = (images or [o for o in objects if isinstance(o, dict)] or [{}])[0]
    return {
        'url': picked.get('key'),
        'ext': str(picked.get('ext') or ''),
        'width': picked.get('width'),
        'height': picked.get('height'),
        'size': picked.get('size'),
        'comfy_task_output_id': picked.get('comfy_task_output_id'),
    }


def object_kind(obj: dict[str, Any]) -> str:
    ext = str(obj.get('ext') or '').lower()
    if ext in VIDEO_EXTS:
        return 'video'
    if ext in IMAGE_EXTS:
        return 'image'
    return 'file'


def owner_of(item: dict[str, Any]) -> dict[str, Any]:
    owner = item.get('owner') or item.get('author') or {}
    if not isinstance(owner, dict):
        owner = {}
    return {
        'id': owner.get('owner_id') or owner.get('id'),
        'name': owner.get('owner_nick_name') or owner.get('nick_name') or owner.get('name'),
        'avatar': owner.get('owner_avatar') or owner.get('avatar'),
    }


def counter_of(item: dict[str, Any]) -> dict[str, Any]:
    counter = item.get('counter') or {}
    if not isinstance(counter, dict):
        counter = {}
    return {
        'likes': common.safe_int(counter.get('liked_count')),
        'favorites': common.safe_int(counter.get('forked_count')),
        'views': common.safe_int(counter.get('viewed_count')),
    }


def summarize_work(item: dict[str, Any], *, owner_fallback: str | None=None) -> dict[str, Any]:
    """Compress one work from the gallery list/detail into a stable structure, shared by JSON output and markdown rendering.

    owner_fallback is used for "my published / my favorites" — those two endpoints do not return an
    owner field, so without it the author would show as "anonymous".
    """
    obj = pick_object(item)
    work_id = item.get('id')
    owner = owner_of(item)
    if not owner.get('name') and owner_fallback:
        owner['name'] = owner_fallback
    return {
        'id': work_id,
        'name': item.get('name'),
        'description': item.get('description'),
        'public': item.get('public'),
        'source': item.get('source'),
        'task_id': item.get('task_id'),
        'created_at': item.get('created_at'),
        'kind': object_kind(obj),
        'url': obj.get('url'),
        'width': obj.get('width'),
        'height': obj.get('height'),
        'size': obj.get('size'),
        'comfy_task_output_id': obj.get('comfy_task_output_id'),
        'owner': owner,
        'counter': counter_of(item),
        'liked': item.get('liked'),
        'favorited': item.get('forked'),
        'page_url': CREATION_PAGE_URL.format(id=work_id) if work_id is not None else None,
        'related_resources': extract_related_resources(item) if item.get('resources_used') else None,
    }


def extract_related_resources(item: dict[str, Any]) -> dict[str, Any]:
    """Take `resources_used` (Related Resources) from work detail.

    Observed structure (v1.10.1):
      resources_used.webapp = {id, name, cover_url, usable, base_model, type, version, bizy_model_version_id}
      resources_used.models = [{id, name, cover_url, usable, base_model, type, ...}]
    The list endpoint does not return this field; only /v1/creations/{id}/detail has it.
    """
    raw = item.get('resources_used')
    if not isinstance(raw, dict):
        return {'webapp': None, 'models': []}

    def one(entry: Any) -> dict[str, Any] | None:
        if not isinstance(entry, dict):
            return None
        return {
            'id': entry.get('id'),
            'bizy_model_version_id': entry.get('bizy_model_version_id'),
            'ref_bizy_model_id': entry.get('ref_bizy_model_id'),
            'ref_bizy_model_version_id': entry.get('ref_bizy_model_version_id'),
            'name': entry.get('name'),
            'type': entry.get('type'),
            'base_model': entry.get('base_model'),
            'version': entry.get('version'),
            'usable': entry.get('usable'),
            'cover_url': entry.get('cover_url'),
        }

    models = [one(entry) for entry in (raw.get('models') or []) if isinstance(entry, dict)]
    return {'webapp': one(raw.get('webapp')), 'models': [m for m in models if m]}


def resource_labels(resources: dict[str, Any] | None) -> list[str]:
    """Compress resources into readable labels, e.g. ['PLAYGROUND · Qwen-Image 2.1'] / ['LoRA · my-lora']."""
    if not isinstance(resources, dict):
        return []
    labels: list[str] = []
    webapp = resources.get('webapp') or {}
    if isinstance(webapp, dict) and (webapp.get('name') or webapp.get('id')):
        kind = webapp.get('type') or 'Application'
        base = webapp.get('base_model')
        label = f'{kind} · {webapp.get("name") or webapp.get("id")}'
        if base:
            label += f' ({base})'
        labels.append(label)
    for model in resources.get('models') or []:
        if not isinstance(model, dict):
            continue
        kind = model.get('type') or 'Model'
        line = f'{kind} · {model.get("name") or model.get("id")}'
        if model.get('base_model'):
            line += f' ({model.get("base_model")})'
        labels.append(line)
    return labels


def summarize_works(items: list[Any], *, owner_fallback: str | None=None) -> list[dict[str, Any]]:
    return [summarize_work(item, owner_fallback=owner_fallback) for item in items or [] if isinstance(item, dict)]


def _norm_model_text(value: Any) -> str:
    return re.sub(r'[^a-z0-9]+', '', str(value or '').lower())


def _is_numeric_id(value: str) -> bool:
    return str(value or '').strip().isdigit()


def _match_one_text(candidate: str, query: str) -> bool:
    """Exact model-name match (ignoring case, spaces and hyphens); no substring/word-subset matching.

    The old substring match made "Qwen Image 2.1" hit "Qwen Image 2.1 Turbo ...";
    prefer numeric IDs for model search; names must match exactly.
    """
    norm_candidate = _norm_model_text(candidate)
    norm_query = _norm_model_text(query)
    return bool(norm_candidate) and norm_candidate == norm_query


def _resource_ids(resource: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for field in ('id', 'bizy_model_version_id', 'ref_bizy_model_id', 'ref_bizy_model_version_id'):
        value = resource.get(field)
        if value not in (None, ''):
            ids.add(str(value))
    return ids


def match_resource(resources: dict[str, Any] | None, query: str) -> str | None:
    """Find a matching model in Related Resources and return its display label; None when nothing matches.

    - query is numeric: exact match on id / bizy_model_version_id / ref_bizy_model_id / ref_bizy_model_version_id;
    - query is a name: only a normalized exact name counts as a match (no substring, no base_model/type matching).
    """
    if not isinstance(resources, dict) or not str(query or '').strip():
        return None
    query = str(query).strip()
    webapp = resources.get('webapp') or {}
    models = [m for m in (resources.get('models') or []) if isinstance(m, dict)]
    candidates = ([webapp] if isinstance(webapp, dict) and webapp else []) + models
    if _is_numeric_id(query):
        for resource in candidates:
            if query in _resource_ids(resource):
                return str(resource.get('name') or resource.get('id') or '')
        return None
    for resource in candidates:
        name = resource.get('name')
        if name and _match_one_text(str(name), query):
            return str(name)
    return None



def list_works(api_key: str, *, keyword: str | None=None, sort: str | None=None, page: int=1, page_size: int=20, mine: bool=False, favorites: bool=False) -> dict[str, Any]:
    """Gallery list / search / my published / my favorites.

    Gallery and search share one endpoint (POST + keyword); "my published" and "my favorites" use GET
    paginated endpoints.
    """
    keyword = str(keyword or '').strip()
    resolved_sort = resolve_sort(sort)
    page = normalize_page(page)
    page_size = normalize_page_size(page_size)

    if mine or favorites:
        url = MY_FORK_URL if favorites else MY_URL
        params: dict[str, Any] = {'current': page, 'page_size': page_size, 'sort': resolved_sort}
        if keyword:
            params['keyword'] = keyword
        return request('GET', url, api_key, params=params)

    payload: dict[str, Any] = {'current': page, 'page_size': page_size, 'sort': resolved_sort}
    if keyword:
        payload['keyword'] = keyword
    return request('POST', COMMUNITY_URL, api_key, payload=payload)


APP_WORKS_URL = CREATIONS_URL + '/bizy_model_version/{version_id}'


def resolve_app_version(api_key: str, target: str | int) -> dict[str, Any]:
    """Normalize an app link / model ID / version ID into a bizy_model_version_id.

    Observed field hierarchy (v1.12):
      bizy_models.id                     = app/model asset ID (not accepted by web page /community/app/)
      bizy_models.versions[].id          = bizy_model_version_id (app page / community/app/{version_id})
      resources_used.webapp.id           = app asset ID
      resources_used.webapp.bizy_model_version_id = version ID (required when querying works by app)
      resources_used.webapp.ref_bizy_model_id     = another app ID referenced by a Workflow, not its own ID
    """
    target_id = api.extract_remote_target_id(str(target or ''))
    if target_id is None:
        return {'ok': False, 'error': 'INVALID_TARGET', 'message': f'Cannot recognize this app input: {target}'}
    # 1) The input itself may already be a version ID
    webapp = api.fetch_remote_webapp_detail(api_key, target_id)
    data = api.extract_result_data(webapp) or {}
    if api.result_is_ok(webapp) and data.get('id'):
        return {
            'ok': True, 'input': str(target), 'input_id': str(target_id),
            'bizy_model_version_id': str(data['id']), 'name': data.get('name'),
            'ref_bizy_model_id': data.get('ref_bizy_model_id'), 'resolved_from': 'webapp-detail',
        }
    # 2) The input is an app asset ID: recover versions[0].id from the community/official list
    model = api.find_remote_model_by_id(api_key, target_id, remote_source='community')
    if model is None:
        model = api.find_remote_model_by_id(api_key, target_id, remote_source='official')
    versions = (model or {}).get('versions') or []
    version_id = versions[0].get('id') if versions and isinstance(versions[0], dict) else None
    if version_id is not None:
        webapp2 = api.fetch_remote_webapp_detail(api_key, version_id)
        data2 = api.extract_result_data(webapp2) or {}
        return {
            'ok': True, 'input': str(target), 'input_id': str(target_id),
            'bizy_model_version_id': str(version_id),
            'name': data2.get('name') or (model or {}).get('name'),
            'bizy_model_id': str((model or {}).get('id') or target_id),
            'ref_bizy_model_id': data2.get('ref_bizy_model_id'),
            'resolved_from': 'model-list',
        }
    # 3) Not an app, but possibly a model version ID directly; let the endpoint decide
    return {
        'ok': True, 'input': str(target), 'input_id': str(target_id),
        'bizy_model_version_id': str(target_id), 'name': None,
        'resolved_from': 'as-version-id',
    }


def list_works_by_app(api_key: str, version_id: str | int, *, page: int=1, page_size: int=20,
                      sort: str | None=None) -> dict[str, Any]:
    params = {'current': normalize_page(page), 'page_size': normalize_page_size(page_size, 20, 100), 'sort': resolve_sort(sort)}
    return request('GET', APP_WORKS_URL.format(version_id=version_id), api_key, params=params)


def app_works_page(result: dict[str, Any]) -> dict[str, Any]:
    data = api.extract_result_data(result) or {}
    if isinstance(data, dict) and isinstance(data.get('data'), (list, dict)):
        data = data['data']
    if isinstance(data, list):
        return {'list': [item for item in data if isinstance(item, dict)], 'total': len(data)}
    items = data.get('list') if isinstance(data, dict) else None
    if not isinstance(items, list):
        items = []
    total = data.get('total') if isinstance(data, dict) else None
    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = None
    return {'list': [item for item in items if isinstance(item, dict)], 'total': total}


RESOURCE_CACHE_TTL_SECONDS = 7 * 24 * 3600
RESOURCE_CACHE_MAX_ENTRIES = 3000


def _resource_cache_candidates() -> list[Path]:
    """Candidate cache file paths (by priority).

    In sandboxes / permission-limited setups the user state directory may not be writable, so
    `.bizyair-cache/` in the current workspace comes first, and ~/Library/Application Support/bizyair
    is the fallback.
    """
    candidates: list[Path] = []
    env_dir = os.environ.get('BIZYAIR_CACHE_DIR', '').strip()
    if env_dir:
        candidates.append(Path(env_dir).expanduser() / 'creation_resources.json')
    candidates.append(Path.cwd() / '.bizyair-cache' / 'creation_resources.json')
    candidates.append(paths.resolve_runtime_root() / 'creation_resources_cache.json')
    return candidates


def _resource_cache_path() -> Path:
    """The currently writable cache path; when none is writable, return the workspace path (saving then fails silently)."""
    for candidate in _resource_cache_candidates():
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            probe = candidate.parent / '.write-probe'
            probe.write_text('ok', encoding='utf-8')
            probe.unlink(missing_ok=True)
            return candidate
        except Exception:
            continue
    return _resource_cache_candidates()[0]


def load_resource_cache() -> dict[str, Any]:
    """Load the detail resource cache: {work_id: {"resources_used": {...}, "cached_at": epoch}}.

    Model search has to fetch detail per work (about 1.5–3 s each), so results are cached for 7 days;
    repeated searches over the same works do not request again. Cache read/write failures always
    degrade silently.
    """
    raw: Any = None
    for candidate in _resource_cache_candidates():
        try:
            raw = json.loads(candidate.read_text(encoding='utf-8'))
            break
        except Exception:
            continue
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return {}
    now = time.time()
    cleaned: dict[str, Any] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        cached_at = entry.get('cached_at')
        if not isinstance(cached_at, (int, float)) or now - cached_at > RESOURCE_CACHE_TTL_SECONDS:
            continue
        cleaned[str(key)] = entry
    return cleaned


def save_resource_cache(cache: dict[str, Any]) -> None:
    try:
        items = sorted(cache.items(), key=lambda kv: kv[1].get('cached_at') or 0, reverse=True)[:RESOURCE_CACHE_MAX_ENTRIES]
        path = _resource_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(items), ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass


def work_detail(api_key: str, work_id: str | int) -> dict[str, Any]:
    return request('GET', f'{CREATIONS_URL}/{work_id}/detail', api_key)


def search_works_by_model(api_key: str, *, model_query: str, keyword: str | None=None, sort: str | None=None,
                          page: int=1, page_size: int=20, limit: int=10, max_scan: int=2,
                          workers: int=4, refresh: bool=False) -> dict[str, Any]:
    """Search works by "calling model / Related Resources".

    The list endpoint only does keyword over name/description and does not return resources_used; to
    search by model the only way is: page through the list → concurrently fetch each work's detail
    (which contains resources_used) → match locally.
    Two limits are therefore applied: --limit (number of matches) and --max-scan (max pages), to avoid
    an unbounded scan.
    """
    model_query = str(model_query or '').strip()
    scan_pages = 0
    scanned = 0
    matches: list[dict[str, Any]] = []
    first_error: dict[str, Any] | None = None
    current = normalize_page(page)
    total: Any = None
    cache = {} if refresh else load_resource_cache()
    cache_hits = 0
    new_cache_entries: dict[str, Any] = {}

    while scan_pages < max_scan and len(matches) < limit:
        result = list_works(api_key, keyword=keyword or None, sort=sort, page=current, page_size=page_size)
        if not api.result_is_ok(result):
            first_error = result
            break
        page_data = extract_page(result)
        if total is None:
            total = page_data.get('total')
        items = [item for item in page_data['list'] if isinstance(item, dict)]
        if not items:
            break
        scan_pages += 1

        def fetch(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
            work_id = item.get('id')
            entry = cache.get(str(work_id))
            if isinstance(entry, dict) and isinstance(entry.get('resources_used'), dict):
                cached_work = entry.get('work') if isinstance(entry.get('work'), dict) else {}
                return item, {**item, **cached_work, 'resources_used': entry['resources_used']}, True
            detail = work_detail(api_key, work_id)
            data = api.extract_result_data(detail) if api.result_is_ok(detail) else None
            if isinstance(data, dict) and isinstance(data.get('resources_used'), dict):
                new_cache_entries[str(work_id)] = {
                    'resources_used': data['resources_used'],
                    'work': {key: value for key, value in data.items() if key != 'resources_used'},
                    'cached_at': time.time(),
                }
            return item, {**item, **data} if isinstance(data, dict) else item, False

        # Batch the detail fetches + stop early: once enough matches are found, stop hitting the detail endpoint (each detail takes ~1.5–3 s)
        batch_size = max(1, workers)
        for start in range(0, len(items), batch_size):
            chunk = items[start:start + batch_size]
            with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as pool:
                fetched = list(pool.map(fetch, chunk))
            scanned += len(chunk)
            for item, detail, from_cache in fetched:
                if from_cache:
                    cache_hits += 1
                source = detail or item
                resources = extract_related_resources(source)
                matched = match_resource(resources, model_query)
                if not matched:
                    continue
                summary = summarize_work(source)
                summary['matched_resource'] = matched
                summary['resource_labels'] = resource_labels(resources)
                matches.append(summary)
            print(f"Scanned {scanned}/{total or '?'} · matched {len(matches)}{' · cached ' + str(cache_hits) if cache_hits else ''}", file=sys.stderr)
            if len(matches) >= limit:
                break
        if len(matches) >= limit:
            break
        current += 1

    if new_cache_entries:
        cache.update(new_cache_entries)
        save_resource_cache(cache)

    return {
        'ok': first_error is None,
        'error_result': first_error,
        'total': total,
        'scanned': scanned,
        'scan_pages': scan_pages,
        'cache_hits': cache_hits,
        'results': matches[:limit],
    }



def extract_page(result: dict[str, Any]) -> dict[str, Any]:
    """Extract {list,total,current,page_size} from a list response; also tolerates data being the paginated object directly."""
    data = api.extract_result_data(result) or {}
    if isinstance(data.get('data'), dict):
        data = data['data']
    items = data.get('list') or []
    return {
        'list': items if isinstance(items, list) else [],
        'total': data.get('total'),
        'current': data.get('current'),
        'page_size': data.get('page_size'),
    }


# ---------------------------------------------------------------------------
# Write operations: like / favorite
# ---------------------------------------------------------------------------

def current_user_id(api_key: str) -> str | None:
    """Get the user_id for the current API Key (needed by the userspace outputs endpoint)."""
    data = api.extract_result_data(request('GET', USER_INFO_URL, api_key)) or {}
    return data.get('id') or data.get('user_id')


def fetch_published_output_map(api_key: str, *, max_pages: int=5, page_size: int=50) -> dict[int, int]:
    """Output id → work id for published works.

    `objects[].comfy_task_output_id` in work detail/list stores the **row id** from the "my generated"
    list (`outputs[].id`), not the comfy-internal id in `outputs[].comfy_task_output_id`,
    so the mapping is keyed by row id.
    """
    mapping: dict[int, int] = {}
    for page in range(1, max_pages + 1):
        result = request('GET', MY_URL, api_key, params={'current': page, 'page_size': page_size, 'sort': 'Recently'})
        if not api.result_is_ok(result):
            break
        data = api.extract_result_data(result) or {}
        items = data.get('list') or []
        for item in items:
            if not isinstance(item, dict):
                continue
            for obj in item.get('objects') or []:
                if not isinstance(obj, dict):
                    continue
                output_id = obj.get('comfy_task_output_id')
                if output_id in (None, ''):
                    continue
                try:
                    mapping[int(output_id)] = int(item.get('id'))
                except (TypeError, ValueError):
                    continue
        if len(items) < page_size:
            break
    return mapping


def list_my_outputs(api_key: str, *, user_id: str, page: int=1, page_size: int=20, types: list[str] | None=None) -> dict[str, Any]:
    params: list[tuple[str, Any]] = [('current', page), ('page_size', page_size)]
    for type_name in types or []:
        params.append(('type', type_name))
    result = request('GET', USER_OUTPUTS_URL.format(user_id=user_id), api_key, params=params)
    if not api.result_is_ok(result):
        return {'ok': False, 'error': result, 'outputs': [], 'total': None}
    data = api.extract_result_data(result) or {}
    outputs = [item for item in (data.get('outputs') or []) if isinstance(item, dict)]
    return {'ok': True, 'outputs': outputs, 'total': data.get('total')}


def output_kind(item: dict[str, Any]) -> str:
    ext = str(item.get('output_ext') or '').lower()
    if ext in VIDEO_EXTS:
        return 'video'
    if ext in IMAGE_EXTS:
        return 'image'
    return 'file'


def summarize_output(item: dict[str, Any], published: dict[int, int]) -> dict[str, Any]:
    output_id = item.get('id')
    try:
        work_id = published.get(int(output_id)) if output_id is not None else None
    except (TypeError, ValueError):
        work_id = None
    expired_at = item.get('expired_at')
    expired = False
    if expired_at:
        try:
            from datetime import datetime, timezone
            expired = datetime.fromisoformat(str(expired_at).replace('Z', '+00:00')) < datetime.now(timezone.utc)
        except Exception:
            expired = False
    publishable = item.get('task_type') != 'PLAYGROUND'
    return {
        'output_id': output_id,
        'comfy_output_id': item.get('comfy_task_output_id'),
        'task_id': item.get('task_id'),
        'task_name': item.get('task_name'),
        'task_type': item.get('task_type'),
        'kind': output_kind(item),
        'url': item.get('object_url'),
        'ext': item.get('output_ext'),
        'created_at': item.get('created_at'),
        'expired_at': expired_at,
        'expired': expired,
        'publishable': publishable,
        'status': 'published' if work_id else ('unpublished' if publishable else 'not_publishable'),
        'work_id': work_id,
        'work_url': CREATION_PAGE_URL.format(id=work_id) if work_id else None,
    }


def like_work(api_key: str, work_id: str | int) -> dict[str, Any]:
    """Like a work.

    Observed behaviour (verified online in v1.8.2): `PUT /v1/creations/{id}/like` is **additive**, not
    a toggle, so repeated calls keep raising liked_count; no unlike endpoint was found (DELETE on the
    same path times out / 404s).
    The command layer therefore calls it once, only when the user explicitly asks, with no automatic
    retry.
    """
    return request('PUT', f'{CREATIONS_URL}/{work_id}/like', api_key)


def fork_work(api_key: str, work_id: str | int) -> dict[str, Any]:
    return request('POST', f'{CREATIONS_URL}/{work_id}/fork', api_key)


def unfork_work(api_key: str, work_id: str | int) -> dict[str, Any]:
    return request('DELETE', f'{CREATIONS_URL}/{work_id}/unfork', api_key)


# ---------------------------------------------------------------------------
# Removed write operation: gallery publishing
# ---------------------------------------------------------------------------

GALLERY_PUBLISH_REMOVED_MESSAGE = (
    'Publishing generated outputs to the work gallery is not available in this skill. '
    'Gallery reads, search, like, favorite and unfavorite still work.'
)

def reject_gallery_publish() -> None:
    """Hard stop for the retired work-publishing entry points; no request is sent."""
    print(json.dumps({
        'error': 'GALLERY_PUBLISH_REMOVED',
        'message': GALLERY_PUBLISH_REMOVED_MESSAGE,
    }, ensure_ascii=False), file=sys.stderr)
    raise SystemExit(2)


# ---------------------------------------------------------------------------
# markdown rendering
# ---------------------------------------------------------------------------

def _fmt_size(size: Any) -> str:
    try:
        value = int(size)
    except Exception:
        return ''
    if value <= 0:
        return ''
    if value >= 1024 * 1024:
        return f'{value / 1024 / 1024:.1f}MB'
    return f'{value / 1024:.0f}KB'


def _fmt_dimensions(work: dict[str, Any]) -> str:
    width, height = work.get('width'), work.get('height')
    if width and height:
        return f'{width}×{height}'
    return ''


def build_list_markdown(payload: dict[str, Any]) -> str:
    scope = payload.get('scope') or 'community'
    scope_label = {'community': 'Work gallery', 'search': 'Work search', 'mine': 'My published', 'favorites': 'My favorites', 'app-works': 'App works'}.get(scope, scope)
    keyword = (payload.get('query') or {}).get('keyword')
    model_query = (payload.get('query') or {}).get('model')
    sort_label = {'Recently': 'recent', 'Recommend': 'recommended'}.get((payload.get('query') or {}).get('sort'), payload.get('query', {}).get('sort'))
    total = payload.get('total')
    results = payload.get('results') or []

    head = f'**{scope_label}**'
    if model_query:
        head += f' · model "{model_query}"'
    if keyword:
        head += f' · keyword "{keyword}"'
    if sort_label:
        head += f' · sorted by {sort_label}'
    if total is not None:
        head += f' · {total} works'
    if payload.get('scope') == 'model-search' and payload.get('scanned'):
        head += f" · scanned {payload.get('scanned')} works"
    lines = [head, '']

    if payload.get('error'):
        lines.append(f"Query failed: {payload.get('message') or 'unknown error'}")
        return '\n'.join(lines)

    if not results:
        if scope == 'app-works':
            lines.append('The endpoint returned 0 works: this means the current version ID has no public works associated, which does not mean exporting the workflow failed, and says nothing about app quality.')
        else:
            lines.append('No works found. Try another keyword, or browse the gallery directly.')
        return '\n'.join(lines)

    for index, work in enumerate(results, start=1):
        dims = _fmt_dimensions(work)
        size = _fmt_size(work.get('size'))
        counter = work.get('counter') or {}
        owner = (work.get('owner') or {}).get('name') or 'anonymous'
        meta_bits = [owner]
        if dims:
            meta_bits.append(dims)
        if size:
            meta_bits.append(size)
        meta_bits.append(f"👍{counter.get('likes', 0)} ⭐{counter.get('favorites', 0)}")
        lines.append(f"{index}. **{work.get('name') or '(untitled)'}** — {' · '.join(meta_bits)}")
        if work.get('matched_resource'):
            lines.append(f"   Model: {work['matched_resource']}")
        if work.get('page_url'):
            lines.append(f"   {work['page_url']}")
        if index <= MAX_MARKDOWN_IMAGES and work.get('kind') == 'image' and work.get('url'):
            lines.append(f"   ![Work {work.get('id')}]({work['url']})")
        elif index <= MAX_MARKDOWN_IMAGES and work.get('kind') == 'video' and work.get('url'):
            lines.append(f"   {work['url']}")
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def build_detail_markdown(payload: dict[str, Any]) -> str:
    work = payload.get('work') or {}
    if payload.get('error'):
        return f"Query failed: {payload.get('message') or 'unknown error'}"
    lines = [f"**{work.get('name') or '(untitled)'}**", '']
    owner = (work.get('owner') or {}).get('name') or 'anonymous'
    counter = work.get('counter') or {}
    lines.append(f"Author: {owner} · Source: {work.get('source') or 'unknown'}")
    dims = _fmt_dimensions(work)
    if dims:
        lines.append(f"Size: {dims} · file size: {_fmt_size(work.get('size')) or 'unknown'}")
    lines.append(f"Likes {counter.get('likes', 0)} · favorites {counter.get('favorites', 0)} · views {counter.get('views', 0)}")
    if work.get('description'):
        lines.extend(['', work['description']])
    labels = resource_labels(work.get('related_resources'))
    if labels:
        lines.extend(['', 'Related resources:'])
        lines.extend(f'- {label}' for label in labels)
    if work.get('url'):
        lines.extend(['', f"![Work {work.get('id')}]({work['url']})"])
    if work.get('page_url'):
        lines.extend(['', f"Work page: {work['page_url']}"])
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def _take_value(rest: list[str], index: int, flag: str) -> str:
    if index + 1 >= len(rest):
        print(f'Parameter {flag} is missing a value', file=sys.stderr)
        raise SystemExit(2)
    return rest[index + 1]


def _parse_common_args(rest: list[str]) -> dict[str, Any]:
    """Parse the common arguments of list-style commands; the first non-flag positional is the keyword."""
    options: dict[str, Any] = {
        'keyword': '', 'model': '', 'sort': None, 'page': 1, 'page_size': 20,
        'max_scan': 2, 'refresh': False,
        'reply_format': 'markdown', 'api_key': None, 'limit': 20,
    }
    positionals: list[str] = []
    index = 0
    while index < len(rest):
        flag = rest[index]
        if flag in {'--keyword', '--model', '--generated-by', '--max-scan', '--sort', '--page', '--page-size', '--reply-format', '--api-key', '--limit'}:
            value = _take_value(rest, index, flag)
            if flag == '--keyword':
                options['keyword'] = value
            elif flag in {'--model', '--generated-by'}:
                options['model'] = value
            elif flag == '--max-scan':
                options['max_scan'] = max(1, min(20, normalize_page_size(value, 4, 20)))
            elif flag == '--sort':
                options['sort'] = value
            elif flag == '--page':
                options['page'] = normalize_page(value)
            elif flag == '--page-size':
                options['page_size'] = normalize_page_size(value)
            elif flag == '--limit':
                options['limit'] = max(1, min(100, normalize_page_size(value, 20, 100)))
            elif flag == '--reply-format':
                options['reply_format'] = value
            elif flag == '--api-key':
                options['api_key'] = value
            index += 2
            continue
        if flag == '--refresh':
            options['refresh'] = True
        elif not flag.startswith('--'):
            positionals.append(flag)
        index += 1
    if positionals and not options['keyword']:
        options['keyword'] = positionals[0]
    if options['reply_format'] not in {'json', 'markdown'}:
        print('--reply-format only supports json or markdown', file=sys.stderr)
        raise SystemExit(2)
    return options


def _model_search_payload(api_key: str, options: dict[str, Any]) -> dict[str, Any]:
    """Payload for searching works by model (used by gallery-search --model)."""
    found = search_works_by_model(
        api_key,
        model_query=options['model'],
        keyword=options['keyword'] or None,
        sort=options['sort'],
        page=options['page'],
        page_size=min(50, options['page_size']),
        limit=options['limit'],
        max_scan=options['max_scan'],
        refresh=options['refresh'],
    )
    payload: dict[str, Any] = {
        'scope': 'model-search',
        'query': {
            'model': options['model'],
            'keyword': options['keyword'] or None,
            'sort': resolve_sort(options['sort']),
            'page': options['page'],
            'page_size': min(50, options['page_size']),
            'max_scan': options['max_scan'],
            'refresh': options['refresh'],
        },
        'scanned': found['scanned'],
        'scan_pages': found['scan_pages'],
        'cache_hits': found.get('cache_hits', 0),
        'count': len(found['results']),
        'total': found['total'],
        'results': found['results'],
    }
    if not found['ok']:
        error = found['error_result'] or {}
        payload['error'] = 'REMOTE_REQUEST_FAILED'
        payload['message'] = api.request_error_message(error)
        payload['http_status'] = error.get('status')
    payload['reply_markdown'] = build_list_markdown(payload)
    return payload


def cmd_gallery_app_works(rest: list[str]) -> None:
    """Look up works by app/model version: hit /v1/creations/bizy_model_version/{version_id} directly, no per-work detail scan."""
    options: dict[str, Any] = {
        'target': None, 'page': 1, 'limit': 20, 'sort': None,
        'reply_format': 'markdown', 'api_key': None,
    }
    positionals: list[str] = []
    index = 0
    while index < len(rest):
        flag = rest[index]
        if flag in {'--page', '--limit', '--sort', '--reply-format', '--api-key'}:
            value = _take_value(rest, index, flag)
            if flag == '--page':
                options['page'] = normalize_page(value)
            elif flag == '--limit':
                options['limit'] = max(1, min(100, normalize_page_size(value, 20, 100)))
            elif flag == '--sort':
                options['sort'] = value
            elif flag == '--reply-format':
                options['reply_format'] = value
            elif flag == '--api-key':
                options['api_key'] = value
            index += 2
            continue
        if not flag.startswith('--'):
            positionals.append(flag)
        else:
            print(f'Unknown parameter: {flag}', file=sys.stderr)
            raise SystemExit(2)
        index += 1
    if options['reply_format'] not in {'json', 'markdown'}:
        print('--reply-format only supports json or markdown', file=sys.stderr)
        raise SystemExit(2)
    if not positionals:
        print('Missing app link or ID: gallery-app-works <community/app/ID | app ID | version ID>', file=sys.stderr)
        raise SystemExit(2)
    options['target'] = positionals[0]
    key = api.require_api_key(options['api_key'])
    resolved = resolve_app_version(key, options['target'])
    if not resolved.get('ok'):
        print(json.dumps(resolved, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(2)
    version_id = resolved['bizy_model_version_id']
    resolved_sort = options['sort'] or 'Recommend'
    result = list_works_by_app(key, version_id, page=options['page'], page_size=max(20, options['limit']),
                               sort=resolved_sort)
    page_data = app_works_page(result)
    results = [summarize_work(item) for item in page_data['list']][: options['limit']]
    payload: dict[str, Any] = {
        'scope': 'app-works',
        'app': {
            'input': options['target'],
            'bizy_model_version_id': version_id,
            'bizy_model_id': resolved.get('bizy_model_id'),
            'name': resolved.get('name'),
            'ref_bizy_model_id': resolved.get('ref_bizy_model_id'),
            'resolved_from': resolved.get('resolved_from'),
        },
        'query': {'page': options['page'], 'limit': options['limit'], 'sort': resolve_sort(resolved_sort)},
        'total': page_data['total'],
        'count': len(results),
        'results': results,
    }
    if not api.result_is_ok(result):
        payload['error'] = 'REMOTE_REQUEST_FAILED'
        payload['message'] = api.request_error_message(result)
        payload['http_status'] = result.get('status')
    if not results and not payload.get('error'):
        payload['note'] = 'The endpoint returned 0 works: this means the current version ID has no public works associated; it does not mean exporting the workflow failed, and says nothing about app quality.'
    payload['reply_markdown'] = build_list_markdown(payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2) if options['reply_format'] == 'json' else payload['reply_markdown'])
    if payload.get('error'):
        raise SystemExit(1)


def cmd_gallery(rest: list[str], *, mine: bool=False, favorites: bool=False) -> None:
    options = _parse_common_args(rest)
    resolved_sort = resolve_sort(options['sort'])
    if resolved_sort not in SUPPORTED_SORTS:
        print(
            json.dumps(
                {
                    'error': 'SORT_NOT_SUPPORTED',
                    'message': 'The work gallery currently supports only --sort Recently or --sort Recommend.',
                    'given': options['sort'],
                    'supported': sorted(SUPPORTED_SORTS),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)
    key = api.require_api_key(options['api_key'])
    keyword = options['keyword']

    if options['model'] and not mine and not favorites:
        payload = _model_search_payload(key, options)
        if options['reply_format'] == 'json':
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(payload['reply_markdown'])
        if payload.get('error'):
            raise SystemExit(1)
        return

    scope = 'mine' if mine else ('favorites' if favorites else ('search' if keyword else 'community'))

    result = list_works(
        key,
        keyword=keyword or None,
        sort=options['sort'],
        page=options['page'],
        page_size=options['page_size'],
        mine=mine,
        favorites=favorites,
    )
    page = extract_page(result) if api.result_is_ok(result) else {'list': [], 'total': None}
    # "My published" falls back to "me" when owner is missing; "my favorites" are other people's works, so they must not be labeled "me".
    owner_fallback = 'me' if mine else None
    results = summarize_works(page['list'], owner_fallback=owner_fallback)[: options['limit']]
    payload: dict[str, Any] = {
        'scope': scope,
        'query': {
            'keyword': keyword or None,
            'sort': resolve_sort(options['sort']),
            'page': options['page'],
            'page_size': options['page_size'],
        },
        'count': len(results),
        'total': page.get('total'),
        'results': results,
    }
    if not api.result_is_ok(result):
        payload['error'] = 'REMOTE_REQUEST_FAILED'
        payload['message'] = api.request_error_message(result)
        payload['http_status'] = result.get('status')
        payload['reply_markdown'] = build_list_markdown(payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1)

    payload['reply_markdown'] = build_list_markdown(payload)
    if options['reply_format'] == 'json':
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload['reply_markdown'])


def build_my_generated_markdown(payload: dict[str, Any]) -> str:
    results = payload.get('results') or []
    counts = payload.get('counts') or {}
    status_filter = (payload.get('query') or {}).get('status') or 'all'
    labels = {'all': 'all', 'published': 'published', 'unpublished': 'unpublished'}
    head = f"**My generated** · {labels.get(status_filter, status_filter)}"
    if counts.get('total') is not None:
        head += f" · {counts['total']} works"
        scanned = counts.get('scanned') or 0
        if scanned and scanned < int(counts['total']):
            head += f" (scanned {scanned}: {counts.get('published', 0)} published / {counts.get('unpublished', 0)} unpublished)"
        else:
            head += f" ({counts.get('published', 0)} published / {counts.get('unpublished', 0)} unpublished)"
    lines = [head, '']
    if payload.get('error'):
        lines.append(f"Query failed: {payload.get('message') or 'unknown error'}")
        return '\n'.join(lines)
    if not results:
        lines.append('No works under this filter. Try `--status all` or loosen `--type`.')
        return '\n'.join(lines)
    for index, item in enumerate(results, start=1):
        if item.get('status') == 'published':
            badge = '✅ published'
        elif item.get('status') == 'not_publishable':
            badge = '🚫 ModelZoo output (no work page)'
        else:
            badge = '⬜ unpublished'
        if item.get('expired'):
            badge += ' · expired'
        bits = [item.get('task_name') or '(untitled task)', item.get('kind') or 'file']
        if item.get('created_at'):
            bits.append(str(item['created_at'])[:10])
        lines.append(f"{index}. {badge} — {' · '.join(bits)}")
        if item.get('work_url'):
            lines.append(f"   Work page: {item['work_url']}")
        if index <= MAX_MARKDOWN_IMAGES and item.get('kind') == 'image' and item.get('url'):
            lines.append(f"   ![Output {item.get('output_id')}]({item['url']})")
        elif index <= MAX_MARKDOWN_IMAGES and item.get('kind') == 'video' and item.get('url'):
            lines.append(f"   {item['url']}")
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def cmd_my_generated(rest: list[str]) -> None:
    """The "my generated" list: distinguishes published/unpublished, both visible; publishing is not available."""
    if any(flag in rest for flag in ('--publish', '--output-id', '--confirm-publish')):
        reject_gallery_publish()
    options: dict[str, Any] = {
        'status': 'all', 'types': ['WEBAPP', 'API'], 'page': 1, 'page_size': 20,
        'limit': 20, 'reply_format': 'markdown', 'api_key': None,
    }
    index = 0
    while index < len(rest):
        flag = rest[index]
        if flag in {'--status', '--type', '--page', '--page-size', '--limit', '--reply-format', '--api-key'}:
            value = _take_value(rest, index, flag)
            if flag == '--status':
                options['status'] = value
            elif flag == '--type':
                options['types'] = [piece.strip().upper() for piece in value.split(',') if piece.strip()]
            elif flag == '--page':
                options['page'] = normalize_page(value)
            elif flag == '--page-size':
                options['page_size'] = normalize_page_size(value)
            elif flag == '--limit':
                options['limit'] = max(1, min(100, normalize_page_size(value, 20, 100)))
            elif flag == '--reply-format':
                options['reply_format'] = value
            elif flag == '--api-key':
                options['api_key'] = value
            index += 2
            continue
        index += 1
    if options['status'] not in {'all', 'published', 'unpublished'}:
        print('--status only supports all / published / unpublished', file=sys.stderr)
        raise SystemExit(2)
    unknown_types = [t for t in options['types'] if t not in {'WEBAPP', 'API', 'PLAYGROUND'}]
    if unknown_types:
        print(f'Unknown --type: {", ".join(unknown_types)} (supported: WEBAPP / API / PLAYGROUND)', file=sys.stderr)
        raise SystemExit(2)
    if options['reply_format'] not in {'json', 'markdown'}:
        print('--reply-format only supports json or markdown', file=sys.stderr)
        raise SystemExit(2)

    key = api.require_api_key(options['api_key'])
    user_id = current_user_id(key)
    if not user_id:
        print('Cannot get user_id: first use `cli.py profile` to confirm the API Key is valid', file=sys.stderr)
        raise SystemExit(1)

    published_map = fetch_published_output_map(key)
    collected: list[dict[str, Any]] = []
    total: Any = None
    page = options['page']
    scanned = 0
    published_count = 0
    unpublished_count = 0
    error_payload: dict[str, Any] | None = None
    # Status is determined locally, so keep paging until limit is reached or everything is scanned (at most 10 pages)
    while page < options['page'] + 10 and len(collected) < options['limit']:
        fetched = list_my_outputs(key, user_id=user_id, page=page, page_size=options['page_size'], types=options['types'])
        if not fetched['ok']:
            error_payload = fetched['error']
            break
        if total is None:
            total = fetched['total']
        outputs = fetched['outputs']
        if not outputs:
            break
        for item in outputs:
            scanned += 1
            summary = summarize_output(item, published_map)
            if summary['status'] == 'published':
                published_count += 1
            elif summary['status'] == 'unpublished':
                unpublished_count += 1
            if options['status'] != 'all' and summary['status'] != options['status']:
                continue
            collected.append(summary)
            if len(collected) >= options['limit']:
                break
        if fetched['total'] is not None and scanned >= int(fetched['total'] or 0):
            break
        page += 1

    payload: dict[str, Any] = {
        'scope': 'my-generated',
        'query': {
            'status': options['status'],
            'types': options['types'],
            'page': options['page'],
            'page_size': options['page_size'],
        },
        'counts': {
            'total': total,
            'scanned': scanned,
            'published': published_count,
            'unpublished': unpublished_count,
        },
        'count': len(collected),
        'results': collected,
    }
    if error_payload is not None:
        payload['error'] = 'REMOTE_REQUEST_FAILED'
        payload['message'] = api.request_error_message(error_payload)
        payload['http_status'] = error_payload.get('status')
    payload['reply_markdown'] = build_my_generated_markdown(payload)
    if options['reply_format'] == 'json':
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload['reply_markdown'])
    if error_payload is not None:
        raise SystemExit(1)


def cmd_gallery_detail(rest: list[str]) -> None:
    args = [r for r in rest if not r.startswith('--')]
    if not args:
        print('Usage: cli.py gallery-detail <work ID or link> [--api-key KEY] [--reply-format json]', file=sys.stderr)
        raise SystemExit(2)
    options = _parse_common_args(rest)
    key = api.require_api_key(options['api_key'])
    work_id = api.extract_remote_target_id(args[0]) or args[0]

    result = work_detail(key, work_id)
    payload: dict[str, Any] = {'work_id': work_id}
    if not api.result_is_ok(result):
        payload['error'] = 'REMOTE_REQUEST_FAILED'
        payload['message'] = api.request_error_message(result)
        payload['http_status'] = result.get('status')
        payload['reply_markdown'] = build_detail_markdown(payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1)

    data = api.extract_result_data(result) or {}
    payload['work'] = summarize_work(data)
    payload['reply_markdown'] = build_detail_markdown(payload)
    if options['reply_format'] == 'json':
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload['reply_markdown'])


def _single_work_command(rest: list[str], *, action: str) -> None:
    args = [r for r in rest if not r.startswith('--')]
    if not args:
        print(f'Usage: cli.py {action} <work ID or link> [--api-key KEY] [--reply-format json]', file=sys.stderr)
        raise SystemExit(2)
    options = _parse_common_args(rest)
    key = api.require_api_key(options['api_key'])
    work_id = api.extract_remote_target_id(args[0]) or args[0]

    if action == 'gallery-like':
        result = like_work(key, work_id)
        verb = 'like'
    elif action == 'gallery-favorite':
        result = fork_work(key, work_id)
        verb = 'favorite'
    elif action == 'gallery-unfavorite':
        result = unfork_work(key, work_id)
        verb = 'unfavorite'
    else:
        raise SystemExit(2)

    state = None
    payload: dict[str, Any] = {
        'action': action,
        'work_id': work_id,
        'verb': verb,
        'ok': api.result_is_ok(result),
        'state': state,
    }
    if not api.result_is_ok(result):
        payload['error'] = 'REMOTE_REQUEST_FAILED'
        message = api.request_error_message(result)
        if isinstance(message, str) and 'own creation' in message.lower():
            message = 'This is your own work, and the platform does not allow favoriting (forking) your own works; please pick someone else\'s work.'
        payload['message'] = message
        payload['http_status'] = result.get('status')

    if payload['ok']:
        payload['reply_markdown'] = f'{verb} succeeded: work {work_id}.'
        if action == 'gallery-like':
            payload['reply_markdown'] += '\n\nNote: the like endpoint is additive, so liking repeatedly keeps raising the count; there is currently no available endpoint to unlike.'
        elif action == 'gallery-favorite':
            payload['reply_markdown'] += '\n\nThe platform "favorite" forks a copy; see it with `my-favorites`. Unfavoriting deletes that fork copy and leaves the original work untouched.'
        elif action == 'gallery-unfavorite':
            payload['reply_markdown'] += '\n\nThe platform "unfavorite" deletes the copy forked when you favorited it (the original work is untouched).'
    else:
        payload['reply_markdown'] = f"{verb} failed: {payload.get('message')}"

    if options['reply_format'] == 'json':
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload['reply_markdown'])
    if not payload['ok']:
        raise SystemExit(1)


def cmd_gallery_like(rest: list[str]) -> None:
    _single_work_command(rest, action='gallery-like')


def cmd_gallery_favorite(rest: list[str]) -> None:
    _single_work_command(rest, action='gallery-favorite')


def cmd_gallery_unfavorite(rest: list[str]) -> None:
    _single_work_command(rest, action='gallery-unfavorite')
