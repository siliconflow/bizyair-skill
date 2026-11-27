#!/usr/bin/env python3
"""repost.py — move third-party site models to BizyAir with the official bizyair-community CLI (new in v1.10.2).

Division of labor:
  - `model-publish` (publish.py): local model files (.safetensors etc.) -> upload -> create model
  - `model-repost` (this file): Civitai / HuggingFace / ModelScope links -> call the official CLI to repost

The official CLI does not accept local paths (`unsupported Civitai host ""`), and this skill does
not accept third-party URLs; the two routes complement each other and the command layer routes by input source.

This file does exactly four things:
  1. Parameter normalization + source inference (civitai / huggingface / modelscope)
  2. A second confirmation gate (without --confirm-publish it only emits a parameter card, exit code 3)
  3. Call `bizyair-community repost`, stream stdout to stderr live and parse milestones
  4. Emit unified JSON + reply_markdown (consistent with the skill's other commands)

The official CLI's `--json` is ineffective in testing (still plain text), so milestones rely on text matching:
  - `discovered model "NAME" with N version(s)`
  - `published model id=ID version_ids=1,2`
  - `already published, skipping`
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import api
import common

INSTALL_COMMAND = 'curl -fsSL https://storage.bizyair.ai/cli-community/install.sh | sh'
BINARY_NAME = 'bizyair-community'
SOURCES = {'civitai', 'huggingface', 'modelscope'}
HOST_SOURCES = {
    'civitai.com': 'civitai',
    'www.civitai.com': 'civitai',
    'huggingface.co': 'huggingface',
    'www.huggingface.co': 'huggingface',
    'modelscope.cn': 'modelscope',
    'www.modelscope.cn': 'modelscope',
    'modelscope.ai': 'modelscope',
    'www.modelscope.ai': 'modelscope',
}
DEFAULT_MAX_DESCRIPTION = 5000


class RepostError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Locating the CLI binary
# ---------------------------------------------------------------------------

def find_binary() -> str | None:
    """Find the official CLI via PATH -> ~/bizyair-community -> ~/.local/bin -> ~/bin."""
    found = shutil.which(BINARY_NAME)
    if found:
        return found
    for candidate in (
        Path.home() / BINARY_NAME,
        Path.home() / '.local' / 'bin' / BINARY_NAME,
        Path.home() / 'bin' / BINARY_NAME,
        Path('/usr/local/bin') / BINARY_NAME,
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def missing_binary_payload(reply_format: str = 'markdown') -> dict[str, Any]:
    payload: dict[str, Any] = {
        'status': 'failed',
        'action': 'model-repost',
        'error': 'BINARY_NOT_FOUND',
        'message': f'{BINARY_NAME} (the official repost CLI) was not found. Install it first, then re-run this command.',
        'install': INSTALL_COMMAND,
        'install_note': 'The install script picks mac/linux x amd64/arm64, verifies SHA256 and installs into ~/.local/bin or ~/bin; if already installed, upgrade with `bizyair-community upgrade`.',
    }
    payload['reply_markdown'] = (
        f'**{BINARY_NAME} is missing**\n\n'
        f'Third-party reposting is done by the official CLI, which is not installed on this machine. Run:\n\n'
        f'```bash\n{INSTALL_COMMAND}\n```\n\n'
        f'Once `{BINARY_NAME} --version` prints a version, re-run this command.'
    )
    return payload


# ---------------------------------------------------------------------------
# Parameters and source
# ---------------------------------------------------------------------------

def infer_source(url: str) -> str | None:
    """Infer the official `--source` from the URL host. Returns None for local paths."""
    raw = str(url or '').strip()
    if not raw:
        return None
    if '://' not in raw:
        return None
    host = raw.split('://', 1)[1].split('/', 1)[0].split('?', 1)[0].lower()
    host = host.split('@')[-1].split(':')[0]
    return HOST_SOURCES.get(host)


def _take_value(rest: list[str], index: int, flag: str) -> str:
    if index + 1 >= len(rest):
        print(f'Parameter {flag} is missing a value', file=sys.stderr)
        raise SystemExit(2)
    return rest[index + 1]


def _parse_args(rest: list[str]) -> dict[str, Any]:
    options: dict[str, Any] = {
        'source_url': '', 'source': None, 'name': None, 'type': None, 'base_model': None,
        'public': False, 'generate_covers': False, 'cover_model': None,
        'max_description_length': DEFAULT_MAX_DESCRIPTION,
        'work_dir': None, 'source_api_key': None, 'allow_nsfw_covers': False,
        'concurrency': None, 'fail_fast': False,
        'confirm': False, 'api_key': None, 'reply_format': 'markdown',
    }
    value_flags = {
        '--source-url': 'source_url', '--source': 'source', '--name': 'name', '-n': 'name',
        '--model-name': 'name', '--type': 'type', '-t': 'type',
        '--base-model': 'base_model', '-b': 'base_model',
        '--cover-model': 'cover_model', '--max-description-length': 'max_description_length',
        '--work-dir': 'work_dir', '--source-api-key': 'source_api_key',
        '--concurrency': 'concurrency', '--api-key': 'api_key', '--reply-format': 'reply_format',
    }
    bool_flags = {
        '--public': 'public', '--generate-covers': 'generate_covers',
        '--allow-nsfw-covers': 'allow_nsfw_covers', '--fail-fast': 'fail_fast',
        '--confirm-publish': 'confirm',
    }
    index = 0
    while index < len(rest):
        flag = rest[index]
        if flag in value_flags:
            options[value_flags[flag]] = _take_value(rest, index, flag)
            index += 2
            continue
        if flag in bool_flags:
            options[bool_flags[flag]] = True
            index += 1
            continue
        if not flag.startswith('--') and not options['source_url']:
            options['source_url'] = flag
        index += 1
    if options['reply_format'] not in {'json', 'markdown'}:
        print('--reply-format only supports json or markdown', file=sys.stderr)
        raise SystemExit(2)
    return options


def validate(options: dict[str, Any]) -> str:
    url = str(options['source_url'] or '').strip()
    if not url:
        print('Missing --source-url (a Civitai / HuggingFace / ModelScope link)', file=sys.stderr)
        raise SystemExit(2)
    if '://' not in url:
        print(
            'Use model-publish for local files (this command only handles third-party links: civitai.com / huggingface.co / modelscope.cn)',
            file=sys.stderr,
        )
        raise SystemExit(2)
    source = options['source'] or infer_source(url)
    if not source:
        host = url.split('://', 1)[1].split('/', 1)[0]
        print(f'Unknown model source: {host} (supported: civitai.com / huggingface.co / modelscope.cn)', file=sys.stderr)
        raise SystemExit(2)
    if source not in SOURCES:
        print(f'--source must be one of {" / ".join(sorted(SOURCES))}', file=sys.stderr)
        raise SystemExit(2)
    if options['cover_model'] and not options['generate_covers']:
        print('--cover-model also requires --generate-covers', file=sys.stderr)
        raise SystemExit(2)
    return source


def build_command(binary: str, options: dict[str, Any], source: str) -> list[str]:
    cmd = [binary, 'repost', '--source', source, '--source-url', str(options['source_url']).strip()]
    if options['name']:
        cmd += ['--model-name', str(options['name'])]
    if options['type']:
        cmd += ['--type', str(options['type'])]
    if options['base_model']:
        cmd += ['--base-model', str(options['base_model'])]
    if options['public']:
        cmd.append('--publish-visible')
    if options['generate_covers']:
        cmd.append('--generate-covers')
    if options['cover_model']:
        cmd += ['--cover-model', str(options['cover_model'])]
    if options['max_description_length']:
        cmd += ['--max-description-length', str(options['max_description_length'])]
    if options['work_dir']:
        cmd += ['--work-dir', str(options['work_dir'])]
    if options['source_api_key']:
        cmd += ['--source-api-key', str(options['source_api_key'])]
    if options['allow_nsfw_covers']:
        cmd.append('--allow-nsfw-covers')
    if options['concurrency']:
        cmd += ['--concurrency', str(options['concurrency'])]
    if options['fail_fast']:
        cmd.append('--fail-fast')
    return cmd


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

def parse_output(lines: list[str]) -> dict[str, Any]:
    """Parse milestones out of the official CLI's text output."""
    info: dict[str, Any] = {
        'model_name': None, 'version_count': None,
        'published': False, 'skipped': False,
        'model_id': None, 'version_ids': [], 'errors': [],
    }
    for line in lines:
        text = line.strip()
        if not text:
            continue
        lower = text.lower()
        if 'discovered model' in lower and '"' in text:
            head = text.split('"', 1)[1]
            name = head.split('"', 1)[0]
            info['model_name'] = name or info['model_name']
            for token in text.replace('(', ' ').replace(')', ' ').split():
                if token.isdigit():
                    info['version_count'] = int(token)
        if 'published model id=' in lower:
            info['published'] = True
            tail = text.split('id=', 1)[1]
            id_part = tail.split()[0].rstrip(',')
            try:
                info['model_id'] = int(id_part)
            except ValueError:
                info['model_id'] = id_part
            if 'version_ids=' in tail:
                ids_part = tail.split('version_ids=', 1)[1].split()[0]
                info['version_ids'] = [int(piece) for piece in ids_part.split(',') if piece.strip().isdigit()]
        if 'already published, skipping' in lower:
            info['skipped'] = True
        if lower.startswith('error') or ' failed' in lower:
            info['errors'].append(text)
    return info


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _gate_payload(options: dict[str, Any], source: str, reply_format: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'status': 'pending_confirmation',
        'action': 'model-repost',
        'request_body': {
            'source': source,
            'source_url': str(options['source_url']).strip(),
            'name': options['name'],
            'type': options['type'],
            'base_model': options['base_model'],
            'public': bool(options['public']),
            'generate_covers': bool(options['generate_covers']),
            'cover_model': options['cover_model'] if options['generate_covers'] else None,
            'max_description_length': options['max_description_length'],
            'work_dir': options['work_dir'],
        },
        'note': 'repost downloads the source model files, uploads them to BizyAir and creates a model under your account (private by default). Nothing runs without --confirm-publish.',
        'next': 'To confirm, append --confirm-publish to the end of the same command and submit.',
    }
    payload['reply_markdown'] = (
        f"**About to repost a model** ({source})\n\n"
        f"- Source link: {payload['request_body']['source_url']}\n"
        + (f"- Model name: {options['name']}\n" if options['name'] else '')
        + (f"- Type: {options['type']}\n" if options['type'] else '')
        + (f"- Base model: {options['base_model']}\n" if options['base_model'] else '')
        + f"- Public: {'yes' if options['public'] else 'no (private by default)'}\n"
        + (f"- Auto-generate cover: yes ({options['cover_model']})\n" if options['generate_covers'] else '')
        + '\nDownload + upload may take a few minutes (depending on source file size). If everything looks right, append `--confirm-publish` and run it again.'
    )
    return payload


