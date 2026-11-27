"""publish.py — model publishing (uploading local model files).

Mirrors the official bizyair-cli `bizyair upload`; the flow is verified end to end (2026-10-29):

  1. Compute the signature: sign = sha256(base64(md5(file)) + str(crc64_ecma(file)))
  2. GET  /v1/files/{sign}?type=<ModelType>   fetch OSS STS temporary credentials
     (data.file.id > 0 means the server already registered this file, so skip the upload)
  3. PUT  direct OSS upload
  4. POST /v1/files                          register the file (sign + object_key + md5_hash + type)
  5. Cover upload: /v1/upload/token?file_type=inputs -> OSS -> /v1/input_resource/commit, get the URL
  6. POST /v1/bizy_models                    create the model (name + type + versions[])

Safety gate: without --confirm-publish it only prints a parameter card and exits with code 3;
account data (model creation + file upload) is written only on a real submit, and only to your own account.

CLI usage:
  python3 scripts/cli.py model-publish -n mymodel -t LoRA \\
    -p /path/a.safetensors -b "FLUX.1 D" -cover cover.jpg -i "intro" [--public] --confirm-publish

Multiple versions (--file is repeatable; --base-model/--version/--cover/--intro align in order):
  python3 scripts/cli.py model-publish -n mymodel -t Checkpoint \\
    -p a.safetensors -b SDXL -v v1.0 -cover c1.jpg -i "first version" \\
    -p b.safetensors -b "SD 1.5" -v v2.0 -cover c2.jpg -i "second version" --confirm-publish
"""
from __future__ import annotations

import json
import sys
import time
import tempfile
from pathlib import Path
from typing import Any

import api
import common

MODEL_TYPES = ('Checkpoint', 'VAE', 'UNet', 'LoRA', 'Controlnet', 'CLIP', 'Upscaler', 'Detection', 'Other')

MODEL_FILE_EXTS = {'.safetensors', '.ckpt', '.pt', '.pth', '.bin', '.sft'}

COVER_EXTS = {
    '.jpg': 'image', '.jpeg': 'image', '.png': 'image', '.gif': 'image', '.webp': 'image',
    '.mp4': 'video', '.webm': 'video', '.mov': 'video',
}

MAX_INTRO_CHARS = 5000

# Multipart upload removes the 5 GB single-PUT limit; this is only a soft cap that guards against mistakes.
MAX_MODEL_BYTES = 200 * 1024 * 1024 * 1024
DEFAULT_UPLOAD_CONCURRENCY = 6

MODEL_TYPE_HELP = ', '.join(MODEL_TYPES)


def _fmt_size(size: Any) -> str:
    try:
        value = float(size or 0)
    except (TypeError, ValueError):
        return '—'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB':
            return f'{value:.0f} {unit}' if unit == 'B' else f'{value:.2f} {unit}'
        value /= 1024
    return '—'


def _take_value(rest: list[str], index: int, flag: str) -> str:
    if index + 1 >= len(rest):
        print(f'{flag} is missing a value', file=sys.stderr)
        raise SystemExit(2)
    value = rest[index + 1]
    if value.startswith('--') and value not in {'-'}:
        print(f'{flag} is missing a value', file=sys.stderr)
        raise SystemExit(2)
    return value


