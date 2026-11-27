"""tasks.py — single task lookup and local file upload (v1.11).

These three commands cover "resume-style checks after or outside run":
  status  <request_id>   check task status once (no polling, no billing)
  outputs <request_id>   fetch outputs; optionally download them locally
  upload  <file>         local file -> OSS URL (for assembling your own payload)

Read-only/idempotent, no confirmation gate, and no task is created.
"""
from __future__ import annotations

import json
import sys
from typing import Any

import api
from common import DETAIL_URL, OUTPUTS_URL

STATUS_LABELS = {
    'Queuing': 'Queuing',
    'Preparing': 'Preparing environment',
    'Running': 'Running',
    'Success': 'Success',
    'Failed': 'Failed',
    'Canceled': 'Canceled',
}


def _request(api_key: str | None, url: str) -> dict[str, Any]:
    key = api.require_api_key(api_key)
    return api.safe_request_json('GET', url, key)


def cmd_status(rest: list[str]) -> None:
    request_id = next((arg for arg in rest if not arg.startswith('--')), None)
    reply_format = 'json'
    if '--reply-format' in rest:
        reply_format = rest[rest.index('--reply-format') + 1]
    if not request_id:
        print('Usage: cli.py status <request_id> [--reply-format json|markdown]', file=sys.stderr)
        raise SystemExit(2)

    result = _request(None, DETAIL_URL.format(request_id=request_id))
    data = api.extract_result_data(result) if api.result_is_ok(result) else {}
    payload: dict[str, Any] = {
        'action': 'status',
        'request_id': request_id,
        'ok': api.result_is_ok(result),
        'status': (data or {}).get('status'),
        'data': data,
    }
    if not api.result_is_ok(result):
        payload['error'] = 'REMOTE_REQUEST_FAILED'
        payload['message'] = api.request_error_message(result)
        payload['http_status'] = result.get('status')
    status = payload.get('status') or 'Unknown'
    payload['status_label'] = STATUS_LABELS.get(status, status)
    payload['reply_markdown'] = f"Task `{request_id}`: **{payload['status_label']}**" if payload['ok'] else f"Lookup failed: {payload.get('message')}"

    if reply_format == 'markdown':
        print(payload['reply_markdown'])
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload['ok']:
        raise SystemExit(1)


def cmd_outputs(rest: list[str]) -> None:
    request_id = next((arg for arg in rest if not arg.startswith('--')), None)
    reply_format = 'json'
    output_dir = None
    index = 0
    while index < len(rest):
        flag = rest[index]
        if flag == '--reply-format' and index + 1 < len(rest):
            reply_format = rest[index + 1]
            index += 2
            continue
        if flag in {'-o', '--output'} and index + 1 < len(rest):
            output_dir = rest[index + 1]
            index += 2
            continue
        index += 1
    if not request_id:
        print('Usage: cli.py outputs <request_id> [-o dir] [--reply-format json|markdown]', file=sys.stderr)
        raise SystemExit(2)

    result = _request(None, OUTPUTS_URL.format(request_id=request_id))
    data = api.extract_result_data(result) if api.result_is_ok(result) else {}
    outputs = (data or {}).get('outputs') or []
    payload: dict[str, Any] = {
        'action': 'outputs',
        'request_id': request_id,
        'ok': api.result_is_ok(result),
        'count': len(outputs),
        'outputs': outputs,
    }
    if not api.result_is_ok(result):
        payload['error'] = 'REMOTE_REQUEST_FAILED'
        payload['message'] = api.request_error_message(result)
        payload['http_status'] = result.get('status')
    elif output_dir:
        try:
            payload['saved_files'] = api.download_outputs(outputs, output_dir)
        except Exception as exc:
            payload['download_error'] = str(exc)

    lines = [f"Task `{request_id}` outputs: {len(outputs)}"]
    for item in outputs:
        url = item.get('object_url')
        if not url:
            continue
        ext = (item.get('output_ext') or '').lower()
        lines.append(f"![output]({url})" if ext in ('.png', '.jpg', '.jpeg', '.webp') else url)
    if payload.get('saved_files'):
        lines.append('Downloaded: ' + ', '.join(payload['saved_files']))
    payload['reply_markdown'] = '\n'.join(lines)

    if reply_format == 'markdown':
        print(payload['reply_markdown'])
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload['ok']:
        raise SystemExit(1)


def cmd_upload(rest: list[str]) -> None:
    file_path = next((arg for arg in rest if not arg.startswith('--')), None)
    reply_format = 'json'
    if '--reply-format' in rest:
        reply_format = rest[rest.index('--reply-format') + 1]
    if not file_path:
        print('Usage: cli.py upload <local file>', file=sys.stderr)
        raise SystemExit(2)

    url = api.upload_input_file(api.require_api_key(None), file_path)
    payload = {'action': 'upload', 'ok': True, 'local_path': file_path, 'access_url': url}
    payload['reply_markdown'] = f"Uploaded: {url}"
    if reply_format == 'markdown':
        print(payload['reply_markdown'])
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