def _success_markdown(payload: dict[str, Any]) -> str:
    if payload.get('skipped'):
        return (
            f"**A model with the same name already exists; repost skipped** (already published, skipping)\n\n"
            f"- Source link: {payload.get('source_url')}\n"
            + (f"- Model name: {payload['model'].get('name')}\n" if payload.get('model', {}).get('name') else '')
            + '\nWhen the platform already has a model with the same name and type, the official CLI skips it. '
              'To repost: delete the old model in `my-models`, or pass `--name` with a different name.'
        )
    model = payload.get('model') or {}
    lines = [f"Model reposted: **{model.get('name') or '(name not parsed)'}** ({payload.get('source')})"]
    if model.get('id'):
        lines.append(f"- Model id: {model['id']}")
    if model.get('version_ids'):
        lines.append(f"- Version ids: {', '.join(str(v) for v in model['version_ids'])} ({len(model['version_ids'])} in total)")
    if model.get('version_count'):
        lines.append(f"- Source version count: {model['version_count']}")
    if model.get('page_url'):
        lines.append(f"- My model page: {model['page_url']}")
    lines.append('- After publishing it enters platform review; until approved it is visible only on your own model page and does not appear in community lists.')
    lines.append('- Reposting and uploading do not consume credits (only generation tasks do).')
    return '\n'.join(lines)