def _parse_args(rest: list[str]) -> dict[str, Any]:
    options: dict[str, Any] = {
        'name': None,
        'type': None,
        'files': [],
        'base_models': [],
        'versions': [],
        'covers': [],
        'intros': [],
        'public': False,
        'confirm': '--confirm-publish' in rest,
        'dry_run': '--dry-run' in rest,
        'restart_upload': '--restart-upload' in rest,
        'upload_concurrency': DEFAULT_UPLOAD_CONCURRENCY,
        'api_key': None,
        'reply_format': 'markdown',
    }
    repeat_flags = {'--file': 'files', '-p': 'files', '--base-model': 'base_models', '-b': 'base_models',
                    '--version': 'versions', '-v': 'versions',
                    '--cover': 'covers', '-cover': 'covers', '--intro': 'intros', '-i': 'intros'}
    single_flags = {'--name': 'name', '-n': 'name', '--type': 'type', '-t': 'type',
                    '--api-key': 'api_key', '--reply-format': 'reply_format'}
    index = 0
    while index < len(rest):
        flag = rest[index]
        if flag in repeat_flags:
            options[repeat_flags[flag]].append(_take_value(rest, index, flag))
            index += 2
            continue
        if flag in single_flags:
            options[single_flags[flag]] = _take_value(rest, index, flag)
            index += 2
            continue
        if flag == '--upload-concurrency':
            raw = _take_value(rest, index, flag)
            try:
                value = int(raw)
            except ValueError:
                print(f'--upload-concurrency needs an integer: {raw}', file=sys.stderr)
                raise SystemExit(2)
            if not 1 <= value <= 8:
                print('--upload-concurrency only supports 1-8', file=sys.stderr)
                raise SystemExit(2)
            options['upload_concurrency'] = value
            index += 2
            continue
        if flag == '--intro-path':
            path = _take_value(rest, index, flag)
            try:
                text = Path(path).read_text(encoding='utf-8')
            except OSError as exc:
                print(f'Failed to read --intro-path: {exc}', file=sys.stderr)
                raise SystemExit(2)
            options['intros'].append(text[:MAX_INTRO_CHARS])
            index += 2
            continue
        if flag == '--public':
            options['public'] = True
        elif flag == '--private':
            options['public'] = False
        index += 1
    return options


def _align(values: list[str], count: int, default: Any, label: str) -> list[Any]:
    if not values:
        return [default] * count
    if len(values) == 1:
        return [values[0]] * count
    if len(values) != count:
        print(f'{label} count ({len(values)}) does not match the --file count ({count}). '
              f'Pass a single value (shared by all versions), or one value per version.', file=sys.stderr)
        raise SystemExit(2)
    return list(values)


def _normalize_versions(options: dict[str, Any]) -> list[dict[str, Any]]:
    files = [str(item) for item in options['files']]
    if not files:
        print('Missing --file: provide at least one local model file path', file=sys.stderr)
        raise SystemExit(2)
    count = len(files)
    base_models = _align(options['base_models'], count, None, '--base-model')
    versions = _align(options['versions'], count, 'v1.0', '--version')
    covers = _align(options['covers'], count, None, '--cover')
    intros = _align(options['intros'], count, '', '--intro')
    items = []
    for idx in range(count):
        items.append({
            'file': files[idx],
            'base_model': base_models[idx],
            'version': versions[idx],
            'cover': covers[idx],
            'intro': intros[idx],
            'public': bool(options['public']),
        })
    return items


def _validate(options: dict[str, Any], versions: list[dict[str, Any]]) -> None:
    name = str(options.get('name') or '').strip()
    if not name:
        print('Missing --name: the model name is required', file=sys.stderr)
        raise SystemExit(2)
    if len(name) > 60:
        print('--name is too long: model names are at most 60 characters', file=sys.stderr)
        raise SystemExit(2)
    model_type = str(options.get('type') or '').strip()
    if not model_type:
        print(f'Missing --type: the model type is required ({MODEL_TYPE_HELP})', file=sys.stderr)
        raise SystemExit(2)
    if model_type not in MODEL_TYPES:
        print(f'Unsupported --type: {model_type}\nSupported: {MODEL_TYPE_HELP}', file=sys.stderr)
        raise SystemExit(2)
    for item in versions:
        file_path = item['file']
        path = Path(file_path).expanduser()
        if str(file_path).startswith(('http://', 'https://')):
            print(
                'This command only accepts local files; use model-repost for third-party links (Civitai / HuggingFace / ModelScope).',
                file=sys.stderr,
            )
            raise SystemExit(2)
        if not path.exists():
            print(f'Model file does not exist: {file_path}', file=sys.stderr)
            raise SystemExit(2)
        if not path.is_file():
            print(f'--file must be a file, not a directory: {file_path}', file=sys.stderr)
            raise SystemExit(2)
        if path.suffix.lower() not in MODEL_FILE_EXTS:
            print(f'Unsupported model file format: {path.suffix or "(no extension)"}\n'
                  f'Supported: {", ".join(sorted(MODEL_FILE_EXTS))}', file=sys.stderr)
            raise SystemExit(2)
        size = path.stat().st_size
        if size <= 0:
            print(f'Model file is empty: {file_path}', file=sys.stderr)
            raise SystemExit(2)
        if size > MAX_MODEL_BYTES:
            print(f'Model file exceeds the 200 GB soft cap: {file_path} ({_fmt_size(size)})', file=sys.stderr)
            raise SystemExit(2)
        item['resolved_file'] = str(path.resolve())
        item['size'] = size
        if not item['base_model']:
            print(f'Missing --base-model: {file_path} needs a base model (e.g. "FLUX.1 D", "SDXL")', file=sys.stderr)
            raise SystemExit(2)
        cover = item['cover']
        if not cover:
            # No cover ready: cover.render_cover generates a placeholder at publish time through the same upload flow
            item['cover_kind'] = 'auto'
            item['cover'] = '(placeholder cover auto-generated)'
        elif str(cover).startswith(('http://', 'https://')):
            item['cover_kind'] = 'url'
        else:
            cover_path = Path(cover).expanduser()
            if not cover_path.exists() or not cover_path.is_file():
                print(f'Cover file does not exist: {cover}', file=sys.stderr)
                raise SystemExit(2)
            ext = cover_path.suffix.lower()
            if ext not in COVER_EXTS:
                print(f'Unsupported cover format: {ext}\nSupported: {", ".join(sorted(COVER_EXTS))}', file=sys.stderr)
                raise SystemExit(2)
            if COVER_EXTS[ext] == 'video' and cover_path.stat().st_size > 100 * 1024 * 1024:
                print(f'Cover videos must not exceed 100 MB: {cover}', file=sys.stderr)
                raise SystemExit(2)
            item['cover_kind'] = 'file'
            item['resolved_cover'] = str(cover_path.resolve())
        intro = str(item['intro'] or '')
        if len(intro) > MAX_INTRO_CHARS:
            print(f'Intro text exceeds {MAX_INTRO_CHARS} characters; shorten it and retry', file=sys.stderr)
            raise SystemExit(2)
        item['intro'] = intro


