"""catalog.py — export the ModelZoo / community AI app catalog as Markdown (v1.11).

Purpose: pull the full catalog once and write it to a file, so later lookups do not
keep hitting the API, and so the list is easy to paste into documentation.
Difference from the search commands: there is no ranking/recommendation here, only a
"complete, savable list".
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import api
import common
import paths

MODELZOO_PAGE_SIZE = 100
MAX_PAGES = 20


def _save(text: str, target: str | None, default_name: str) -> str:
    if target:
        path = Path(target).expanduser()
        if path.is_dir():
            path = path / default_name
    else:
        path = paths.resolve_output_root() / default_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return str(path.resolve())


def _escape(value: Any) -> str:
    return str(value if value is not None else '').replace('|', '\\|')


def fetch_all_modelzoo(api_key: str) -> tuple[list[dict[str, Any]], Any]:
    items: list[dict[str, Any]] = []
    total = None
    for page in range(1, MAX_PAGES + 1):
        result = api.safe_request_json(
            'POST', f'{common.MODELZOO_LIST_URL}?current={page}&page_size={MODELZOO_PAGE_SIZE}&sort=Auto',
            api_key, payload={'tags': [], 'categories': [], 'show_deprecated': False},
        )
        api.require_ok(result, 'modelzoo catalog')
        resp = api.extract_result_data(result) or {}
        if total is None:
            total = resp.get('total')
        page_items = resp.get('list') or []
        items.extend(page_items)
        if len(page_items) < MODELZOO_PAGE_SIZE or (total is not None and len(items) >= int(total)):
            break
    return items, total


def modelzoo_markdown(items: list[dict[str, Any]], total: Any) -> str:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(str(item.get('category') or 'Other'), []).append(item)
    lines = ['# BizyAir ModelZoo catalog', '', f'{total if total is not None else len(items)} endpoints in total', '']
    for category in sorted(groups):
        lines += [f'## {category}', '', '| Endpoint | Name | Price (credits/run) | Version |', '|---|---|---:|---|']
        for item in sorted(groups[category], key=lambda x: str(x.get('endpoint') or '')):
            lines.append('| `{}` | {} | {} | {} |'.format(
                _escape(item.get('endpoint')), _escape(item.get('display_name')),
                _escape(item.get('min_credits') or '-'), _escape(item.get('edition') or '-')))
        lines.append('')
    return '\n'.join(lines)


def fetch_all_apps(api_key: str) -> tuple[list[dict[str, Any]], Any]:
    items: list[dict[str, Any]] = []
    total = None
    for page in range(1, MAX_PAGES + 1):
        resp = api.fetch_remote_models(api_key, remote_source='community', page=page,
                                       page_size=100, keyword='', sort='Most Used', model_types=['Application'])
        api.require_ok(resp, 'app catalog')
        data = api.extract_result_data(resp) or {}
        if total is None:
            total = data.get('total')
        page_items = data.get('list') or []
        items.extend(page_items)
        if len(page_items) < 100 or (total is not None and len(items) >= int(total)):
            break
    return items, total


def _public_version(item: dict[str, Any]) -> dict[str, Any]:
    versions = item.get('versions') or []
    for version in versions:
        if version.get('public') and version.get('available'):
            return version
    return versions[0] if versions else {}


def apps_markdown(items: list[dict[str, Any]], total: Any) -> str:
    lines = ['# BizyAir community AI apps', '', f'{total if total is not None else len(items)} in total', '',
             '| Name | Base model | web_app_id | Version |', '|---|---|---|---|']
    for item in items:
        version = _public_version(item)
        lines.append('| {} | {} | `{}` | {} |'.format(
            _escape(item.get('name')), _escape(version.get('base_model') or '-'),
            _escape(version.get('id')), _escape(version.get('version') or '-')))
    return '\n'.join(lines)


def _cmd(rest: list[str], *, kind: str) -> None:
    save = '--save' in rest
    target = None
    api_key_arg = None
    for index, arg in enumerate(rest):
        if arg == '--out' and index + 1 < len(rest):
            target = rest[index + 1]
        elif arg == '--api-key' and index + 1 < len(rest):
            api_key_arg = rest[index + 1]
        elif arg.startswith('--api-key='):
            api_key_arg = arg.split('=', 1)[1]
    api_key = api.require_api_key(api_key_arg)
    if kind == 'modelzoo':
        items, total = fetch_all_modelzoo(api_key)
        text = modelzoo_markdown(items, total)
        default_name = 'MODELS.md'
    else:
        items, total = fetch_all_apps(api_key)
        text = apps_markdown(items, total)
        default_name = 'APPS.md'
    print(text)
    if save:
        payload = {'saved_to': _save(text, target, default_name), 'count': len(items), 'total': total}
        print('\n' + json.dumps(payload, ensure_ascii=False))


def cmd_modelzoo_md(rest: list[str]) -> None:
    _cmd(rest, kind='modelzoo')


def cmd_app_md(rest: list[str]) -> None:
    _cmd(rest, kind='apps')