def _failed_markdown(payload: dict[str, Any]) -> str:
    lines = ['**Repost failed**', '']
    message = payload.get('message') or 'the official CLI returned a non-zero exit code'
    lines.append(message)
    hints = payload.get('hints') or []
    if hints:
        lines.append('')
        lines.extend(f'- {hint}' for hint in hints)
    tail = payload.get('log_tail') or []
    if tail:
        lines.extend(['', 'Recent output:', '```'])
        lines.extend(tail[-8:])
        lines.append('```')
    return '\n'.join(lines)


def error_hints(log_tail: list[str]) -> list[str]:
    text = '\n'.join(log_tail).lower()
    hints: list[str] = []
    if 'cannot infer base model' in text:
        hints.append('Cannot infer the base model: add `--base-model "FLUX.1 D"` (must be a platform dictionary value; look it up with the dictionary command outside `cli.py info`).')
    if 'no cover images' in text:
        hints.append('The source site has no usable cover: add `--generate-covers` (this calls a text-to-image model and consumes credits).')
    if 'invalid --type' in text:
        hints.append('Type spelling must match the platform enum: `Controlnet`, not `ControlNet`.')
    if 'api key is invalid' in text or ' 401' in text:
        hints.append('Invalid API key: check `config.json` or `--api-key`; reposting needs a key with publish permission.')
    if 'already published' in text:
        hints.append('A model with the same name already exists: delete the old model first, or pass a different `--name`.')
    return hints


