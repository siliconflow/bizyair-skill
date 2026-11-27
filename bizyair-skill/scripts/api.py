"""api.py — BizyAir HTTP client + task execution pipeline.

Responsibilities:
  - API key resolution and lookup (CLI / env / config.json three-level fallback, see resolve_api_key)
  - Remote HTTP calls: bizy_models list / single model detail / webapp_detail / version_detail / workflow
  - Task execution pipeline: upload_input_file → build_remote_run_payload → create_remote_task_attempt
                → poll_until_done → download_outputs
  - Guidance shown to the user on NO_API_KEY errors (the quick test command in require_api_key)

build_remote_run_payload builds the execution payload for a remote AI app:
  it walks webapp input_nodes, binds media slots to uploaded files, routes standard parameters
  (duration/resolution, etc.) through override, randomizes seed automatically, writes --prompt
  into every customtext-type non-negative text field, and lets --param key=value override any
  field exactly (highest priority).
"""
from __future__ import annotations
import argparse, base64, hashlib, hmac, http.client, json, mimetypes, os, random, re, sys, time, urllib.parse, urllib.request
from email.utils import formatdate
from pathlib import Path
from paths import load_config_json, resolve_output_root
from typing import Any

import contract
import remote
import search
import common
import oss_multipart
from common import (
    BIZY_MODEL_DETAIL_URL, CREATE_URL, DEFAULT_TIMEOUT, DETAIL_URL,
    INPUT_COMMIT_URL, MAX_POLL_SECONDS, OUTPUTS_URL, POLL_INTERVAL,
    RESOLVE_VERSION_URL, UPLOAD_TOKEN_URL, WEBAPP_DETAIL_URL_TEMPLATE,
)

_PLACEHOLDER_API_KEYS = {
    'your_api_key_here',
    'your_bizyair_api_key_here',
    'your_api_key',
    'your_bizyair_key',
    'your_bizyair_api_key',
    '<your_api_key>',
    '<your_bizyair_api_key>',
    'bizyair_api_key',
    'replace_me',
}

def is_placeholder_api_key(value: str | None) -> bool:
    """Whether the config still holds the install-template placeholder instead of a real user key.

    Real keys usually start with sk-; only explicit template text is caught here, so a normal
    key is never misjudged.
    """
    normalized = str(value or '').strip()
    if not normalized:
        return True
    plain = normalized.strip('<>').strip()
    lowered = plain.lower()
    if lowered in _PLACEHOLDER_API_KEYS:
        return True
    token = re.sub(r'[\s_-]+', '_', lowered)
    return token.startswith('your_') and 'key' in token

def normalize_model_types(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Normalize model_types into a list of repeatable parameters.

    The server requires each type to appear on its own:
      model_types=Application&model_types=Workflow
    A comma-joined `Application,Workflow` is treated as one non-existent type and returns 0 rows.
    String / comma-string arguments from older callers are supported here as well.
    """
    if not value:
        return []
    if isinstance(value, str):
        raw = value.split(',')
    else:
        raw = list(value)
    result: list[str] = []
    for item in raw:
        text = str(item or '').strip()
        if text and text not in result:
            result.append(text)
    return result

def fetch_remote_models(api_key: str, *, remote_source: str='community', page: int=1, page_size: int=10, keyword: str | None=None, sort: str | None=None, model_types: str | list[str] | tuple[str, ...] | None=None, base_models: str | None=None) -> dict[str, Any]:
    if not common.CATALOG_SEARCH_SUPPORTED:
        return {'ok': False, 'status': None, 'error': {'code': None, 'message': f'Community/official list search for {common.SITE_LABEL} has been disabled by configuration.'}}
    # The list endpoint is public: an invalid key would only cause a 401, so public requests are used here.
    # The api_key parameter is kept for compatibility with existing callers; auth is only needed for real runs.
    resolved_sort = sort or ('Auto' if common.normalized_text(remote_source) == 'official' else 'Most Used')
    params: list[tuple[str, Any]] = [('current', page), ('page_size', page_size), ('sort', resolved_sort)]
    if keyword is not None:
        params.append(('keyword', keyword))
    for model_type in normalize_model_types(model_types):
        params.append(('model_types', model_type))
    if base_models:
        params.append(('base_models', base_models))
    if common.normalized_text(remote_source) == 'official':
        params.append(('mode', 'official'))
    return safe_request_json_public('GET', remote.remote_list_endpoint(remote_source), params=params)

def find_remote_model_by_id(api_key: str, bizy_model_id: str | int, *, remote_source: str='community') -> dict[str, Any] | None:
    """Find an object by ID in the community/official lists. First search one page (100 rows) with no type filter; if that fails, search one page per type."""
    target = str(bizy_model_id)
    sort = 'Auto' if common.normalized_text(remote_source) == 'official' else 'Most Used'
    # Round one: no type filter, widest coverage
    resp = fetch_remote_models(api_key, remote_source=remote_source, page=1, page_size=100, keyword='', sort=sort, model_types=None)
    items = extract_result_data(resp).get('list', [])
    for item in items:
        if str(item.get('id')) == target:
            return item
    # Round two: one page per type (some types may fall outside the default top 100)
    for model_type in ['Application', 'Workflow']:
        resp = fetch_remote_models(api_key, remote_source=remote_source, page=1, page_size=100, keyword='', sort=sort, model_types=model_type)
        items = extract_result_data(resp).get('list', [])
        for item in items:
            if str(item.get('id')) == target:
                return item
    return None

def fetch_remote_detail(api_key: str, bizy_model_id: str | int) -> dict[str, Any]:
    return safe_request_json('GET', f'{BIZY_MODEL_DETAIL_URL}/{bizy_model_id}/detail', api_key)

def safe_request_json_public(method: str, url: str, *, params: dict[str, Any] | list[tuple[str, Any]] | None=None, payload: dict[str, Any] | None=None, headers: dict[str, str] | None=None, timeout: int=DEFAULT_TIMEOUT) -> dict[str, Any]:
    url = common.apply_host_override(url)
    if params:
        # doseq=True expands repeatable parameters such as model_types into
        # model_types=Application&model_types=Workflow instead of comma-joining them.
        url = url + '?' + urllib.parse.urlencode(params, doseq=True)
    body = None
    req_headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'User-Agent': 'BizyAir-Skill/1.0', 'lang': common.request_lang()}
    if headers:
        req_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=body, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8')
            if not raw.strip():
                return {}
            return {'ok': True, 'status': resp.status, 'data': json.loads(raw)}
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')
        try:
            parsed = json.loads(detail)
        except Exception:
            parsed = {'message': detail}
        return {'ok': False, 'status': e.code, 'error': parsed}
    except Exception as e:
        return {'ok': False, 'status': None, 'error': {'message': str(e)}}

def unwrap_result_payload(result: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    if 'ok' in result:
        payload = result.get('data')
        if isinstance(payload, dict):
            result = payload
        else:
            return {}
    return result if isinstance(result, dict) else {}

def extract_result_data(result: dict[str, Any] | None) -> dict[str, Any]:
    payload = unwrap_result_payload(result)
    if isinstance(payload.get('data'), dict):
        return payload.get('data', {})
    return payload

def result_is_ok(result: dict[str, Any] | None) -> bool:
    if not isinstance(result, dict):
        return False
    if 'ok' in result:
        return bool(result.get('ok'))
    status = result.get('status')
    if isinstance(status, bool):
        return status
    return True

def request_error_message(result: dict[str, Any] | None) -> str:
    """Extract one readable error from the safe_request_json failure envelope."""
    if not isinstance(result, dict):
        return 'request failed'
    error = result.get('error')
    if isinstance(error, dict):
        message = error.get('message') or error.get('detail')
        if message:
            return str(message)
        if error:
            return json.dumps(error, ensure_ascii=False)
    elif error:
        return str(error)
    status = result.get('status')
    return f'HTTP {status}' if status else 'request failed'

def auth_error_status(result: dict[str, Any] | None) -> int | None:
    """Return 401/403 when a request failed because the API key is invalid or expired."""
    if not isinstance(result, dict):
        return None
    status = result.get('status')
    if status in (401, 403):
        return int(status)
    return None

def auth_error_from_raw(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Find the first 401/403 recorded in a remote-info raw debug branch."""
    if not isinstance(raw, dict):
        return None
    for key in ('detail_error', 'webapp_detail_error'):
        error = raw.get(key)
        if isinstance(error, dict) and error.get('status') in (401, 403):
            return {'status': int(error.get('status')), 'source': key, 'message': request_error_message({'error': error.get('error')})}
    return None

def finish_remote_result(result: dict[str, Any] | None, payload: dict[str, Any], *, action: str, emit: bool=True) -> dict[str, Any]:
    """Finish a public-list command consistently.

    Success: print the caller's payload as JSON (unless emit=False) and return it.
    Failure: add error/http_status/message to the payload, print it to stderr, and exit 1.
    This keeps `browse` / `search` / picker commands from reporting remote failures as exit 0.
    """
    if result_is_ok(result):
        if emit:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload
    failed = dict(payload)
    failed.update({
        'error': 'REMOTE_REQUEST_FAILED',
        'action': action,
        'http_status': result.get('status') if isinstance(result, dict) else None,
        'message': request_error_message(result),
    })
    print(json.dumps(failed, ensure_ascii=False, indent=2), file=sys.stderr)
    sys.exit(1)

def require_ok(result: dict[str, Any] | None, action: str) -> dict[str, Any]:
    """For CLI entry points: on failure print a structured error and exit 1; on success return the result unchanged.

    Library functions still return the envelope; only the outermost command calls this function, so
    errors are not silently swallowed into an empty result by `(result.get('data') or {}).get('data') or {}`.
    """
    if result_is_ok(result):
        return result or {}
    payload = {
        'error': 'REMOTE_REQUEST_FAILED',
        'action': action,
        'http_status': result.get('status') if isinstance(result, dict) else None,
        'message': request_error_message(result),
    }
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)