def _plan_card(options: dict[str, Any], versions: list[dict[str, Any]], *, note: str) -> dict[str, Any]:
    return {
        'status': 'pending_confirmation',
        'action': 'model-publish',
        'request_body': {
            'name': str(options['name']).strip(),
            'type': str(options['type']).strip(),
            'versions': [
                {
                    'version': item['version'],
                    'base_model': item['base_model'],
                    'public': item['public'],
                    'file': item['resolved_file'],
                    'file_size': item['size'],
                    'cover': item.get('resolved_cover') or item['cover'],
                    'intro_chars': len(item['intro']),
                }
                for item in versions
            ],
        },
        'note': note,
        'next': 'To confirm, append --confirm-publish to the end of the same command and submit.',
    }


def _base_model_warning(api_key: str, versions: list[dict[str, Any]]) -> str | None:
    """Optional check: compare against /v1/dict base_models; if the API fails, skip it (never blocks publishing)."""
    try:
        allowed = api.fetch_base_models(api_key)
    except Exception:
        return None
    if not allowed:
        return None
    unknown = []
    for item in versions:
        if item['base_model'] not in allowed and item['base_model'] not in unknown:
            unknown.append(item['base_model'])
    if unknown:
        return (f'Base model is not in the official dictionary: {", ".join(unknown)}\n'
                f'Available values: {", ".join(allowed)}')
    return None


