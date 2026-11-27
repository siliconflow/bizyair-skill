"""dispatch.py — 10 fixed models (5 image + 5 video) + ModelZoo search + AI app search + batch entry.

Main flow (main() runs top to bottom):
  --image-menu / --video-menu        print the local menu copy
  --clear-runtime-state              clear resume cache
  --batch-watchdog-file <file>       start as a batch watchdog child process (hidden entry)
  --batch-worker-file <file>         start as a batch worker child process (spawned by batch.py)
  --batch-json + --batch-prefill/run batch entry; delegates to batch.handle_batch_*
  --check / --wallet / --browse / --search / --info  shell out to app.py
  --param-help / --prefill-card      render a confirmation card (ROUTE_TABLE models use ModelZoo)
  --model X (default)                resolve -> fixed models run directly through ModelZoo

All 10 fixed models (ROUTE_TABLE) run through the standard ModelZoo API; they no longer shell out to app.py.
Non-fixed models (AI app links / IDs) still use app.py's webapp path.
ModelZoo search routes go through pick-modelzoo-*; AI app search routes (remote-search-flow / -video) go through
pick-image / pick-video --remote, and the server filters by Application + Workflow.

[⚠️ args.scene handling order]
args.scene is parsed at L155-156 (scene -> model). Does this belong before or after the batch path?
Current design:
  - single-task path parses at L155
  - batch path goes through batch.build_task_args, which falls back to base_args.scene (fixed once)
Previously the batch path did not read base_args.scene, so `--scene 5 --batch-prefill` quietly fell back to the default.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from paths import resolve_runtime_state_file

import card
import batch
import dispatch_common
from dispatch_common import (
    APP_SCRIPT, MODEL_MAP, ROUTE_TABLE, SCENE_NUMBER_MAP,
)

def _append_user_args(cmd: list[str], args, *, skip_prompt: bool = False) -> None:
    """Turn every user argument (image/audio/video/aspect_ratio/...) into an ['--flag', value] fragment.
    Used by both the prefill and run shell paths so the mapping is written once.
    """
    if not skip_prompt and getattr(args, 'prompt', None):
        cmd += ['--prompt', args.prompt]
    for image in args.image or []:
        cmd += ['--image', image]
    for audio in args.audio or []:
        cmd += ['--audio', audio]
    for video in args.video or []:
        cmd += ['--video', video]
    if args.aspect_ratio:
        cmd += ['--aspect-ratio', args.aspect_ratio]
    if args.resolution:
        cmd += ['--resolution', args.resolution]
    if args.steps:
        cmd += ['--steps', str(args.steps)]
    if args.duration:
        cmd += ['--duration', str(args.duration)]
    if args.seed:
        cmd += ['--seed', str(args.seed)]
    if args.random_seed:
        cmd += ['--random-seed']
    if args.model_name:
        cmd += ['--model-name', args.model_name]
    if args.width:
        cmd += ['--width', str(args.width)]
    if args.height:
        cmd += ['--height', str(args.height)]
    for param in args.param or []:
        cmd += ['--param', param]
    if args.output:
        cmd += ['--output', args.output]
    if args.sync:
        cmd += ['--sync']

def _emit_param_error(exc, *, extra: dict | None = None) -> None:
    payload = {
        'error': 'INVALID_PARAM',
        'message': str(exc) or 'Invalid parameters',
        'issues': getattr(exc, 'issues', []),
    }
    available = getattr(exc, 'available_params', None)
    if available:
        payload['available_params'] = available
    if extra:
        payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    sys.exit(2)


def _collect_modelzoo_params(slug: str, args) -> dict[str, str]:
    """Collect explicit user parameters (standard flags + --param) without injecting defaults."""
    return card.collect_modelzoo_params(slug, args)


def _supplied_media_types(args) -> set[str]:
    supplied: set[str] = set()
    if getattr(args, 'image', None):
        supplied.add('images')
    if getattr(args, 'audio', None):
        supplied.add('audios')
    if getattr(args, 'video', None):
        supplied.add('videos')
    return supplied


def _batch_input_error(code: str, message: str) -> None:
    print(json.dumps({'error': code, 'message': message}, ensure_ascii=False, indent=2), file=sys.stderr)
    sys.exit(2)

def resolve_batch_json_input(batch_json: str | None, batch_json_file: str | None) -> str | None:
    """Resolve --batch-json-file or --batch-json @path into the inline JSON string.

    Long batch payloads are awkward and error-prone inline in a shell, so the file form is
    the recommended path for real batches. Exactly one of the two inputs may be supplied.
    """
    inline = batch_json if batch_json is not None else None
    if inline and batch_json_file:
        _batch_input_error('INVALID_BATCH_INPUT', 'Use either --batch-json or --batch-json-file, not both.')
    source_path = batch_json_file
    if not source_path and inline and str(inline).startswith('@') and len(str(inline)) > 1:
        source_path = str(inline)[1:]
    if not source_path:
        return inline
    path = Path(str(source_path)).expanduser()
    try:
        text = path.read_text(encoding='utf-8-sig')
    except OSError as exc:
        _batch_input_error('BATCH_JSON_FILE_UNREADABLE', f'Cannot read batch JSON file {path}: {exc}')
    if not text.strip():
        _batch_input_error('EMPTY_BATCH_JSON', f'Batch JSON file {path} is empty.')
    return text

def _modelzoo_execute(slug: str, args, *, endpoint: str | None=None, display_name: str | None=None) -> None:
    """Run a ModelZoo task (fixed route or arbitrary endpoint) through the shared pipeline."""
    import modelzoo
    import api as _api

    if endpoint is None:
        endpoint = ROUTE_TABLE[slug]['endpoint']
    display = display_name or dispatch_common.display_model_name(slug)
    manifest_slug = slug if slug in ROUTE_TABLE else endpoint.replace('/', '-')
    key = _api.require_api_key(args.api_key)

    # Fetch detail first: both validation and payload construction depend on the remote schema.
    detail_result = modelzoo.get_detail(key, endpoint)
    _api.require_ok(detail_result, 'modelzoo detail')
    detail_data = (detail_result.get('data') or {}).get('data') or detail_result.get('data') or {}
    if not (detail_data.get('input_params') or []):
        print(json.dumps({'error': 'MODELZOO_DETAIL_EMPTY', 'message': f'Could not load the parameter schema for {endpoint}; nothing was submitted', 'endpoint': endpoint}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    # Collect and validate parameters; unknown params, invalid combos and missing prompts are blocked before submission.
    try:
        user_params = _collect_modelzoo_params(slug, args)
    except modelzoo.ParamError as exc:
        _emit_param_error(exc, extra={'endpoint': endpoint})
    normalized_params, issues = modelzoo.validate_user_params(
        detail_data,
        user_params,
        supplied_media_types=_supplied_media_types(args),
    )
    if issues:
        _emit_param_error(modelzoo.ParamError(issues), extra={'endpoint': endpoint})

    # Upload media only after validation passes, to avoid wasted uploads.
    media_overrides: dict[str, list[str]] = {}
    if args.image:
        media_overrides['images'] = [_api.upload_input_file(key, p) for p in args.image]
    if args.audio:
        media_overrides['audios'] = [_api.upload_input_file(key, p) for p in args.audio]
    if args.video:
        media_overrides['videos'] = [_api.upload_input_file(key, p) for p in args.video]

    payload = modelzoo.build_task_payload(detail_data, normalized_params, media_overrides=media_overrides)
    if not payload:
        print(json.dumps({'error': 'MODELZOO_EMPTY_PAYLOAD', 'message': 'Parameters were empty; nothing was submitted', 'endpoint': endpoint}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    print(json.dumps({'action': 'modelzoo_submit', 'endpoint': endpoint, 'payload_preview': {k: (v if len(str(v)) < 100 else str(v)[:100] + '...') for k, v in payload.items()}}, ensure_ascii=False), file=sys.stderr)

    # Create the task
    create_result = _api.require_ok(modelzoo.create_task(key, endpoint, payload), 'modelzoo create')
    create_data = (create_result.get('data') or {}).get('data') or create_result.get('data') or {}
    request_id = create_data.get('request_id')
    if not request_id:
        print(json.dumps({'error': 'MODELZOO_CREATE_FAILED', 'message': f'Failed to create ModelZoo task for {endpoint}', 'detail': create_result}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    print(f'REQUEST_ID:{request_id}', file=sys.stderr)

    # Announce only after a successful submit; never say "submitted" when creation failed.
    print(dispatch_common.build_run_dispatch_message(slug))

    # Poll
    final = modelzoo.poll_until_done(key, request_id)
    outputs = final.get('outputs') or {}
    status = final.get('status')

    if status == 'Success':
        # Collect outputs and save them (batch children pass --output; single tasks use the default output root).
        result_items: list[dict[str, str]] = []
        for media_type in ('texts', 'images', 'videos', 'audios'):
            for url in (outputs.get(media_type) or []):
                result_items.append({'type': media_type, 'url': url})
        manifest = modelzoo.save_task_outputs(
            outputs,
            getattr(args, 'output', None),
            slug=manifest_slug,
            endpoint=endpoint,
            request_id=request_id,
            prompt=normalized_params.get('prompt'),
            params=payload,
        )
        summary = f'{display} finished!'
        if result_items:
            summary += f' {len(result_items)} output(s).'
        print(json.dumps({
            'status': 'ok',
            'summary': summary,
            'verdict': '\n'.join(f"[{item['type']}] {item['url']}" for item in result_items),
            'outputs': result_items,
            'saved_files': manifest.get('saved_files') or [],
            'run_manifest': manifest.get('manifest_path'),
            'download_errors': manifest.get('download_errors') or [],
            'request_id': request_id,
        }, ensure_ascii=False, indent=2))
    else:
        message = final.get('message') or 'Unknown error'
        print(json.dumps({
            'error': 'TASK_FAILED',
            'message': message,
            'status': status,
            'request_id': request_id,
        }, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)


def _modelzoo_prefill(slug: str, args, *, endpoint: str | None=None, display_name: str | None=None) -> None:
    """Render a confirmation card from ModelZoo detail input_params (fixed route or endpoint)."""
    import modelzoo
    import api as _api

    if endpoint is None:
        endpoint = ROUTE_TABLE[slug]['endpoint']
    display = display_name or dispatch_common.display_model_name(slug)
    key = _api.require_api_key(args.api_key)
    detail_result = modelzoo.get_detail(key, endpoint)
    _api.require_ok(detail_result, 'modelzoo prefill detail')
    detail_data = (detail_result.get('data') or {}).get('data') or detail_result.get('data') or {}
    input_params = detail_data.get('input_params') or []
    if not input_params:
        print(json.dumps({'error': 'MODELZOO_DETAIL_EMPTY', 'message': f'Could not load the parameter schema for {endpoint}; no card was generated', 'endpoint': endpoint}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    # The card may leave prompt empty (shown as "enter a value"), but malformed parameters must fail.
    try:
        user_params = _collect_modelzoo_params(slug, args)
    except modelzoo.ParamError as exc:
        _emit_param_error(exc, extra={'endpoint': endpoint})
    normalized_params, issues = modelzoo.validate_user_params(
        detail_data,
        user_params,
        require_all=False,
        supplied_media_types=_supplied_media_types(args),
    )
    if issues:
        _emit_param_error(modelzoo.ParamError(issues), extra={'endpoint': endpoint})

    print(card.build_modelzoo_prefill_card(
        display,
        endpoint,
        input_params,
        normalized_params,
    ))


def main():
    p = argparse.ArgumentParser(description='BizyAir task wrapper for common entries and remote discovery')
    p.add_argument('--api-key')
    p.add_argument('--check', action='store_true')
    p.add_argument('--wallet', action='store_true')
    p.add_argument('--browse', action='store_true')
    p.add_argument('--search')
    p.add_argument('--modality')
    p.add_argument('--stability')
    p.add_argument('--info')
    p.add_argument('--task')
    p.add_argument('--model')
    p.add_argument('--scene')
    p.add_argument('--image-menu', action='store_true')
    p.add_argument('--json', action='store_true', help='Print the menu as structured JSON instead of conversational text')
    p.add_argument('--video-menu', action='store_true')
    p.add_argument('--clear-runtime-state', action='store_true', help='Clear the local follow-up runtime_state cache')
    p.add_argument('--prompt')
    p.add_argument('--param-help', action='store_true')
    p.add_argument('--prefill-card', action='store_true')
    p.add_argument('--batch-json')
    p.add_argument('--batch-json-file', help='Read the batch JSON payload from a UTF-8 file instead of passing it inline')
    p.add_argument('--batch-prefill', action='store_true')
    p.add_argument('--batch-run', action='store_true')
    p.add_argument('--batch-concurrency', type=int)
    p.add_argument('--batch-concurrency-approved', action='store_true')
    p.add_argument('--batch-task-count-approved', action='store_true')
    p.add_argument('--batch-worker-file', help=argparse.SUPPRESS)
    p.add_argument('--batch-watchdog-file', help=argparse.SUPPRESS)
    p.add_argument('--batch-worker-pid', type=int, help=argparse.SUPPRESS)
    p.add_argument('--batch-status', nargs='?', const='', default=None, help='Read a batch status file or batch directory; omit the value to use the latest batch')
    p.add_argument('--card-input')
    p.add_argument('--run-with-defaults', action='store_true')
    p.add_argument('--confirm-run', action='store_true')
    p.add_argument('--image', action='append')
    p.add_argument('--audio', action='append')
    p.add_argument('--video', action='append')
    p.add_argument('--aspect-ratio')
    p.add_argument('--resolution')
    p.add_argument('--steps')
    p.add_argument('--duration')
    p.add_argument('--seed')
    p.add_argument('--random-seed', action='store_true')
    p.add_argument('--model-name')
    p.add_argument('--width')
    p.add_argument('--height')
    p.add_argument('--param', action='append')
    p.add_argument('-o', '--output')
    p.add_argument('--sync', action='store_true')
    args = p.parse_args()
    args.batch_json = resolve_batch_json_input(args.batch_json, args.batch_json_file)
    if args.image_menu:
        if args.json:
            print(json.dumps(dispatch_common.build_image_menu_payload(args.prompt, args.image or []), ensure_ascii=False, indent=2))
        else:
            print(dispatch_common.build_image_menu_message(args.prompt, args.image or []), end='')
        return
    if args.video_menu:
        if args.json:
            print(json.dumps(dispatch_common.build_video_menu_payload(args.prompt, args.image or [], args.audio or []), ensure_ascii=False, indent=2))
        else:
            print(dispatch_common.build_video_menu_message(args.prompt, args.image or [], args.audio or []), end='')
        return
    if args.clear_runtime_state:
        cleared = dispatch_common.clear_runtime_state()
        print(json.dumps({'status': 'cleared' if cleared else 'already_empty', 'path': str(resolve_runtime_state_file().resolve())}, ensure_ascii=False, indent=2))
        return
    if args.batch_watchdog_file:
        batch.handle_batch_watchdog(args.batch_watchdog_file, args.batch_worker_pid)
        return
    if args.batch_worker_file:
        batch.handle_batch_worker(args.batch_worker_file)
        return
    if args.batch_status is not None:
        batch.handle_batch_status(args.batch_status)
        return
    if args.batch_json:
        if args.batch_prefill == args.batch_run:
            print(json.dumps({'error': 'INVALID_BATCH_MODE', 'message': 'Use exactly one of --batch-prefill or --batch-run with --batch-json'}, ensure_ascii=False, indent=2), file=sys.stderr)
            sys.exit(2)
        if args.prefill_card or args.param_help or args.card_input or (args.confirm_run and args.batch_prefill):
            print(json.dumps({'error': 'INVALID_BATCH_FLAG_COMBINATION', 'message': 'Batch prefill cannot be mixed with single-task flags like --prefill-card / --param-help / --card-input. --confirm-run is only valid with --batch-run.'}, ensure_ascii=False, indent=2), file=sys.stderr)
            sys.exit(2)
        if args.batch_prefill:
            batch.handle_batch_prefill(args)
            return
        if not args.confirm_run:
            # Match the modelzoo-run confirmation gate: without --confirm-run, print the card and do not submit.
            batch.handle_batch_prefill(args)
            sys.stdout.flush()
            print(json.dumps({'error': 'CONFIRM_REQUIRED', 'message': 'Nothing was submitted. After reviewing the parameter card above, rerun the same command with --confirm-run at the end.'}, ensure_ascii=False), file=sys.stderr)
            sys.exit(3)
        batch.handle_batch_run(args)
        return
    if args.batch_prefill or args.batch_run:
        print(json.dumps({'error': 'MISSING_BATCH_JSON', 'message': 'Use --batch-json together with --batch-prefill or --batch-run'}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(2)
    if args.prefill_card and args.confirm_run:
        print(json.dumps({'error': 'INVALID_FLAG_COMBINATION', 'message': 'Single-task mode cannot combine --prefill-card and --confirm-run. Show the card first, then run in a separate command after confirmation.'}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(2)
    base_cmd = [sys.executable, str(APP_SCRIPT)]
    if args.api_key:
        base_cmd += ['--api-key', args.api_key]
    if args.check:
        dispatch_common.run_subprocess(base_cmd + ['--check'])
        return
    if args.wallet:
        dispatch_common.run_subprocess(base_cmd + ['--wallet'])
        return
    if args.browse:
        cmd = list(base_cmd) + ['--browse', '--remote']
        if args.modality:
            cmd += ['--modality', args.modality]
        if args.stability:
            cmd += ['--stability', args.stability]
        dispatch_common.run_subprocess(cmd)
        return
    if args.search:
        dispatch_common.run_subprocess(base_cmd + ['--search', args.search, '--remote'])
        return
    if args.info is not None:
        info_target = str(args.info).strip()
        if not info_target:
            print(json.dumps({'error': 'EMPTY_INFO_TARGET', 'message': '--info needs a model name, ID or link; received an empty value'}, ensure_ascii=False), file=sys.stderr)
            sys.exit(2)
        target = SCENE_NUMBER_MAP.get(info_target, info_target)
        target = MODEL_MAP.get(target, MODEL_MAP.get(str(target).lower(), target))
        if target in ROUTE_TABLE:
            # Fixed model: print ModelZoo detail directly instead of shelling out to app.py --info
            import modelzoo
            import api as _api
            endpoint = ROUTE_TABLE[target]['endpoint']
            detail = modelzoo.get_detail(_api.require_api_key(args.api_key), endpoint)
            _api.require_ok(detail, 'modelzoo info')
            print(json.dumps(detail, ensure_ascii=False, indent=2))
            return
        # Non-fixed model: shell out to app.py --info
        dispatch_common.run_subprocess(base_cmd + ['--info', target, '--remote'])
        return
    model = args.model
    if args.scene:
        model = SCENE_NUMBER_MAP.get(args.scene, args.scene)
    if args.param_help or args.prefill_card:
        target = dispatch_common.resolve_model(args.task, model)
        if target in ROUTE_TABLE:
            # Fixed model: render the confirmation card from ModelZoo
            _modelzoo_prefill(target, args)
        elif dispatch_common.is_modelzoo_endpoint(target):
            # Arbitrary ModelZoo endpoint
            _modelzoo_prefill(target, args, endpoint=target, display_name=target)
        else:
            prefill_cmd = list(base_cmd) + ['--prefill-card', target]
            _append_user_args(prefill_cmd, args)
            dispatch_common.run_subprocess(prefill_cmd)
        return
    target_for_card = dispatch_common.resolve_model(args.task, model) if args.card_input or model else None
    if args.card_input and target_for_card:
        card.apply_compiled_selection(args, target_for_card)
    if model == 'remote-search-flow':
        query = args.prompt or args.search or args.task or 'general image generation'
        dispatch_common.run_subprocess(base_cmd + ['--pick-image-candidates', query, '--remote', '--remote-source', 'community', '--reply-format', 'json'])
        return
    if model == 'remote-video-search-flow':
        query = args.prompt or args.search or args.task or 'text-to-video'
        dispatch_common.run_subprocess(base_cmd + ['--pick-video-candidates', query, '--remote', '--remote-source', 'community', '--reply-format', 'json'])
        return
    if model == 'modelzoo-search-flow':
        dispatch_common.ensure_api_key_ready(args.api_key)
        query = args.prompt or args.search or args.task or 'text-to-image'
        cli_script = dispatch_common.SCRIPT_DIR / 'cli.py'
        cmd = [sys.executable, str(cli_script), 'pick-modelzoo-image', query, '--reply-format', 'json']
        if args.api_key:
            cmd += ['--api-key', args.api_key]
        dispatch_common.run_subprocess(cmd)
        return
    if model == 'modelzoo-video-search-flow':
        dispatch_common.ensure_api_key_ready(args.api_key)
        query = args.prompt or args.search or args.task or 'text-to-video'
        cli_script = dispatch_common.SCRIPT_DIR / 'cli.py'
        cmd = [sys.executable, str(cli_script), 'pick-modelzoo-video', query, '--reply-format', 'json']
        if args.api_key:
            cmd += ['--api-key', args.api_key]
        dispatch_common.run_subprocess(cmd)
        return
    app_id = dispatch_common.resolve_model(args.task, model)
    is_endpoint = dispatch_common.is_modelzoo_endpoint(app_id)
    parameter_state = card.determine_parameter_state(args, app_id)
    if parameter_state in ('missing_required', 'awaiting_confirmation'):
        if app_id in ROUTE_TABLE:
            _modelzoo_prefill(app_id, args)
        elif is_endpoint:
            _modelzoo_prefill(app_id, args, endpoint=app_id, display_name=app_id)
        else:
            prefill_cmd = list(base_cmd) + ['--prefill-card', app_id]
            _append_user_args(prefill_cmd, args)
            dispatch_common.run_subprocess(prefill_cmd)
        return
    dispatch_common.ensure_api_key_ready(args.api_key)

    if app_id in ROUTE_TABLE or is_endpoint:
        # Fixed model or arbitrary ModelZoo endpoint: execute directly through ModelZoo.
        prompt_bundle = card.build_prompt_bundle_for_model(app_id, args)
        execution_prompt = prompt_bundle.get('execution_prompt', '') or (args.prompt or '')
        state_entry = dispatch_common.build_runtime_state_entry(str(app_id), args, execution_prompt)
        if is_endpoint:
            _modelzoo_execute(app_id, args, endpoint=app_id, display_name=app_id)
        else:
            _modelzoo_execute(app_id, args)
        # Persist runtime state after success
        state = dispatch_common.load_runtime_state()
        state.setdefault('last_run', {})[str(app_id)] = state_entry
        dispatch_common.save_runtime_state(state)
        return

    # Non-fixed model: legacy webapp path (user-provided AI app link / ID)
    cmd = list(base_cmd) + ['--run', app_id]
    prompt_bundle = card.build_prompt_bundle_for_model(app_id, args)
    execution_prompt = prompt_bundle.get('execution_prompt', '') or (args.prompt or '')
    if execution_prompt:
        cmd += ['--prompt', execution_prompt]
    _append_user_args(cmd, args, skip_prompt=True)
    state_entry = dispatch_common.build_runtime_state_entry(str(app_id), args, execution_prompt)

    def on_success() -> None:
        state = dispatch_common.load_runtime_state()
        state.setdefault('last_run', {})[str(app_id)] = state_entry
        dispatch_common.save_runtime_state(state)
        # Announce "submitted" only when the child process reports a successful webapp submit.
        print(dispatch_common.build_run_dispatch_message(app_id))

    dispatch_common.run_subprocess(cmd, success_callback=on_success)


if __name__ == '__main__':
    main()