def fetch_remote_webapp_detail(api_key: str, webapp_id: str | int) -> dict[str, Any]:
    # International-site webapp metadata goes through the meta host and needs an auth header.
    url = WEBAPP_DETAIL_URL_TEMPLATE.format(webapp_id=webapp_id)
    return request_json('GET', url, api_key)

def fetch_remote_version_detail(api_key: str, version_id: str | int) -> dict[str, Any]:
    return request_json('GET', f'{BIZY_MODEL_DETAIL_URL}/versions/{version_id}', api_key)

def fetch_remote_workflow(api_key: str, version_id: str | int) -> dict[str, Any]:
    return request_json('GET', f'{RESOLVE_VERSION_URL}/{version_id}', api_key)

_WORKFLOW_URL_ALLOWED_HOSTS = common.WORKFLOW_URL_ALLOWED_HOSTS

def fetch_remote_workflow_from_url(url: str | None) -> dict[str, Any]:
    target = str(url or '').strip()
    if not target:
        return {}
    parsed = urllib.parse.urlparse(target)
    host = (parsed.hostname or '').lower()
    if not any(host == allowed or host.endswith('.' + allowed) for allowed in _WORKFLOW_URL_ALLOWED_HOSTS):
        return {'ok': False, 'error': f'blocked_host: {host}'}
    req = urllib.request.Request(target, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            raw = resp.read().decode('utf-8')
            if not raw.strip():
                return {}
            return {'ok': True, 'status': resp.status, 'data': json.loads(raw)}
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}

def safe_request_json(method: str, url: str, api_key: str, *, params: dict[str, Any] | None=None, payload: dict[str, Any] | None=None, headers: dict[str, str] | None=None, timeout: int=DEFAULT_TIMEOUT) -> dict[str, Any]:
    url = common.apply_host_override(url)
    if params:
        url = url + '?' + urllib.parse.urlencode(params)
    body = None
    req_headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json', 'lang': common.request_lang(), 'User-Agent': 'BizyAir-Skill/1.0'}
    if headers:
        req_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=body, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8')
            if not raw.strip():
                return {}
            return {'ok': True, 'status': resp.status, 'data': json.loads(raw)}
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')
        try:
            parsed = json.loads(detail)
        except Exception:
            parsed = {'message': detail}
        return {'ok': False, 'status': e.code, 'error': parsed}
    except Exception as e:
        return {'ok': False, 'status': None, 'error': {'message': str(e)}}

def extract_remote_target_id(raw: str | None) -> str | None:
    """Normalize a user-provided "plain numeric ID / community or app page link" into an object ID; return None if unrecognized.

    Accepted forms (bizyair.ai international site):
      57862
      https://www.bizyair.ai/community/app/57862
      https://www.bizyair.ai/.../webapp/57862?...
      ...?app_id=57862 / ?id=57862 / ?webapp_id=57862
      webapp_57862 / app-57862
    Never guess a trailing segment beyond short numbers: the fallback only accepts a purely
    numeric trailing segment of length >= 3.
    """
    text = str(raw or '').strip()
    if not text:
        return None
    if text.isdigit():
        return text
    match = re.fullmatch(r'(?:app|webapp|application|workflow)[_-]?(\d+)', text, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r'[?&](?:id|app_id|webapp_id|bizy_model_id|model_id)=(\d+)', text, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r'/(?:app|webapp|application|workflow)/(\d+)(?=[/?#]|$)', text, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r'/(\d{3,})(?=[/?#]|$)', text)
    if match:
        return match.group(1)
    return None