def cmd_model_repost(rest: list[str]) -> None:
    options = _parse_args(rest)
    source = validate(options)
    reply_format = options['reply_format']

    if not options['confirm']:
        payload = _gate_payload(options, source, reply_format)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        raise SystemExit(3)

    api_key = api.require_api_key(options['api_key'])
    binary = find_binary()
    if not binary:
        payload = missing_binary_payload(reply_format)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        raise SystemExit(1)

    cmd = build_command(binary, options, source)
    env = dict(os.environ)
    env['BIZYAIR_PUBLISH_API_KEY'] = api_key
    env.setdefault('BIZYAIR_API_BASE_URL', 'https://bizyair.ai')

    print(f'Calling the official CLI: {binary} repost ({source}) ...', file=sys.stderr)
    lines: list[str] = []
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.rstrip('\n')
        lines.append(line)
        print(line, file=sys.stderr)
    code = process.wait()

    info = parse_output(lines)
    model: dict[str, Any] = {
        'name': info.get('model_name') or options['name'],
        'id': info.get('model_id'),
        'version_ids': info.get('version_ids') or [],
        'version_count': info.get('version_count'),
    }
    if model['id'] is not None and str(model['id']).isdigit():
        model['page_url'] = common.MY_MODEL_PAGE_URL.format(id=model['id'])

    payload: dict[str, Any] = {
        'action': 'model-repost',
        'source': source,
        'source_url': str(options['source_url']).strip(),
        'model': model,
        'skipped': bool(info.get('skipped')),
        'published': bool(info.get('published')),
        'public': bool(options['public']),
        'binary': binary,
        'log_tail': lines[-20:],
    }
    success = code == 0 and (info['published'] or info['skipped'])
    if success:
        payload['status'] = 'skipped' if info['skipped'] else 'published'
        payload['reply_markdown'] = _success_markdown(payload)
        if reply_format == 'json':
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(payload['reply_markdown'])
        return

    payload['status'] = 'failed'
    payload['exit_code'] = code
    payload['message'] = info['errors'][-1] if info['errors'] else (lines[-1] if lines else 'the official CLI produced no output')
    payload['hints'] = error_hints(lines)
    payload['reply_markdown'] = _failed_markdown(payload)
    if reply_format == 'json':
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload['reply_markdown'])
    raise SystemExit(1)