class _ProgressPrinter:
    """Print hashing/upload progress to stderr; refresh in place on a TTY, print every 10% otherwise."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.started = time.time()
        self.last_time = 0.0
        self.last_pct = -1.0
        self.tty = sys.stderr.isatty()

    def __call__(self, done: int, total: int) -> None:
        if total <= 0:
            return
        pct = done * 100.0 / total
        now = time.time()
        if pct < 100 and now - self.last_time < 1.0 and pct - self.last_pct < 5:
            return
        if not self.tty and pct < 100 and int(pct // 10) == int(self.last_pct // 10):
            return
        self.last_time = now
        self.last_pct = pct
        elapsed = max(0.001, now - self.started)
        speed = done / elapsed / (1024 * 1024)
        line = (f'{self.label} {pct:5.1f}%  {_fmt_size(done)} / {_fmt_size(total)}'
                f'  {speed:.1f} MB/s')
        if self.tty:
            print('\r' + line.ljust(72), end='', file=sys.stderr, flush=True)
            if pct >= 100:
                print(file=sys.stderr)
        elif pct >= 100 or int(pct // 10) != int(self.last_pct // 10):
            print(line, file=sys.stderr, flush=True)

    def done(self) -> None:
        if self.tty:
            print(file=sys.stderr)


def _upload_one(api_key: str, item: dict[str, Any], model_type: str, index: int, total: int,
                *, concurrency: int = DEFAULT_UPLOAD_CONCURRENCY, restart: bool = False) -> dict[str, Any]:
    file_path = item['resolved_file']
    name = Path(file_path).name
    print(f'[{index}/{total}] Computing signature for {name} ({_fmt_size(item["size"])}) ...', file=sys.stderr)
    signature_progress = _ProgressPrinter('Computing signature')
    signature = api.model_file_signature(file_path, progress=signature_progress)
    signature_progress.done()
    item['sign'] = signature['sign']
    item['md5_b64'] = signature['md5_b64']

    token_resp = api.fetch_model_file_token(api_key, signature['sign'], model_type)
    if not api.result_is_ok(token_resp):
        raise PublishError(api.request_error_message(token_resp) or 'Failed to get file upload credentials')
    token_inner = api.extract_result_data(token_resp) or {}
    file_info = token_inner.get('file') or {}
    object_key = file_info.get('object_key')

    if file_info.get('id'):
        print(f'[{index}/{total}] The server already has this file (id={file_info.get("id")}), skipping upload', file=sys.stderr)
    else:
        print(f'[{index}/{total}] Multipart upload {name} -> OSS ({concurrency} concurrent, resumable) ...', file=sys.stderr)

        def refresh() -> dict[str, Any]:
            refreshed = api.fetch_model_file_token(api_key, signature['sign'], model_type)
            if not api.result_is_ok(refreshed):
                raise PublishError(api.request_error_message(refreshed) or 'Failed to refresh upload credentials')
            return api.extract_result_data(refreshed) or {}

        upload_progress = _ProgressPrinter('Uploading')
        info = api.upload_model_file_oss(
            file_path, token_inner,
            token_refresh=refresh,
            concurrency=concurrency,
            progress=upload_progress,
            restart=restart,
            checkpoint_name=signature['sign'],
        )
        upload_progress.done()
        if info.get('resumed_parts'):
            print(f'[{index}/{total}] Resumed {info["resumed_parts"]} parts (of {info.get("total_parts")})', file=sys.stderr)
        if info.get('restarted'):
            print(f'[{index}/{total}] The previous upload session expired; upload restarted', file=sys.stderr)
        commit_resp = api.commit_model_file(
            api_key,
            sign=signature['sign'],
            object_key=object_key,
            md5_b64=signature['md5_b64'],
            model_type=model_type,
        )
        if not api.result_is_ok(commit_resp):
            message = api.request_error_message(commit_resp) or 'File registration failed'
            raise PublishError(message)
        committed = api.extract_result_data(commit_resp) or {}
        object_key = (committed.get('file') or {}).get('object_key') or object_key
        if info.get('checkpoint_path'):
            api.clear_model_upload_checkpoint(info['checkpoint_path'])
    item['object_key'] = object_key
    return item


def _resolve_cover(api_key: str, item: dict[str, Any], index: int, total: int, *, name: str, model_type: str) -> str:
    if item['cover_kind'] == 'url':
        return item['cover']
    if item['cover_kind'] == 'auto':
        import cover
        workspace = Path(tempfile.mkdtemp(prefix='bizyair-cover-'))
        try:
            rendered = cover.render_cover(
                workspace / 'auto-cover.png',
                name=name,
                model_type=model_type,
                base_model=item.get('base_model') or '',
            )
            print(f'[{index}/{total}] No cover provided; generated placeholder cover {rendered.name} ...', file=sys.stderr)
            return api.upload_input_file(api_key, str(rendered))
        finally:
            for leftover in workspace.glob('*'):
                leftover.unlink(missing_ok=True)
            workspace.rmdir()
    print(f'[{index}/{total}] Uploading cover {Path(item["resolved_cover"]).name} ...', file=sys.stderr)
    return api.upload_input_file(api_key, item['resolved_cover'])


class PublishError(RuntimeError):
    pass


def _build_markdown(options: dict[str, Any], result: dict[str, Any]) -> str:
    model = result.get('model') or {}
    name = model.get('name') or options.get('name')
    model_type = model.get('type') or options.get('type')
    lines = [f'Model published: **{name}** ({model_type})']
    for item in result.get('versions') or []:
        lines.append(f'- {item.get("version")} · {item.get("base_model")} · {item.get("file_size_text")}')
    page_url = result.get('page_url')
    if page_url:
        lines.append(f'- My model page: {page_url}')
    lines.append('- After publishing it enters platform review; until approved it is visible only on your own model page and does not appear in community lists.')
    lines.append('- Uploading model files does not consume credits (only generation tasks do).')
    return '\n'.join(lines)


def cmd_model_publish(rest: list[str]) -> None:
    """Publish local model files: emits only a parameter card by default (exit code 3); --confirm-publish actually submits."""
    options = _parse_args(rest)
    versions = _normalize_versions(options)
    _validate(options, versions)

    if not options['confirm'] and not options['dry_run']:
        print(json.dumps(_plan_card(
            options, versions,
            note='Publishing uploads the local model files to BizyAir and creates a model under your account (private by default). Nothing is submitted without --confirm-publish.',
        ), ensure_ascii=False, indent=2))
        raise SystemExit(3)

    api_key = api.require_api_key(options['api_key'])

    warning = _base_model_warning(api_key, versions)

    if options['dry_run']:
        for index, item in enumerate(versions, start=1):
            print(f'[{index}/{len(versions)}] Computing signature for {Path(item["resolved_file"]).name} ...', file=sys.stderr)
            signature = api.model_file_signature(item['resolved_file'])
            item['sign'] = signature['sign']
        try:
            exists = api.model_exists(api_key, str(options['name']).strip(), str(options['type']).strip())
        except Exception:
            exists = False
        card = _plan_card(options, versions, note='--dry-run: local validation and signature computation only; nothing is uploaded and no model is created.')
        card['dry_run'] = True
        card['name_exists'] = exists
        if warning:
            card['base_model_warning'] = warning
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return

    if warning:
        print(warning, file=sys.stderr)
        raise SystemExit(2)

    name = str(options['name']).strip()
    model_type = str(options['type']).strip()
    if api.model_exists(api_key, name, model_type):
        print(f'A model with the same name already exists: {name} ({model_type}). Choose a different --name, or add a version under "my models".', file=sys.stderr)
        raise SystemExit(1)

    uploaded: list[dict[str, Any]] = []
    try:
        for index, item in enumerate(versions, start=1):
            _upload_one(api_key, item, model_type, index, len(versions),
                        concurrency=int(options['upload_concurrency']), restart=bool(options['restart_upload']))
            item['cover_url'] = _resolve_cover(api_key, item, index, len(versions), name=name, model_type=model_type)
            uploaded.append(item)
    except PublishError as exc:
        print(json.dumps({'ok': False, 'error': 'PUBLISH_FAILED', 'message': str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    except Exception as exc:  # network / OSS failure
        print(json.dumps({'ok': False, 'error': 'PUBLISH_FAILED', 'message': str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)

    payload_versions = [
        {
            'version': item['version'],
            'base_model': item['base_model'],
            'description': item['intro'],
            'public': item['public'],
            'sign': item['sign'],
            'path': item['resolved_file'],
            'cover_urls': [item['cover_url']],
        }
        for item in uploaded
    ]

    print(f'Creating model {name} ({model_type}, {len(payload_versions)} versions) ...', file=sys.stderr)
    create_resp = api.create_model(api_key, name=name, model_type=model_type, versions=payload_versions)

    if not api.result_is_ok(create_resp):
        message = api.request_error_message(create_resp) or 'Failed to create the model'
        payload = {'ok': False, 'error': 'PUBLISH_FAILED', 'message': message,
                   'request_body': {'name': name, 'type': model_type, 'versions': payload_versions}}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        raise SystemExit(1)

    data = api.extract_result_data(create_resp) or {}
    model_id = data.get('id')
    version_ids = data.get('version_ids') or []
    page_url = common.MY_MODEL_PAGE_URL.format(id=model_id) if model_id else None

    result: dict[str, Any] = {
        'ok': True,
        'model': {'id': model_id, 'name': name, 'type': model_type},
        'version_ids': version_ids,
        'page_url': page_url,
        'versions': [
            {
                'version': item['version'],
                'base_model': item['base_model'],
                'public': item['public'],
                'sign': item['sign'],
                'file': item['resolved_file'],
                'file_size': item['size'],
                'file_size_text': _fmt_size(item['size']),
                'cover_url': item['cover_url'],
            }
            for item in uploaded
        ],
        'reply_markdown': '',
    }
    result['reply_markdown'] = _build_markdown(options, result)

    if options['reply_format'] == 'json':
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result['reply_markdown'])