def diagnose_remote_numeric_id(api_key: str | None, raw_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {'input_id': str(raw_id), 'is_numeric': str(raw_id).isdigit(), 'matched_remote_bizy_model': None, 'matched_webapp': None, 'matched_local_web_app': None, 'suspected_id_kind': None, 'guidance': []}
    if not str(raw_id).isdigit():
        result['suspected_id_kind'] = 'non_numeric'
        result['guidance'] = ['I checked and the ID you gave is not purely numeric, so the system cannot pinpoint it directly.', 'The safest way is to send me the full page link, or tell me a bit more about what it does, and I will look it up for you~']
        return result
    if api_key:
        webapp = fetch_remote_webapp_detail(api_key, str(raw_id))
        webapp_data = extract_result_data(webapp)
        if webapp_data.get('id'):
            result['matched_webapp'] = {
                'id': webapp_data.get('id'),
                'name': webapp_data.get('name'),
                'base_model': webapp_data.get('base_model'),
                'ref_bizy_model_id': webapp_data.get('ref_bizy_model_id'),
            }
            result['suspected_id_kind'] = 'webapp_version_id'
            result['guidance'] = ['This numeric ID is the version ID of an app page (webapp version) and can be used directly for info / prefill / run.', 'It may collide with some model asset ID in the model hub; use model-detail to look up model hub objects.']
            model_detail = fetch_remote_detail(api_key, str(raw_id))
            model_data = extract_result_data(model_detail)
            if model_data.get('id'):
                result['id_collision'] = {'requested_id': str(raw_id), 'model': {'id': model_data.get('id'), 'name': model_data.get('name'), 'type': model_data.get('type')}, 'webapp': result['matched_webapp'], 'resolution': 'webapp_app_version'}
        community = find_remote_model_by_id(api_key, str(raw_id), remote_source='community')
        official = None if community else find_remote_model_by_id(api_key, str(raw_id), remote_source='official')
        matched = community or official
        if matched:
            result['matched_remote_bizy_model'] = search.summarize_remote_candidate(matched)
            if result['suspected_id_kind'] == 'webapp_version_id':
                result['id_collision'] = {'requested_id': str(raw_id), 'model': {'id': matched.get('id'), 'name': matched.get('name'), 'type': matched.get('type')}, 'webapp': result['matched_webapp'], 'resolution': 'webapp_app_version'}
            else:
                result['suspected_id_kind'] = 'remote_bizy_model_id'
                result['guidance'] = ['Matched it~ this numeric ID exists in the catalog, so I will follow the thread and fetch its details. One moment~']
            return result
    if result['suspected_id_kind'] == 'webapp_version_id':
        return result
    result['suspected_id_kind'] = 'unknown_numeric_id_or_web_app_id'
    result['guidance'] = ['Hmm, this numeric ID has no match in the public catalog.', 'To be on the safe side, I will not run blind.', 'Check whether you have the full link and send it over, so we can try again!']
    return result

def read_bizyair_entry_from_skill_config() -> dict[str, Any]:
    cfg = load_config_json()
    entry = cfg.get('credentials', {})
    return entry if isinstance(entry, dict) else {}

def read_key_from_skill_config() -> str | None:
    entry = read_bizyair_entry_from_skill_config()
    api_key = entry.get('api_key')
    if isinstance(api_key, str) and api_key.strip() and (not is_placeholder_api_key(api_key)):
        return api_key.strip()
    # Multi-key mode: take the first non-placeholder key
    api_keys = entry.get('api_keys')
    if isinstance(api_keys, list):
        for item in api_keys:
            if isinstance(item, str) and item.strip() and (not is_placeholder_api_key(item)):
                return item.strip()
    return None

def _load_retryable_codes() -> tuple[set[int], set[int]]:
    """Load retryable error codes from config/error_codes.json."""
    config_dir = Path(__file__).resolve().parent.parent / 'config'
    ec_file = config_dir / 'error_codes.json'
    if ec_file.exists():
        data = json.loads(ec_file.read_text(encoding='utf-8'))
        biz = {int(k) for k in (data.get('retryable_biz_codes') or {}).keys()}
        http = set(data.get('retryable_http_codes') or [])
        return biz, http
    # fallback
    return {20049, 20050, 20051, 30039, 30040, 30015, 30016, 30018, 50600, 50601, 50602, 50603, 50604}, {429, 402}

_RETRYABLE_BIZ_CODES, _RETRYABLE_HTTP_CODES = _load_retryable_codes()

def resolve_api_key(provided: str | None) -> str | None:
    if provided:
        normalized = provided.strip()
        if normalized and (not is_placeholder_api_key(normalized)):
            return normalized
    env = os.environ.get('BIZYAIR_API_KEY', '').strip()
    if env and (not is_placeholder_api_key(env)):
        return env
    return read_key_from_skill_config()

def get_key_source(provided: str | None) -> str:
    if provided:
        normalized = provided.strip()
        if normalized and (not is_placeholder_api_key(normalized)):
            return 'cli'
    env = os.environ.get('BIZYAIR_API_KEY', '').strip()
    if env and (not is_placeholder_api_key(env)):
        return 'env'
    if read_key_from_skill_config():
        return 'skill_config'
    return 'none'

def require_api_key(provided: str | None) -> str:
    key = resolve_api_key(provided)
    if key:
        return key
    print(json.dumps({'error': 'NO_API_KEY', 'message': 'No BizyAir API key configured', 'supported_inputs': {'cli': '--api-key YOUR_BIZYAIR_KEY', 'env': 'BIZYAIR_API_KEY', 'skill_config': f'{common.config_display_path()} -> credentials.api_key'}, 'recommended': f'Save the key to {common.config_display_path()} at credentials.api_key for reusable local skill runs', 'steps': ['1. Get BizyAir API key', f"2. Quick test: {common.local_script_command('dispatch.py', '--check', '--api-key', 'YOUR_BIZYAIR_KEY')}", f'3. Recommended: save it to {common.config_display_path()} under credentials.api_key', '4. Or set env BIZYAIR_API_KEY for the current shell/session']}, ensure_ascii=False))
    sys.exit(1)

def request_json(method: str, url: str, api_key: str, *, params: dict[str, Any] | None=None, payload: dict[str, Any] | None=None, headers: dict[str, str] | None=None, timeout: int=DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Send an authenticated request to BizyAir and return the same envelope as safe_request_json.

    On success returns {ok: True, status: 200, data: {...}};
    on failure returns {ok: False, status: ..., error: ...}.
    Every caller unwraps business data through extract_result_data().
    """
    return safe_request_json(method, url, api_key, params=params, payload=payload, headers=headers, timeout=timeout)

def generate_seed() -> int:
    return random.randint(1, 2 ** 50)

def get_upload_token(api_key: str, file_name: str) -> dict[str, Any]:
    return request_json('GET', UPLOAD_TOKEN_URL, api_key, params={'file_name': file_name, 'file_type': 'inputs'})

def upload_to_oss(file_path: str, token_inner: dict[str, Any]) -> None:
    """Upload a file to OSS. token_inner is the inner data after extract_result_data(token_resp)."""
    file_info = token_inner['file']
    storage = token_inner['storage']
    object_key = file_info['object_key']
    bucket = storage['bucket']
    endpoint = storage['endpoint']
    access_key_id = file_info['access_key_id']
    access_key_secret = file_info['access_key_secret']
    security_token = file_info['security_token']
    file_bytes = Path(file_path).read_bytes()
    content_type = mimetypes.guess_type(file_path)[0] or 'application/octet-stream'
    date_str = formatdate(usegmt=True)
    canonical_headers = f'x-oss-security-token:{security_token}\n'
    canonical_resource = f'/{bucket}/{object_key}'
    string_to_sign = f'PUT\n\n{content_type}\n{date_str}\n{canonical_headers}{canonical_resource}'
    signature = base64.b64encode(hmac.new(access_key_secret.encode('utf-8'), string_to_sign.encode('utf-8'), hashlib.sha1).digest()).decode('utf-8')
    upload_url = f'https://{bucket}.{endpoint}/{object_key}'
    req = urllib.request.Request(upload_url, data=file_bytes, method='PUT')
    req.add_header('Date', date_str)
    req.add_header('Content-Type', content_type)
    req.add_header('Content-Length', str(len(file_bytes)))
    req.add_header('x-oss-security-token', security_token)
    req.add_header('Authorization', f'OSS {access_key_id}:{signature}')
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            if resp.status not in {200, 201}:
                raise RuntimeError(f'Unexpected upload status: {resp.status}')
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')
        print(json.dumps({'error': 'UPLOAD_FAILED', 'status': e.code, 'message': detail}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(json.dumps({'error': 'UPLOAD_FAILED', 'message': str(e)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

def commit_input(api_key: str, file_name: str, object_key: str) -> str:
    resp = request_json('POST', INPUT_COMMIT_URL, api_key, payload={'name': file_name, 'object_key': object_key})
    data = extract_result_data(resp)
    url = data.get('url')
    if not url:
        print(json.dumps({'error': 'UPLOAD_FAILED', 'message': 'No input url returned after commit'}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    return url

BASE64_IMAGE_MAX_BYTES = 1_500_000


def image_data_url(file_path: str) -> str:
    """Local image → data:image/...;base64 URL (ModelZoo accepts it directly, saving one OSS round trip)."""
    data = Path(file_path).read_bytes()
    suffix = Path(file_path).suffix.lower().lstrip('.')
    mime = {'jpg': 'jpeg', 'jpeg': 'jpeg', 'png': 'png', 'webp': 'webp', 'bmp': 'bmp', 'gif': 'gif'}.get(suffix, 'jpeg')
    return f'data:image/{mime};base64,' + base64.b64encode(data).decode('ascii')


def media_input_value(api_key: str, file_path: str, *, mode: str='auto', kind: str='image') -> str:
    """Resolve a media input value: small image files default to base64, everything else (or large files) goes through OSS.

    mode: auto | base64 | oss
    """
    path = Path(file_path)
    if not path.exists():
        print(json.dumps({'error': 'FILE_NOT_FOUND', 'message': f'Local upload file does not exist: {file_path}'}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
    if kind == 'image' and mode in {'auto', 'base64'}:
        if mode == 'base64' or path.stat().st_size <= BASE64_IMAGE_MAX_BYTES:
            return image_data_url(file_path)
    return upload_input_file(api_key, file_path)


def upload_input_file(api_key: str, file_path: str) -> str:
    """Upload a file to OSS and return the real HTTP URL. Every file goes through direct OSS upload + commit."""
    p = Path(file_path)
    if not p.exists():
        print(json.dumps({'error': 'FILE_NOT_FOUND', 'message': f'Local upload file does not exist: {file_path}'}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
    token_resp = get_upload_token(api_key, p.name)
    if not result_is_ok(token_resp):
        print(json.dumps({'error': 'UPLOAD_FAILED', 'message': 'Failed to get upload token', 'detail': token_resp.get('error') if isinstance(token_resp, dict) else str(token_resp)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    token_inner = extract_result_data(token_resp)
    upload_to_oss(str(p), token_inner)
    return commit_input(api_key, p.name, token_inner['file']['object_key'])

# ---------------------------------------------------------------------------
# Model publishing (v1.10)
#
# The sign is not "the file's sha256". The official bizyair-cli (lib/filehash/hash.go) computes:
#   md5_b64 = base64(md5(file))
#   crc64   = Go hash/crc64 ECMA (start and end bit-inverted) of file
#   sign    = sha256(md5_b64 + str(crc64))
# The server's POST /v1/files validates this set of values; a mismatched algorithm fails with a
# 40021 file signature error.
# ---------------------------------------------------------------------------

_CRC64_ECMA_POLY = 0xC96C5795D7870F42


def _crc64_tables() -> tuple[list[int], ...]:
    """Slice-by-8 lookup tables for reflected CRC64/ECMA; built only once."""
    base: list[int] = []
    for index in range(256):
        value = index
        for _ in range(8):
            value = (value >> 1) ^ (_CRC64_ECMA_POLY if value & 1 else 0)
        base.append(value)
    tables = [base]
    for _ in range(7):
        previous = tables[-1]
        tables.append([base[previous[index] & 0xFF] ^ (previous[index] >> 8) for index in range(256)])
    return tuple(tables)


_CRC64_TABLES = _crc64_tables()


def crc64_ecma(data: bytes) -> int:
    """Go hash/crc64 ECMA (initial value and result inverted), slice-by-8 accelerated."""
    crc = 0xFFFFFFFFFFFFFFFF
    length = len(data)
    end = length - (length % 8)
    tables = _CRC64_TABLES
    index = 0
    while index < end:
        crc ^= int.from_bytes(data[index:index + 8], 'little')
        crc = (tables[7][crc & 0xFF] ^ tables[6][(crc >> 8) & 0xFF]
               ^ tables[5][(crc >> 16) & 0xFF] ^ tables[4][(crc >> 24) & 0xFF]
               ^ tables[3][(crc >> 32) & 0xFF] ^ tables[2][(crc >> 40) & 0xFF]
               ^ tables[1][(crc >> 48) & 0xFF] ^ tables[0][(crc >> 56) & 0xFF])
        index += 8
    table = tables[0]
    for byte in data[end:]:
        crc = table[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFFFFFFFFFF


def _crc64_update(crc: int, data: bytes) -> int:
    """Append a chunk of data onto an existing CRC state (slice-by-8)."""
    length = len(data)
    end = length - (length % 8)
    tables = _CRC64_TABLES
    index = 0
    while index < end:
        crc ^= int.from_bytes(data[index:index + 8], 'little')
        crc = (tables[7][crc & 0xFF] ^ tables[6][(crc >> 8) & 0xFF]
               ^ tables[5][(crc >> 16) & 0xFF] ^ tables[4][(crc >> 24) & 0xFF]
               ^ tables[3][(crc >> 32) & 0xFF] ^ tables[2][(crc >> 40) & 0xFF]
               ^ tables[1][(crc >> 48) & 0xFF] ^ tables[0][(crc >> 56) & 0xFF])
        index += 8
    table = tables[0]
    for byte in data[end:]:
        crc = table[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc


def model_file_signature(file_path: str, *, chunk_size: int = 4 * 1024 * 1024,
                         progress: Any = None, progress_interval: int = 64 * 1024 * 1024) -> dict[str, Any]:
    """Return {'sign', 'md5_b64', 'size'}; the algorithm matches the official bizyair-cli.

    Streamed reading, so model files of hundreds of MB / a few GB never land in memory at once.
    progress(done, total) reports hashing progress for large files; the default callback fires
    every 64 MB.
    """
    md5 = hashlib.md5()
    crc = 0xFFFFFFFFFFFFFFFF
    size = 0
    total = Path(file_path).stat().st_size
    reported = 0
    with open(file_path, 'rb') as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            md5.update(chunk)
            crc = _crc64_update(crc, chunk)
            if progress and size - reported >= progress_interval:
                reported = size
                progress(size, total)
    crc ^= 0xFFFFFFFFFFFFFFFF
    md5_b64 = base64.b64encode(md5.digest()).decode('ascii')
    sign = hashlib.sha256(f'{md5_b64}{crc}'.encode('ascii')).hexdigest()
    if progress:
        progress(size, total)
    return {'sign': sign, 'md5_b64': md5_b64, 'size': size}


def upload_model_file_oss(file_path: str, token_inner: dict[str, Any], *, chunk_size: int = 8 * 1024 * 1024,
                          token_refresh: Any = None, concurrency: int = 6, progress: Any = None,
                          restart: bool = False, checkpoint_name: str | None = None) -> dict[str, Any]:
    """Upload a model file to OSS in chunks (STS temporary credentials + OSS v1 signature), resumable.

    token_inner is the {'file','storage'} payload unwrapped by fetch_model_file_token.
    token_refresh: no-arg callback returning a new token_inner (refresh when STS expires).
    progress: progress(done, total); checkpoint_name: stable key for the checkpoint file (usually
    the file signature).
    """
    return oss_multipart.upload_model_file(
        file_path, token_inner,
        token_refresh=token_refresh,
        concurrency=concurrency,
        progress=progress,
        restart=restart,
        checkpoint_name=checkpoint_name,
    )


def clear_model_upload_checkpoint(checkpoint_path: str) -> None:
    """Clear the local checkpoint once the file is registered; on failure it may be kept, and the next attempt only retries registration without re-uploading."""
    oss_multipart.delete_checkpoint(Path(checkpoint_path))


def fetch_model_file_token(api_key: str, sign: str, model_type: str) -> dict[str, Any]:
    """GET /v1/files/{sign}?type=<ModelType>.

    Returns inner data {'file': {...}, 'storage': {...}};
    an existing file.id means the file is already registered server-side, so upload can be skipped.
    """
    url = common.MODEL_FILE_SIGN_URL.format(sign=sign)
    return safe_request_json('GET', url, api_key, params={'type': model_type})


def commit_model_file(api_key: str, *, sign: str, object_key: str, md5_b64: str, model_type: str) -> dict[str, Any]:
    return safe_request_json('POST', common.MODEL_FILE_COMMIT_URL, api_key, payload={
        'sign': sign,
        'object_key': object_key,
        'md5_hash': md5_b64,
        'type': model_type,
    })


def create_model(api_key: str, *, name: str, model_type: str, versions: list[dict[str, Any]]) -> dict[str, Any]:
    return safe_request_json('POST', common.MODEL_CREATE_URL, api_key, payload={
        'name': name,
        'type': model_type,
        'versions': versions,
    })


def model_exists(api_key: str, name: str, model_type: str) -> bool | None:
    """GET /v1/bizy_models/exists: 200 + data=null when a model with the same name and type already exists;
    when absent, 404 + code 20224 ("LoRA not found"). Other failures return None (unknown)."""
    resp = safe_request_json('GET', common.MODEL_EXISTS_URL, api_key, params={'name': name, 'type': model_type})
    if result_is_ok(resp):
        return True
    error = resp.get('error') if isinstance(resp, dict) else None
    code = (error or {}).get('code')
    if code == 20224 or resp.get('status') == 404:
        return False
    return None


def fetch_base_models(api_key: str) -> list[str]:
    """base_models from GET /v1/dict (the value is the string to pass to --base-model)."""
    resp = safe_request_json('GET', common.BASE_MODEL_DICT_URL, api_key)
    data = extract_result_data(resp)
    if not isinstance(data, dict):
        return []
    models = []
    for item in data.get('base_models') or []:
        value = item.get('value') if isinstance(item, dict) else item
        text = str(value or '').strip()
        if text and text not in models:
            models.append(text)
    return models

def coerce_value(value: str) -> Any:
    low = value.lower()
    if low == 'true':
        return True
    if low == 'false':
        return False
    try:
        if '.' in value:
            return float(value)
        return int(value)
    except ValueError:
        return value

def remote_param_key_candidates(preflight: dict[str, Any]) -> tuple[dict[str, set[str]], set[str]]:
    """Return (alias -> set of variable_name, all variable_names).

    When the same field_name repeats (e.g. four a's), one alias maps to several variable_names;
    callers must reject such ambiguous keys instead of silently overwriting with the last one.
    """
    execution_target = preflight.get('execution_target') or {}
    webapp_detail_data = extract_result_data(execution_target.get('webapp_detail') or preflight.get('webapp_detail')) or {}
    alias_candidates: dict[str, set[str]] = {}
    variable_keys: set[str] = set()
    for node in webapp_detail_data.get('input_nodes', []) or []:
        variable_key = str(node.get('variable_name') or '').strip()
        if not variable_key:
            continue
        variable_keys.add(variable_key)
        for alias in (node.get('field_name'), node.get('field_label')):
            normalized = str(alias or '').strip().lower()
            if normalized:
                alias_candidates.setdefault(normalized, set()).add(variable_key)
    return alias_candidates, variable_keys


def validate_remote_param_keys(preflight: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    """Validate --param keys before submitting; duplicate-named fields must use variable_name, ambiguity is rejected outright."""
    alias_candidates, _ = remote_param_key_candidates(preflight)
    issues: list[dict[str, Any]] = []
    for item in getattr(args, 'param', None) or []:
        text = str(item)
        if '=' not in text:
            continue
        key = text.split('=', 1)[0].strip()
        normalized = key.lower()
        candidates = sorted(alias_candidates.get(normalized, set()))
        if len(candidates) > 1:
            issues.append({
                'param': key,
                'error': 'AMBIGUOUS_PARAM',
                'message': f'Parameter {key!r} maps to several fields: {", ".join(candidates)}; specify it with variable_name instead, e.g. --param "{candidates[0]}=...".',
                'candidates': candidates,
            })
    return issues


def build_remote_run_payload(bizy_model_id: str, preflight: dict[str, Any], args: argparse.Namespace, uploaded_urls: list[str] | None=None, uploaded_audios: list[str] | None=None, uploaded_videos: list[str] | None=None) -> dict[str, Any]:
    detail_data = extract_result_data(preflight.get('detail'))
    execution_target = preflight.get('execution_target') or {}
    runtime_webapp_detail = execution_target.get('webapp_detail') or preflight.get('webapp_detail')
    webapp_detail_data = extract_result_data(runtime_webapp_detail)
    version_data = extract_result_data(preflight.get('version_detail'))
    resolved_contract = preflight.get('resolved_contract') or (preflight.get('summary') or {}).get('resolved_contract') or contract.resolve_remote_input_contract(execution_target=execution_target, webapp_detail=preflight.get('webapp_detail'), workflow=preflight.get('workflow'))
    input_values: dict[str, Any] = {}
    uploaded_urls = list(uploaded_urls or [])
    uploaded_audios = list(uploaded_audios or [])
    uploaded_videos = list(uploaded_videos or [])
    contract_bindings = resolved_contract.get('bindings') or {}
    logical_bindings = contract_bindings.get('logical') or {}
    media_slot_bindings = contract_bindings.get('media_slots') or {}
    binding_field_map = contract.contract_field_by_binding_key(resolved_contract)
    if webapp_detail_data:
        image_index = 0
        audio_index = 0
        video_index = 0
        field_alias_map: dict[str, str] = {}
        alias_candidates: dict[str, set[str]] = {}
        bound_media_keys = {'image': set(), 'audio': set(), 'video': set()}
        for node in webapp_detail_data.get('input_nodes', []) or []:
            variable_name = node.get('variable_name')
            if not variable_name:
                continue
            variable_key = str(variable_name)
            field_name = str(node.get('field_name') or '').lower()
            field_label = str(node.get('field_label') or '').lower()
            field_type = str(node.get('field_type') or '').lower()
            field_value = node.get('field_value')
            media_slot_type = contract.classify_remote_media_slot(node)
            contract_field = binding_field_map.get(variable_key) or {}
            logical_name = str(contract_field.get('logical_name') or '')
            for alias in (field_name, field_label):
                if not alias:
                    continue
                alias_candidates.setdefault(alias, set()).add(variable_key)
                if len(alias_candidates[alias]) == 1:
                    field_alias_map[alias] = variable_key
                else:
                    # Duplicate-named fields must not be shadowed by a short name; use variable_name.
                    field_alias_map.pop(alias, None)
            # Media slot binding
            if uploaded_urls and image_index < len(uploaded_urls) and (media_slot_type == 'image' or variable_key in (media_slot_bindings.get('image') or [])):
                input_values[variable_key] = uploaded_urls[image_index]
                image_index += 1
                bound_media_keys['image'].add(variable_key)
                continue
            if uploaded_audios and audio_index < len(uploaded_audios) and (media_slot_type == 'audio' or variable_key in (media_slot_bindings.get('audio') or [])):
                input_values[variable_key] = uploaded_audios[audio_index]
                audio_index += 1
                bound_media_keys['audio'].add(variable_key)
                continue
            if uploaded_videos and video_index < len(uploaded_videos) and (media_slot_type == 'video' or variable_key in (media_slot_bindings.get('video') or [])):
                input_values[variable_key] = uploaded_videos[video_index]
                video_index += 1
                bound_media_keys['video'].add(variable_key)
                continue
            # Standard parameter override (duration/aspect_ratio, etc.)
            override_value = None
            if logical_name and logical_name in logical_bindings and (variable_key in (logical_bindings.get(logical_name) or [])):
                override_value = remote.match_remote_field_override(logical_name, args)
            if override_value is None:
                override_value = remote.match_remote_field_override(field_name or field_label, args)
            if override_value is not None:
                input_values[variable_key] = override_value
                continue
            # Skip hidden media slots
            if media_slot_type is not None and field_type == 'hidden':
                continue
            # Seed field fallback
            is_seed_field = logical_name == 'seed' or contract.is_remote_seed_field(field_name, field_label)
            empty_seed_value = field_value is None or field_value == '' or (not isinstance(field_value, bool) and field_value == 0) or field_value == '0'
            if is_seed_field and empty_seed_value:
                input_values[variable_key] = generate_seed()
                continue
            # Keep the default value
            input_values[variable_key] = field_value
        # Fill in unbound media slots
        for key in list(media_slot_bindings.get('image') or []):
            if key in bound_media_keys['image'] or image_index >= len(uploaded_urls):
                continue
            input_values[str(key)] = uploaded_urls[image_index]
            image_index += 1
        for key in list(media_slot_bindings.get('audio') or []):
            if key in bound_media_keys['audio'] or audio_index >= len(uploaded_audios):
                continue
            input_values[str(key)] = uploaded_audios[audio_index]
            audio_index += 1
        for key in list(media_slot_bindings.get('video') or []):
            if key in bound_media_keys['video'] or video_index >= len(uploaded_videos):
                continue
            input_values[str(key)] = uploaded_videos[video_index]
            video_index += 1
        # --prompt compatibility: write into every customtext-type non-negative text field (later --param overrides win)
        user_prompt = getattr(args, 'prompt', None)
        if user_prompt:
            text_field_types = {'customtext', 'textarea', 'text', 'string'}
            for node in webapp_detail_data.get('input_nodes', []) or []:
                vn = node.get('variable_name')
                if not vn:
                    continue
                ft = str(node.get('field_type') or '').lower()
                fn = str(node.get('field_name') or '').lower()
                fl = str(node.get('field_label') or '').lower()
                if ft not in text_field_types:
                    continue
                if contract.is_negative_prompt_key(fn, fl):
                    continue
                input_values[str(vn)] = user_prompt
        # --param exact override (highest priority)
        for item in getattr(args, 'param', None) or []:
            if '=' not in item:
                continue
            (key, value) = item.split('=', 1)
            resolved_key = field_alias_map.get(str(key).strip().lower(), key)
            input_values[resolved_key] = coerce_value(value)
        return {'web_app_id': webapp_detail_data.get('id'), 'backend_id': 0, 'client_id': ''.join((random.choice('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(25))), 'suppress_preview_output': False, 'input_values': input_values}
    # Fallback path when there is no webapp_detail
    for (key, url) in zip(list(media_slot_bindings.get('image') or []), uploaded_urls):
        input_values[str(key)] = url
    for (key, url) in zip(list(media_slot_bindings.get('audio') or []), uploaded_audios):
        input_values[str(key)] = url
    for (key, url) in zip(list(media_slot_bindings.get('video') or []), uploaded_videos):
        input_values[str(key)] = url
    for item in getattr(args, 'param', None) or []:
        if '=' not in item:
            continue
        (key, value) = item.split('=', 1)
        input_values[str(key)] = coerce_value(value)
    payload: dict[str, Any] = {'bizy_model_id': int(bizy_model_id) if str(bizy_model_id).isdigit() else bizy_model_id, 'input_values': input_values}
    identity_candidates = {'version_id': preflight.get('version_id') or version_data.get('id') or detail_data.get('bizy_model_version_id') or detail_data.get('version_id'), 'sign': version_data.get('sign') or detail_data.get('sign'), 'draft_id': version_data.get('draft_id') or detail_data.get('draft_id'), 'web_app_id': detail_data.get('web_app_id') or version_data.get('web_app_id'), 'bizy_model_version_id': version_data.get('bizy_model_version_id') or detail_data.get('bizy_model_version_id'), 'path': version_data.get('path') or detail_data.get('path'), 'file_name': version_data.get('file_name') or detail_data.get('file_name')}
    for (key, value) in identity_candidates.items():
        if value not in (None, '', [], {}):
            payload[key] = value
    media_slot_bindings = contract_bindings.get('media_slots') or {}
    if media_slot_bindings.get('image'):
        payload['expected_image_slots'] = len(media_slot_bindings['image'])
    if media_slot_bindings.get('audio'):
        payload['expected_audio_slots'] = len(media_slot_bindings['audio'])
    if media_slot_bindings.get('video'):
        payload['expected_video_slots'] = len(media_slot_bindings['video'])
    return payload

def create_remote_task_attempt(api_key: str, bizy_model_id: str, preflight: dict[str, Any], args: argparse.Namespace, uploaded_urls: list[str] | None=None, uploaded_audios: list[str] | None=None, uploaded_videos: list[str] | None=None, *, use_async: bool=True) -> dict[str, Any]:
    headers = {}
    if use_async:
        headers['X-Bizyair-Task-Async'] = 'enable'
    payload = build_remote_run_payload(bizy_model_id, preflight, args, uploaded_urls=uploaded_urls, uploaded_audios=uploaded_audios, uploaded_videos=uploaded_videos)
    result = safe_request_json('POST', CREATE_URL, api_key, payload=payload, headers=headers, timeout=120)
    return {'payload': payload, 'result': result, 'auth_mode': 'api_key', 'submit_route': 'openapi_create_api_key'}

def _resolve_task_query_url(template: str, request_id: str) -> str | None:
    """The task query URL may be a template (the international site puts request_id directly in the path segment).

    Contains {request_id} → substitute into a full URL and stop passing params;
    does not contain it → return None, and the caller falls back to the ?requestId= query parameter.
    """
    if '{request_id}' in str(template or ''):
        return str(template).replace('{request_id}', request_id)
    return None


def query_detail(api_key: str, request_id: str) -> dict[str, Any]:
    url = _resolve_task_query_url(DETAIL_URL, request_id)
    if url:
        return request_json('GET', url, api_key)
    return request_json('GET', DETAIL_URL, api_key, params={'requestId': request_id})


def query_outputs(api_key: str, request_id: str) -> dict[str, Any]:
    url = _resolve_task_query_url(OUTPUTS_URL, request_id)
    if url:
        return request_json('GET', url, api_key)
    return request_json('GET', OUTPUTS_URL, api_key, params={'requestId': request_id})

def normalize_output_extension(ext: str | None) -> str | None:
    if not ext:
        return None
    cleaned = str(ext).strip()
    if not cleaned:
        return None
    return cleaned if cleaned.startswith('.') else f'.{cleaned}'

def infer_extension_from_url(url: str) -> str | None:
    path = urllib.parse.urlparse(url or '').path
    suffix = Path(path).suffix
    return normalize_output_extension(suffix)

UNTRUSTED_CONTENT_TYPE_MIMES = {'application/octet-stream', 'binary/octet-stream'}

def infer_extension_from_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    mime = str(content_type).split(';', 1)[0].strip().lower()
    if mime in UNTRUSTED_CONTENT_TYPE_MIMES:
        return None
    if not mime:
        return None
    guessed = mimetypes.guess_extension(mime)
    if guessed:
        return normalize_output_extension(guessed)
    manual_map = {'video/mp4': '.mp4', 'video/quicktime': '.mov', 'video/webm': '.webm', 'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'image/gif': '.gif'}
    return manual_map.get(mime)

def resolve_download_output_path(output_path: str, *, content_type: str | None=None, source_url: str | None=None, preferred_ext: str | None=None) -> Path:
    out = Path(output_path)
    if out.suffix:
        return out
    inferred_ext = infer_extension_from_content_type(content_type) or infer_extension_from_url(source_url or '') or normalize_output_extension(preferred_ext)
    if not inferred_ext:
        return out
    return out.with_name(f'{out.name}{inferred_ext}')

def download_file(url: str, output_path: str, *, preferred_ext: str | None=None) -> str:
    import http.client
    import shutil as _shutil
    import urllib.error
    # Outputs land on storage.bizyair.ai (behind Cloudflare); a request without a User-Agent is
    # blocked with a 403 — this used to call urlopen(url) with no UA, so a task that had clearly
    # succeeded crashed in the download step and the result URLs were never delivered. Like API
    # requests, a UA is mandatory.
    #
    # Download is an idempotent GET, and the current network intermittently raises
    # "Connection reset by peer" (actually observed), so transport-layer failures get bounded
    # retries; 4xx is not retried, and 5xx retries at most twice.
    attempts = 3
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=300) as resp:
                final_url = resp.geturl() or url
                content_type = resp.headers.get('Content-Type')
                out = resolve_download_output_path(output_path, content_type=content_type, source_url=final_url, preferred_ext=preferred_ext)
                out.parent.mkdir(parents=True, exist_ok=True)
                with open(out, 'wb') as f:
                    _shutil.copyfileobj(resp, f, length=8 * 1024 * 1024)
            return str(out.resolve())
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code < 500 or attempt >= attempts - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionResetError, http.client.HTTPException) as exc:
            last_exc = exc
            if attempt >= attempts - 1:
                raise
        time.sleep(0.8 * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f'Download failed: {url}')

def resolve_indexed_output_path(output_path: str | None, *, index: int, total: int, preferred_ext: str | None=None) -> str:
    default_output_root = resolve_output_root()
    if total <= 1:
        if output_path:
            # A single output must also honor a directory: otherwise <dir name>.png is written next to the directory (the multi-output branch makes the same check).
            candidate = Path(output_path)
            if candidate.exists() and candidate.is_dir():
                inferred_ext = normalize_output_extension(preferred_ext) or '.bin'
                return str((candidate / f'result{inferred_ext}').resolve())
            return output_path
        inferred_ext = normalize_output_extension(preferred_ext) or '.bin'
        return str((default_output_root / f'result{inferred_ext}').resolve())
    if output_path:
        base_path = Path(output_path)
        if base_path.exists() and base_path.is_dir():
            base_path = base_path / 'result'
    else:
        base_path = default_output_root / 'result'
    preferred = normalize_output_extension(preferred_ext)
    suffix = preferred or base_path.suffix
    if base_path.suffix:
        base_name = base_path.stem
        return str(base_path.with_name(f'{base_name}-{index:02d}{suffix or base_path.suffix}').resolve())
    return str(base_path.with_name(f"{base_path.name}-{index:02d}{suffix or ''}").resolve())

def download_outputs(outputs: list[dict[str, Any]], output_path: str | None) -> list[str]:
    saved_paths: list[str] = []
    total = len(outputs)
    for (index, item) in enumerate(outputs, start=1):
        object_url = item.get('object_url')
        if not object_url:
            raise ValueError(f'output #{index} missing object_url')
        target_path = resolve_indexed_output_path(output_path, index=index, total=total, preferred_ext=item.get('output_ext'))
        saved_paths.append(download_file(object_url, target_path, preferred_ext=item.get('output_ext')))
    return saved_paths

def write_run_manifest(output_path: str | None, *, request_id: str | None, outputs: list[dict[str, Any]], saved_files: list[str], slug: str | None=None, prompt: str | None=None, params: dict[str, Any] | None=None) -> dict[str, Any]:
    """Write run.json for AI app tasks (fixed-model tasks use modelzoo.save_task_outputs).

    Batch workers require this manifest after a successful child exit; without it an
    otherwise successful app task is marked failed. The run directory follows the same
    semantics as download_outputs: a suffix path is a file base (parent is the run dir),
    otherwise the path is the directory; with no path, use the first saved file's folder.
    """
    raw_output = str(output_path).strip() if output_path else ''
    if raw_output:
        candidate = Path(raw_output).expanduser().resolve()
        run_dir = candidate.parent if candidate.suffix else candidate
    elif saved_files:
        run_dir = Path(saved_files[0]).expanduser().resolve().parent
    else:
        from paths import resolve_output_root
        run_dir = resolve_output_root()
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        'slug': slug,
        'request_id': request_id,
        'status': 'Success',
        'saved_at': int(time.time()),
        'output_path': str(run_dir),
        'outputs': outputs or [],
        'saved_files': saved_files or [],
        'download_errors': [],
        'prompt': prompt,
        'params': params or {},
    }
    manifest_path = run_dir / 'run.json'
    manifest['manifest_path'] = str(manifest_path.resolve())
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest

def _is_auxiliary_output(object_url: str, saved_path: str) -> bool:
    """Recognize intermediate images / placeholder thumbnails (e.g. _temp_ node outputs from rgthree.compare)."""
    url_lower = (object_url or '').lower()
    if '_temp_' in url_lower or 'rgthree.compare' in url_lower:
        return True
    # Files < 10KB in an image format are most likely placeholder thumbnails
    try:
        size = Path(saved_path).stat().st_size
        if size < 10240 and any(saved_path.lower().endswith(ext) for ext in ('.png', '.jpg', '.jpeg', '.webp')):
            return True
    except Exception:
        pass
    return False


def emit_downloaded_outputs(outputs: list[dict[str, Any]], saved_paths: list[str]) -> None:
    video_exts = ('.mp4', '.mov', '.webm', '.mkv', '.m4v')
    main_outputs = []
    aux_outputs = []
    for (index, saved) in enumerate(saved_paths, start=1):
        output_meta = outputs[index - 1] if index - 1 < len(outputs) else {}
        object_url = output_meta.get('object_url') or ''
        if _is_auxiliary_output(object_url, saved):
            aux_outputs.append((index, saved, output_meta))
        else:
            main_outputs.append((index, saved, output_meta))

    print(f'OUTPUT_COUNT:{len(main_outputs)}')
    for (index, saved, output_meta) in main_outputs:
        object_url = output_meta.get('object_url') or ''
        print(f'OUTPUT_FILE:{saved}')
        if object_url:
            print(f'OUTPUT_URL:{object_url}')
        url_lower = object_url.lower().split('?', 1)[0]
        saved_lower = saved.lower()
        is_video = url_lower.endswith(video_exts) or saved_lower.endswith(video_exts)
        if is_video and object_url:
            print(object_url)
        else:
            display_src = object_url or saved
            print(f'![Generated result]({display_src})')
        if output_meta.get('cost_time') is not None:
            print(f"OUTPUT_{index:02d}_DURATION_MS:{output_meta['cost_time']}")

    if aux_outputs:
        print(f'AUX_OUTPUT_COUNT:{len(aux_outputs)}')
        for (index, saved, output_meta) in aux_outputs:
            print(f'AUX_OUTPUT_FILE:{saved}')

def poll_until_done(api_key: str, request_id: str) -> dict[str, Any]:
    start = time.time()
    last_status = None
    consecutive_errors = 0
    while time.time() - start < MAX_POLL_SECONDS:
        resp = query_detail(api_key, request_id)
        # query_detail returned an error (network jitter, etc.) → tolerate a few then give up
        if isinstance(resp, dict) and resp.get('ok') is False:
            consecutive_errors += 1
            if consecutive_errors >= 3:
                return {'ok': False, 'error': 'POLL_QUERY_FAILED', 'detail': resp.get('error'), 'request_id': request_id}
            time.sleep(POLL_INTERVAL)
            continue
        consecutive_errors = 0
        data = extract_result_data(resp)
        status = data.get('status')
        if status != last_status:
            print(f'STATUS:{status}', file=sys.stderr)
            last_status = status
        if status == 'Success':
            outputs_resp = query_outputs(api_key, request_id)
            if isinstance(outputs_resp, dict) and outputs_resp.get('ok') is False:
                return {'ok': False, 'error': 'OUTPUTS_QUERY_FAILED', 'detail': outputs_resp.get('error'), 'request_id': request_id}
            return outputs_resp
        if status in {'Failed', 'Canceled'}:
            return {'ok': False, 'error': 'TASK_FAILED', 'status': status, 'detail': data, 'request_id': request_id}
        time.sleep(POLL_INTERVAL)
    return {'ok': False, 'error': 'TIMEOUT', 'message': 'Polling timed out', 'request_id': request_id}
