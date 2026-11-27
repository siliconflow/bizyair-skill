#!/usr/bin/env python3
"""cli.py — unified CLI entry point for the BizyAir Skill.

Subcommands map to the matching features of dispatch.py / app.py.
The legacy entry points (dispatch.py / app.py) still work; this file is the new unified entry point.

Usage:
  python3 scripts/cli.py check
  python3 scripts/cli.py wallet
  python3 scripts/cli.py status <request_id>          # check task status once (no polling)
  python3 scripts/cli.py outputs <request_id> [-o dir]  # fetch outputs / download
  python3 scripts/cli.py upload <local file>            # local file -> OSS URL
  python3 scripts/cli.py image-menu
  python3 scripts/cli.py video-menu
  python3 scripts/cli.py browse --remote-source community --modality Application --sort "Most Used"
  python3 scripts/cli.py search "<keyword>" --remote
  python3 scripts/cli.py search-workflows "<keyword>" [--sort "Recently"] [--limit 20]
  python3 scripts/cli.py info <link_or_id>
  python3 scripts/cli.py download-workflow <link_or_id> [-o output path] [--version v1.0]
  python3 scripts/cli.py prefill <link_or_id> --prompt "xxx"
  python3 scripts/cli.py run <app_id> --prompt "xxx"
  python3 scripts/cli.py pick-image "<keyword>" --remote
  python3 scripts/cli.py pick-video "<keyword>" --remote
  python3 scripts/cli.py batch-prefill --model <route> --batch-json '[...]'
  python3 scripts/cli.py batch-prefill --model <route> --batch-json-file ./batch.json
  python3 scripts/cli.py batch-run --model <route> --batch-json '[...]' --confirm-run
  python3 scripts/cli.py batch-status [<batch-dir-or-batch-status.json>]
  python3 scripts/cli.py modelzoo-list --keyword "flux"
  python3 scripts/cli.py modelzoo-detail <endpoint>
  python3 scripts/cli.py modelzoo-price <endpoint>
  python3 scripts/cli.py modelzoo-run <endpoint> --param key=value [--image local.png --image-mode auto|base64|oss] --confirm-run
  python3 scripts/cli.py modelzoo-status <request_id>
  python3 scripts/cli.py modelzoo-md [--save] [--out dir]   # export the full ModelZoo catalog
  python3 scripts/cli.py app-md [--save] [--out dir]        # export the full community AI app catalog
  python3 scripts/cli.py pick-modelzoo-image "<keyword>"
  python3 scripts/cli.py pick-modelzoo-video "<keyword>"

Works gallery (new in v1.8.2):
  python3 scripts/cli.py gallery [--sort Recently|Recommend] [--limit 20]
  python3 scripts/cli.py gallery-search "<keyword>" [--sort Recently|Recommend]
  python3 scripts/cli.py gallery-search --model "qwen image 2.1" [--limit 10] [--max-scan 2] [--refresh]
  python3 scripts/cli.py gallery-detail <work ID or link>
  python3 scripts/cli.py gallery-app-works <AI app link or ID> [--page 1] [--limit 20]
  python3 scripts/cli.py gallery-like <work ID or link>
  python3 scripts/cli.py gallery-favorite <work ID or link>
  python3 scripts/cli.py gallery-unfavorite <work ID or link>
  python3 scripts/cli.py my-generated [--status all|published|unpublished] [--type WEBAPP,API] [--limit 20]
  python3 scripts/cli.py my-works                 # legacy entry: works I published
  python3 scripts/cli.py my-favorites             # legacy entry: works I forked

Personal center (restructured in v1.12.0, recommended):
  python3 scripts/cli.py my-profile                          # profile lookup
  python3 scripts/cli.py my-profile published [--kind creation|app|workflow|model|all]
  python3 scripts/cli.py my-profile forked    [--kind creation|app|workflow|model|all]
  python3 scripts/cli.py my-profile generated [--status all|published|unpublished]
  python3 scripts/cli.py my-profile edit --nickname "new nickname" [--description ...] [--avatar URL] [--homepage github=URL]
  python3 scripts/cli.py my-profile edit ... --confirm      # submit profile changes after confirming
  python3 scripts/cli.py my-profile share                    # share link for the personal page

Model hub / personal center / membership benefits (new in v1.9):
  python3 scripts/cli.py models ["keyword"] [--type LoRA] [--base-model Qwen-Image] [--source community|official]
  python3 scripts/cli.py model-detail <model ID or link>
  python3 scripts/cli.py my-models
  python3 scripts/cli.py model-types
  python3 scripts/cli.py profile [show|edit|share]   # legacy entry; edit/share behave like my-profile
  python3 scripts/cli.py plan [--with-tiers]
  python3 scripts/cli.py plan-tiers
  python3 scripts/cli.py notifications [--limit 20]
  python3 scripts/cli.py credits [--limit 20]

Model publishing (new in v1.10, uploads local model files):
  python3 scripts/cli.py model-repost --source-url https://huggingface.co/owner/repo --confirm-publish
  python3 scripts/cli.py model-publish -n model-name -t LoRA -p /path/a.safetensors \
    -b "FLUX.1 D" -cover cover.jpg -i "intro" [--public] [--dry-run] [--confirm-publish]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402

# Default truncation length for a single string value. Whole-JSON hard cuts are gone, so output stays parseable (see A2).
DEFAULT_VALUE_LIMIT = 4000


def _truncate_long_values(obj, limit: int | None):
    """Truncate only over-long string scalars so the overall JSON output always stays parseable."""
    if limit is None:
        return obj
    if isinstance(obj, str):
        if len(obj) <= limit:
            return obj
        return obj[:limit] + f'...[truncated {len(obj) - limit} characters; pass --full for the complete text]'
    if isinstance(obj, list):
        return [_truncate_long_values(item, limit) for item in obj]
    if isinstance(obj, dict):
        return {key: _truncate_long_values(value, limit) for key, value in obj.items()}
    return obj


def _dump_json(data, *, value_limit: int | None=DEFAULT_VALUE_LIMIT):
    print(json.dumps(_truncate_long_values(data, value_limit), ensure_ascii=False, indent=2))


def _format_price_tables(tables: list) -> str:
    """Render price_tables as one line of readable prices.

    Raw values in the international-site price table are 1/1000 USD (1,000 = $1); the official
    price page shows USD per run. Only USD is emitted here, to avoid confusion with the credits
    terminology used for wallet balances.
    For example "output (resolution): 1k=$0.028/run, 1.5k=$0.048/run".
    """
    parts: list[str] = []
    for table in tables or []:
        if not isinstance(table, dict):
            continue
        columns = [str(c.get('field_label') or c.get('variable_name') or '') for c in (table.get('columns') or []) if isinstance(c, dict)]
        columns = [c for c in columns if c]
        cells = table.get('cells_v2') or []
        values = table.get('pricing_values') or []
        pricing_name = str(table.get('pricing_name') or 'price')
        unit = str(table.get('unit_name') or 'run')
        segments: list[str] = []
        for index, value in enumerate(values):
            label = None
            if index < len(cells):
                row = cells[index]
                if isinstance(row, list) and row:
                    label = ' / '.join(str(cell) for cell in row)
                elif row not in (None, ''):
                    label = str(row)
            usd_text = common.format_usd(value)
            price_text = usd_text or str(value)
            segment = f'{label}={price_text}/{unit}' if label else f'{price_text}/{unit}'
            segments.append(segment)
        head = f"{pricing_name} ({' · '.join(columns)})" if columns else pricing_name
        parts.append(f"{head}: {', '.join(segments)}")
    return ' | '.join(parts)


def _run_script_command(module_name: str, flags: list[str], rest: list[str]) -> None:
    """Run a legacy argparse entry point with a fixed flag prefix."""
    import importlib
    script_name = 'app.py' if module_name == 'app' else f'{module_name}.py'
    sys.argv = [str(SCRIPT_DIR / script_name), *flags, *rest]
    importlib.import_module(module_name).main()


def _run_cli_command(module_name: str | None, function_name: str, kwargs: dict, rest: list[str]) -> None:
    """Run a command function that already accepts a raw rest list."""
    if module_name:
        import importlib
        module = importlib.import_module(module_name)
        handler = getattr(module, function_name)
    else:
        handler = globals()[function_name]
    handler(rest, **kwargs)


# Commands implemented by an existing script entry point. Adding a new alias is a one-line table edit.
_SCRIPT_COMMANDS: dict[str, tuple[str, list[str]]] = {
    'check': ('dispatch', ['--check']),
    'wallet': ('dispatch', ['--wallet']),
    'image-menu': ('dispatch', ['--image-menu']),
    'video-menu': ('dispatch', ['--video-menu']),
    'batch-prefill': ('dispatch', ['--batch-prefill']),
    'batch-run': ('dispatch', ['--batch-run']),
    'batch-status': ('dispatch', ['--batch-status']),
    'info': ('app', ['--info-from-link']),
    'browse': ('app', ['--browse', '--remote']),
    'search': ('app', ['--search']),
    'prefill': ('app', ['--prefill-card']),
    'run': ('app', ['--run']),
    'pick-image': ('app', ['--pick-image-candidates']),
    'pick-video': ('app', ['--pick-video-candidates']),
}

# Commands implemented by a module-level function that takes (rest, **kwargs).
_CLI_COMMANDS: dict[str, tuple[str | None, str, dict]] = {
    'search-workflows': (None, '_search_workflows', {}),
    'download-workflow': ('workflow_download', 'main', {}),
    'modelzoo-list': (None, '_modelzoo_list', {}),
    'modelzoo-detail': (None, '_modelzoo_detail', {}),
    'modelzoo-price': (None, '_modelzoo_price', {}),
    'modelzoo-run': (None, '_modelzoo_run', {}),
    'modelzoo-status': (None, '_modelzoo_status', {}),
    'pick-modelzoo-image': (None, '_modelzoo_pick', {'modality': 'image'}),
    'pick-modelzoo-video': (None, '_modelzoo_pick', {'modality': 'video'}),
    'gallery': ('gallery', 'cmd_gallery', {}),
    'gallery-search': ('gallery', 'cmd_gallery', {}),
    'gallery-app-works': ('gallery', 'cmd_gallery_app_works', {}),
    'app-works': ('gallery', 'cmd_gallery_app_works', {}),
    'gallery-detail': ('gallery', 'cmd_gallery_detail', {}),
    'gallery-like': ('gallery', 'cmd_gallery_like', {}),
    'gallery-favorite': ('gallery', 'cmd_gallery_favorite', {}),
    'gallery-unfavorite': ('gallery', 'cmd_gallery_unfavorite', {}),
    'modelzoo-md': ('catalog', 'cmd_modelzoo_md', {}),
    'app-md': ('catalog', 'cmd_app_md', {}),
    'status': ('tasks', 'cmd_status', {}),
    'outputs': ('tasks', 'cmd_outputs', {}),
    'upload': ('tasks', 'cmd_upload', {}),
    'my-generated': ('gallery', 'cmd_my_generated', {}),
    'my-works': ('gallery', 'cmd_gallery', {'mine': True}),
    'my-favorites': ('gallery', 'cmd_gallery', {'favorites': True}),
    'models': ('hub', 'cmd_models', {}),
    'model-detail': ('hub', 'cmd_model_detail', {}),
    'my-models': ('hub', 'cmd_my_models', {}),
    'model-types': ('hub', 'cmd_model_types', {}),
    'my-profile': ('personal', 'cmd_my_profile', {}),
    'profile': ('personal', 'cmd_profile', {}),
    'plan': ('account', 'cmd_plan', {}),
    'plan-tiers': ('account', 'cmd_plan_tiers', {}),
    'notifications': ('account', 'cmd_notifications', {}),
    'credits': ('account', 'cmd_credits', {}),
    'model-publish': ('publish', 'cmd_model_publish', {}),
    'model-repost': ('repost', 'cmd_model_repost', {}),
}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]
    rest = sys.argv[2:]

    if cmd in {'help', '--help', '-h'}:
        print(__doc__)
        return

    if cmd == 'gallery-publish':
        import gallery
        gallery.reject_gallery_publish()

    spec = _SCRIPT_COMMANDS.get(cmd)
    if spec:
        module_name, flags = spec
        _run_script_command(module_name, flags, rest)
        return

    spec = _CLI_COMMANDS.get(cmd)
    if spec:
        module_name, function_name, kwargs = spec
        _run_cli_command(module_name, function_name, kwargs, rest)
        return

    print(f"Unknown command: {cmd}")
    print(__doc__)
    sys.exit(2)


# ---- Workflow-specific search ----

def _search_workflows(rest: list[str]):
    """Exact workflow-specific search (one keyword, one round, returns workflows only)."""
    import search

    query = ""

    remote_source = "community"
    sort = None
    page = 1
    page_size = 50
    limit = 20
    base_models = None
    reply_format = "json"
    api_key = None

    i = 0
    while i < len(rest):
        flag = rest[i]
        if flag in {
            "--remote-source", "--sort", "--page", "--page-size",
            "--limit", "--base-models", "--reply-format", "--api-key",
        }:
            if i + 1 >= len(rest):
                print(f"Parameter {flag} is missing a value", file=sys.stderr)
                sys.exit(2)
            value = rest[i + 1]
            if flag == "--remote-source":
                remote_source = value
            elif flag == "--sort":
                sort = value
            elif flag == "--page":
                try:
                    page = int(value)
                except ValueError:
                    print("--page must be an integer", file=sys.stderr)
                    sys.exit(2)
            elif flag == "--page-size":
                try:
                    page_size = int(value)
                except ValueError:
                    print("--page-size must be an integer", file=sys.stderr)
                    sys.exit(2)
            elif flag == "--limit":
                try:
                    limit = int(value)
                except ValueError:
                    print("--limit must be an integer", file=sys.stderr)
                    sys.exit(2)
            elif flag == "--base-models":
                base_models = value
            elif flag == "--reply-format":
                reply_format = value
            elif flag == "--api-key":
                api_key = value
            i += 2
            continue
        if not flag.startswith("--") and not query:
            query = flag
        i += 1

    if not query:
        print('Usage: cli.py search-workflows "<keyword>" [--remote-source community|official] '
              '[--sort "Recently"] [--limit 20] [--page-size 50] [--base-models "Qwen-Image"] '
              '[--reply-format json|markdown]', file=sys.stderr)
        sys.exit(2)

    if remote_source not in {"community", "official"}:
        print("--remote-source only supports community or official", file=sys.stderr)
        sys.exit(2)
    if reply_format not in {"json", "markdown"}:
        print("--reply-format only supports json or markdown", file=sys.stderr)
        sys.exit(2)

    import remote
    resolved_sort = remote.resolve_list_sort(sort, remote_source=remote_source, default='Recently')
    if resolved_sort not in remote.SUPPORTED_SORTS:
        print(json.dumps({
            'error': 'SORT_NOT_SUPPORTED',
            'message': 'The public list supports only --sort "Most Used" / Recently / "Most Liked" / Auto.',
            'given': sort,
            'supported': sorted(remote.SUPPORTED_SORTS),
        }, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
    for flag, value in (('--page', page), ('--page-size', page_size), ('--limit', limit)):
        if value < 1:
            print(f'{flag} must be an integer >= 1', file=sys.stderr)
            sys.exit(2)

    search.cmd_search_workflows(
        query,
        api_key_arg=api_key,
        remote_source=remote_source,
        page=page,
        page_size=page_size,
        sort=resolved_sort,
        base_models=base_models,
        limit=limit,
        reply_format=reply_format,
    )


# ---- ModelZoo subcommand implementations ----

def _get_key(rest: list[str]) -> str:
    import api
    key_arg = None
    if "--api-key" in rest:
        idx = rest.index("--api-key")
        if idx + 1 < len(rest):
            key_arg = rest[idx + 1]
    return api.require_api_key(key_arg)


def _modelzoo_list(rest: list[str]):
    import modelzoo
    import api as _api

    key = _get_key(rest)
    keyword = ""
    if rest and not rest[0].startswith("--"):
        keyword = rest[0]
    elif "--keyword" in rest:
        idx = rest.index("--keyword")
        if idx + 1 < len(rest):
            keyword = rest[idx + 1]

    result = _api.require_ok(modelzoo.list_endpoints(key, keyword=keyword), 'modelzoo-list')
    data = (result.get("data") or {}).get("data") or result.get("data") or {}
    items = data.get("list") or []
    print(f"{data.get('total', len(items))} endpoints in total:")
    for item in items:
        print(f"  {item.get('endpoint'):50} | {item.get('display_name')} | {item.get('category')}")


def _modelzoo_detail(rest: list[str]):
    import modelzoo
    import api as _api

    endpoint = rest[0] if rest and not rest[0].startswith("--") else ""
    if not endpoint:
        print("Usage: cli.py modelzoo-detail <endpoint> [--full]")
        sys.exit(2)

    key = _get_key(rest)
    result = _api.require_ok(modelzoo.get_detail(key, endpoint), 'modelzoo-detail')
    data = (result.get("data") or {}).get("data") or result.get("data") or {}
    _dump_json(data, value_limit=None if "--full" in rest else DEFAULT_VALUE_LIMIT)


def _modelzoo_price(rest: list[str]):
    import modelzoo
    import api as _api

    endpoint = rest[0] if rest and not rest[0].startswith("--") else ""
    if not endpoint:
        print("Usage: cli.py modelzoo-price <endpoint> [--full]")
        sys.exit(2)

    key = _get_key(rest)
    result = _api.require_ok(modelzoo.get_price(key, endpoint), 'modelzoo-price')
    data = (result.get("data") or {}).get("data") or result.get("data") or {}
    tables = data.get("price_tables") or []
    simple = (data.get("price_table") or {}).get("simple_price_text") or (_format_price_tables(tables) if tables else None)
    if simple:
        print(f"Price: {simple}")
    if "--full" in rest or not simple:
        _dump_json(data, value_limit=None if "--full" in rest else DEFAULT_VALUE_LIMIT)


def _emit_param_error(exc):
    payload = {
        "error": "INVALID_PARAM",
        "message": str(exc) or "invalid parameter",
        "issues": getattr(exc, "issues", []),
    }
    available = getattr(exc, "available_params", None)
    if available:
        payload["available_params"] = available
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    sys.exit(2)


def _modelzoo_run(rest: list[str]):
    import modelzoo
    import api as _api

    endpoint = rest[0] if rest and not rest[0].startswith("--") else ""
    if not endpoint:
        print("Usage: cli.py modelzoo-run <endpoint> --param key=value [--image path ...] [--audio path ...] [--video path ...] [--output DIR] --confirm-run")
        sys.exit(2)

    key = _get_key(rest)

    # Parse --param / --image / --audio / --video / --output
    params: dict[str, str] = {}
    images: list[str] = []
    audios: list[str] = []
    videos: list[str] = []
    output = None
    image_mode = "auto"
    i = 1
    while i < len(rest):
        if rest[i] == "--param" and i + 1 < len(rest):
            kv = rest[i + 1]
            parsed, issues = modelzoo.parse_param_pairs([kv])
            if issues:
                _emit_param_error(modelzoo.ParamError(issues))
            params.update(parsed)
            i += 2
        elif rest[i] == "--image" and i + 1 < len(rest):
            images.append(rest[i + 1])
            i += 2
        elif rest[i] == "--image-mode" and i + 1 < len(rest):
            image_mode = rest[i + 1]
            i += 2
        elif rest[i] == "--audio" and i + 1 < len(rest):
            audios.append(rest[i + 1])
            i += 2
        elif rest[i] == "--video" and i + 1 < len(rest):
            videos.append(rest[i + 1])
            i += 2
        elif rest[i] in ("--output", "-o") and i + 1 < len(rest):
            output = rest[i + 1]
            i += 2
        else:
            i += 1

    if image_mode not in ("auto", "base64", "oss"):
        print(json.dumps({
            "error": "INVALID_IMAGE_MODE",
            "message": f"--image-mode only supports auto / base64 / oss, got: {image_mode}",
        }, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)

    # Validate parameters against the detail (unknown parameters / illegal combos / missing prompt are all caught before submitting)
    detail_result = _api.require_ok(modelzoo.get_detail(key, endpoint), 'modelzoo-run detail')
    detail_data = (detail_result.get("data") or {}).get("data") or detail_result.get("data") or {}
    if not (detail_data.get("input_params") or []):
        print(json.dumps({
            "error": "MODELZOO_DETAIL_EMPTY",
            "message": f"Cannot fetch the parameter definition for {endpoint}; the task was not submitted",
            "endpoint": endpoint,
        }, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    supplied_media: set[str] = set()
    if images:
        supplied_media.add("images")
    if audios:
        supplied_media.add("audios")
    if videos:
        supplied_media.add("videos")
    normalized_params, issues = modelzoo.validate_user_params(
        detail_data,
        params,
        supplied_media_types=supplied_media,
    )
    if issues:
        _emit_param_error(modelzoo.ParamError(issues))
    params = normalized_params

    if "--confirm-run" not in rest:
        payload = modelzoo.build_task_payload(detail_data, params)
        # Compute red line: without explicit confirmation, emit only the parameter card and never create a task.
        print("🧾 **ModelZoo prefill confirmation card (not submitted yet)**")
        print(f"- **endpoint**: `{endpoint}`")
        print(f"- **Parameters**: {json.dumps(payload, ensure_ascii=False)[:2000]}")
        if images or audios or videos:
            print(f"- **Media assets**: {json.dumps({'images': len(images), 'audios': len(audios), 'videos': len(videos)}, ensure_ascii=False)}")
        print("")
        print("If everything looks right, append `--confirm-run` to the end of the same command and run it again to submit (billed at platform prices).")
        sys.exit(3)

    # Upload media files -> build media_overrides (replaces the default sample assets wholesale)
    media_overrides: dict[str, list[str]] = {}
    if images:
        media_overrides["images"] = [_api.media_input_value(key, p, mode=image_mode, kind='image') for p in images]
    if audios:
        media_overrides["audios"] = [_api.media_input_value(key, p, mode='oss', kind='audio') for p in audios]
    if videos:
        media_overrides["videos"] = [_api.media_input_value(key, p, mode='oss', kind='video') for p in videos]

    payload = modelzoo.build_task_payload(detail_data, params, media_overrides=media_overrides)
    if not payload:
        print(json.dumps({
            "error": "MODELZOO_EMPTY_PAYLOAD",
            "message": "Parameters are empty; the task was not submitted",
            "endpoint": endpoint,
        }, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    print(f"Submitting task: {endpoint}")
    print(f"Parameters: {json.dumps(payload, ensure_ascii=False)[:500]}")

    create_result = _api.require_ok(modelzoo.create_task(key, endpoint, payload), 'modelzoo-run create')
    create_data = (create_result.get("data") or {}).get("data") or create_result.get("data") or {}
    request_id = create_data.get("request_id")

    if not request_id:
        print(f"Create failed: {json.dumps(create_result, ensure_ascii=False)[:500]}")
        sys.exit(1)

    print(f"request_id: {request_id}")
    print("Polling...")

    final = modelzoo.poll_until_done(key, request_id)
    status = final.get("status")
    outputs = final.get("outputs") or {}

    if status == "Success":
        manifest = modelzoo.save_task_outputs(
            outputs,
            output,
            endpoint=endpoint,
            request_id=request_id,
            prompt=params.get("prompt"),
            params=payload,
        )
        print(f"\n✅ Success!")
        for media_type in ("texts", "images", "videos", "audios"):
            items = outputs.get(media_type) or []
            for url in items:
                print(f"  [{media_type}] {url}")
        for saved in manifest.get("saved_files") or []:
            print(f"  [saved] {saved}")
        if manifest.get("manifest_path"):
            print(f"  [manifest] {manifest['manifest_path']}")
        for error in manifest.get("download_errors") or []:
            print(f"  [download_error] {json.dumps(error, ensure_ascii=False)}", file=sys.stderr)
    else:
        print(f"\n❌ {status}: {final.get('message', '')}")
        sys.exit(1)


def _modelzoo_status(rest: list[str]):
    import modelzoo
    import api as _api

    request_id = rest[0] if rest and not rest[0].startswith("--") else ""
    if not request_id:
        print("Usage: cli.py modelzoo-status <request_id> [--full]")
        sys.exit(2)

    key = _get_key(rest)
    result = _api.require_ok(modelzoo.query_task(key, request_id), 'modelzoo-status')
    data = (result.get("data") or {}).get("data") or result.get("data") or {}
    _dump_json(data, value_limit=None if "--full" in rest else DEFAULT_VALUE_LIMIT)


def _modelzoo_pick(rest: list[str], *, modality: str):
    """ModelZoo endpoint candidate search (menu 6 / v6).

    Supports --reply-format json|markdown, default json (compatible with the legacy picker output format).
    --limit caps the number of candidates.
    """
    import modelzoo

    query = ""
    if rest and not rest[0].startswith("--"):
        query = rest[0]

    # Simple flag parsing (matching the existing _modelzoo_run style; no argparse)
    reply_format = "json"
    limit = 10
    i = 0
    while i < len(rest):
        if rest[i] == "--reply-format" and i + 1 < len(rest):
            reply_format = rest[i + 1]
            i += 2
        elif rest[i] == "--limit" and i + 1 < len(rest):
            try:
                # Cap at 30: the picker no longer filters by modality and recalls everything, so avoid output blowups
                limit = max(1, min(30, int(rest[i + 1])))
            except ValueError:
                pass
            i += 2
        else:
            i += 1

    if not query:
        print("Usage: cli.py pick-modelzoo-image|pick-modelzoo-video \"<keyword>\" [--limit N] [--reply-format json|markdown]")
        sys.exit(2)

    key = _get_key(rest)
    payload = modelzoo.pick_endpoint_candidates(key, query, modality, limit=limit)
    if payload.get("error"):
        # A failed search request must not masquerade as "no candidates", or the agent will
        # spin and retry on an empty reply_markdown.
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    if reply_format == "markdown":
        print(payload.get("reply_markdown") or "")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
