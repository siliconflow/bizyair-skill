"""Batch parameter cards / batch execution handling.

Two main entry points:
  - handle_batch_prefill: --batch-prefill. Chains the prefill cards of several tasks, **rendered via route B**
                          (remote.build_remote_info_output -> build_remote_prefilled_card).
                          per-batch cache: the same model id fetches its contract only once per batch.
  - handle_batch_run:     --batch-run. Writes each task as its own subprocess
                          (`python3 dispatch.py --model X --confirm-run ...`),
                          starts a worker asynchronously to schedule them, and the main process
                          returns status JSON immediately.
                          The worker is itself a dispatch.py subprocess
                          (`python3 dispatch.py --batch-worker-file <file>`).

[WARNING: every subprocess entry point is dispatch.py, not batch.py itself]
Where build_batch_child_command and handle_batch_run launch the worker, both use
dispatch_common.DISPATCH_SCRIPT rather than Path(__file__). The reason: batch.py has no main(),
and a previous misuse of __file__ made the whole --batch-run exit silently (the process died 0 seconds after launch).
The watchdog is the only exception: it must bypass test/scheduler replacements of DISPATCH_SCRIPT,
so launch_batch_watchdog explicitly uses the real dispatch.py in the same directory.

Concurrency / task count are both limited by config.json's batch.max_concurrency / max_tasks
plus a hard system cap (see resolve_batch_policy and enforce_batch_task_limits).
"""
from __future__ import annotations
import argparse, json, os, secrets, subprocess, sys, time, traceback
from contextlib import contextmanager
from pathlib import Path
from paths import (
    is_protected_output_path,
    resolve_batch_outputs_dir,
    resolve_batch_runs_dir,
)
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # Windows fallback: atomic replace still prevents torn reads.
    fcntl = None

import card
import dispatch_common
import remote
from dispatch_common import (
    COMPILER_RULES, CONFIGURED_MAX_BATCH_CONCURRENCY_FALLBACK,
    CONFIGURED_MAX_BATCH_TASKS_FALLBACK, ROUTE_TABLE,
    RUNTIME_STATE_DISABLED_ENV, SCENE_NUMBER_MAP,
    SYSTEM_MAX_BATCH_CONCURRENCY, SYSTEM_MAX_BATCH_TASKS,
)

# Search routes (menu 6 / 7 / v6 / v7) are interactive pickers, not executable models. batch must not run such slugs.
SEARCH_FLOW_SLUGS = {
    'remote-search-flow', 'remote-video-search-flow',
    'modelzoo-search-flow', 'modelzoo-video-search-flow',
}

# Runtime-only batch files. The heartbeat lets an independent watchdog detect a
# SIGKILLed worker even when the parent batch-run process has already exited.
BATCH_HEARTBEAT_FILENAME = 'batch-worker-heartbeat.json'
BATCH_STATUS_FILENAME = 'batch-status.json'
BATCH_WATCHDOG_LOG_FILENAME = 'batch-watchdog.log'
BATCH_WATCHDOG_DEFAULT_INTERVAL = 0.5
BATCH_WATCHDOG_DEFAULT_GRACE = 1.0
BATCH_WATCHDOG_DEFAULT_HEARTBEAT_TIMEOUT = 15.0
BATCH_WATCHDOG_DEFAULT_MAX_SECONDS = 86400.0

# Worker status writes must not erase fields owned by the parent/watchdog.
BATCH_STATUS_PRESERVED_KEYS = (
    'watchdog_pid',
    'watchdog_status',
    'watchdog_log',
    'watchdog_finished_at',
)


def reject_search_flow_target(target: str, *, task_index: int, phase: str) -> None:
    """If a search route slug shows up in a batch task (writing scene: 6 / 7 triggers it), return a friendly error."""
    if target not in SEARCH_FLOW_SLUGS:
        return
    print(json.dumps({
        'error': 'BATCH_SEARCH_FLOW_NOT_SUPPORTED',
        'message': (
            f'batch task #{task_index} resolved to a search entry (menu 6 / 7), which is not an executable model. '
            'Search entries are interactive lookups and cannot be batched directly. '
            'Run the search on its own first to get a concrete model slug or ModelZoo endpoint, then put that in the batch.'
        ),
        'slug': target,
        'task_index': task_index,
        'phase': phase,
    }, ensure_ascii=False, indent=2), file=sys.stderr)
    sys.exit(2)

def resolve_batch_policy() -> dict:
    config = dispatch_common.load_skill_config()
    batch_cfg = config.get('batch') if isinstance(config.get('batch'), dict) else {}
    configured_max_concurrency = batch_cfg.get('max_concurrency', CONFIGURED_MAX_BATCH_CONCURRENCY_FALLBACK)
    configured_max_tasks = batch_cfg.get('max_tasks', CONFIGURED_MAX_BATCH_TASKS_FALLBACK)
    try:
        configured_max_concurrency = int(configured_max_concurrency)
    except Exception:
        configured_max_concurrency = CONFIGURED_MAX_BATCH_CONCURRENCY_FALLBACK
    try:
        configured_max_tasks = int(configured_max_tasks)
    except Exception:
        configured_max_tasks = CONFIGURED_MAX_BATCH_TASKS_FALLBACK
    if configured_max_concurrency < 1:
        configured_max_concurrency = CONFIGURED_MAX_BATCH_CONCURRENCY_FALLBACK
    if configured_max_tasks < 1:
        configured_max_tasks = CONFIGURED_MAX_BATCH_TASKS_FALLBACK
    configured_max_concurrency = min(configured_max_concurrency, SYSTEM_MAX_BATCH_CONCURRENCY)
    configured_max_tasks = min(configured_max_tasks, SYSTEM_MAX_BATCH_TASKS)
    return {'max_concurrency': configured_max_concurrency, 'system_max_concurrency': SYSTEM_MAX_BATCH_CONCURRENCY, 'max_tasks': configured_max_tasks, 'system_max_tasks': SYSTEM_MAX_BATCH_TASKS}

def build_cli_arg_list(flag: str, values: list[str] | None) -> list[str]:
    output: list[str] = []
    for value in values or []:
        output += [flag, str(value)]
    return output

def listify_task_value(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item or '').strip()]
    text = str(value).strip()
    return [text] if text else []

def task_label(task: dict, index: int) -> str:
    label = str(task.get('label') or task.get('title') or '').strip()
    return label if label else f'Task {index}'

def parse_batch_payload(raw: str) -> tuple[list[dict], int | None]:
    parsed = json.loads(raw)
    concurrency = None
    tasks = parsed
    if isinstance(parsed, dict):
        tasks = parsed.get('tasks')
        concurrency = parsed.get('concurrency')
    if not isinstance(tasks, list) or not tasks:
        raise ValueError('batch_json must be a non-empty JSON array or an object with a tasks array')
    normalized: list[dict] = []
    for (index, item) in enumerate(tasks, start=1):
        if isinstance(item, str):
            normalized.append({'prompt': item})
            continue
        if isinstance(item, dict):
            normalized.append(dict(item))
            continue
        raise ValueError(f'batch task #{index} must be an object or string')
    return (normalized, concurrency)

def abort_invalid_batch_payload(error: str, message: str) -> None:
    print(json.dumps({'error': error, 'message': message}, ensure_ascii=False, indent=2), file=sys.stderr)
    sys.exit(2)

def parse_batch_payload_or_exit(raw: str) -> tuple[list[dict], int | None]:
    """Parse batch JSON; input errors become structured errors with exit 2, without exposing a traceback."""
    try:
        return parse_batch_payload(raw)
    except json.JSONDecodeError as exc:
        abort_invalid_batch_payload('INVALID_BATCH_JSON', f'batch_json must be valid JSON (line {exc.lineno}, column {exc.colno}): {exc.msg}')
    except TypeError as exc:
        abort_invalid_batch_payload('INVALID_BATCH_PAYLOAD', f'batch_json must be a JSON string: {exc}')
    except ValueError as exc:
        abort_invalid_batch_payload('INVALID_BATCH_PAYLOAD', str(exc) or 'batch_json is invalid')
    raise AssertionError('unreachable')

def build_task_args(base_args: argparse.Namespace, task: dict) -> argparse.Namespace:
    prompt = task.get('prompt')
    card_input = task.get('card_input')
    task_args = argparse.Namespace(api_key=base_args.api_key, check=False, wallet=False, browse=False, search=None, modality=None, stability=None, info=None, task=task.get('task') or getattr(base_args, 'task', None), model=task.get('model') or base_args.model, scene=task.get('scene') or getattr(base_args, 'scene', None), image_menu=False, video_menu=False, prompt='' if prompt is None else str(prompt), param_help=bool(task.get('param_help', False)), prefill_card=False, card_input='' if card_input is None else str(card_input), run_with_defaults=bool(task.get('run_with_defaults', getattr(base_args, 'run_with_defaults', False))), confirm_run=bool(task.get('confirm_run', False)), image=listify_task_value(task.get('image')), audio=listify_task_value(task.get('audio')), video=listify_task_value(task.get('video')), aspect_ratio=task.get('aspect_ratio'), resolution=task.get('resolution'), steps=task.get('steps'), duration=task.get('duration'), seed=task.get('seed'), random_seed=bool(task.get('random_seed', False)), model_name=task.get('model_name'), width=task.get('width'), height=task.get('height'), param=listify_task_value(task.get('param')), output=task.get('output'), sync=bool(task.get('sync', getattr(base_args, 'sync', False))), batch_json=None, batch_prefill=False, batch_run=False, batch_concurrency=None, batch_concurrency_approved=False, batch_task_count_approved=False, batch_worker_file=None)
    return task_args

def should_batch_use_defaults(task_args: argparse.Namespace, model: str | None) -> bool:
    if task_args.run_with_defaults or card.allows_explicit_defaults(task_args):
        return True
    if model in COMPILER_RULES and (not card.has_explicit_tuning(task_args)):
        return True
    return False

def _supplied_media_types(task_args: argparse.Namespace) -> set[str]:
    supplied: set[str] = set()
    if getattr(task_args, 'image', None):
        supplied.add('images')
    if getattr(task_args, 'audio', None):
        supplied.add('audios')
    if getattr(task_args, 'video', None):
        supplied.add('videos')
    return supplied

def fail_invalid_params(exc, *, task_index: int | None = None, endpoint: str | None = None) -> None:
    payload = {
        'error': 'INVALID_PARAM',
        'message': str(exc) or 'invalid parameter',
        'issues': getattr(exc, 'issues', []),
    }
    available = getattr(exc, 'available_params', None)
    if available:
        payload['available_params'] = available
    if task_index is not None:
        payload['task_index'] = task_index
    if endpoint:
        payload['endpoint'] = endpoint
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
    sys.exit(2)

def resolve_task_run_manifest_path(output_path: str | None) -> Path | None:
    """Locate the run.json the subprocess should write; --output may be a directory or a file basename."""
    if not output_path:
        return None
    candidate = Path(output_path)
    # A single task's --output may be a directory or a file basename; try both, so a
    # directory name containing a dot does not make the run.json lookup miss into the parent directory.
    candidates = [candidate / 'run.json', candidate.parent / 'run.json']
    for manifest_path in candidates:
        if manifest_path.exists():
            return manifest_path
    return None


def inspect_task_run_manifest(output_path: str | None) -> tuple[dict | None, str | None, str | None]:
    """Strictly check run.json and return (manifest, error_code, detail).

    A worker exit code of 0 does not mean the task succeeded: run.json must exist and parse.
    Only an empty error_code means the manifest is usable; detail is written to the status file
    and logs and contains no secrets.
    """
    manifest_path = resolve_task_run_manifest_path(output_path)
    if manifest_path is None:
        return None, 'run_manifest_missing', 'run.json was not found under the task output path'
    try:
        raw = manifest_path.read_text(encoding='utf-8')
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, 'run_manifest_invalid', f'run.json is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})'
    except Exception as exc:
        return None, 'run_manifest_invalid', f'run.json could not be read: {type(exc).__name__}: {exc}'
    if not isinstance(data, dict):
        return None, 'run_manifest_invalid', 'run.json root must be a JSON object'
    if not data:
        return None, 'run_manifest_invalid', 'run.json is empty'
    return data, None, str(manifest_path)

def build_batch_prefill_reply(tasks: list[dict]) -> str:
    lines = ['I have organized the parameter confirmation cards for this batch, one per task:', '']
    for item in tasks:
        lines.append(f"### {item['label']}")
        lines.append(item['card_markdown'].strip())
        lines.append('')
    lines.append('You can adjust them one by one, or just say "run them all" and I will submit them together.')
    return '\n'.join(lines).strip()

def build_batch_run_reply(tasks: list[dict]) -> str:
    lines = ['I have submitted these tasks as separate tasks:', '']
    for item in tasks:
        lines.append(f"- **{item['label']}**: queued in the batch, log at `{item['log_path']}`")
    lines.append('')
    lines.append('I will follow up on each task result separately rather than merging them into one.')
    return '\n'.join(lines).strip()

def redact_command_args(args: list[str]) -> list[str]:
    redacted = list(args)
    sensitive_flags = {'--api-key'}
    for (idx, value) in enumerate(redacted[:-1]):
        if value in sensitive_flags:
            redacted[idx + 1] = '***'
    return redacted

def resolve_batch_concurrency(base_args: argparse.Namespace, requested: int | None, task_count: int) -> tuple[int, int, dict]:
    policy = resolve_batch_policy()
    cli_value = getattr(base_args, 'batch_concurrency', None)
    raw_value = cli_value if cli_value is not None else requested
    if raw_value is None:
        raw_value = policy['max_concurrency']
    try:
        normalized = int(raw_value)
    except Exception as exc:
        raise ValueError('batch concurrency must be an integer') from exc
    if normalized < 1:
        raise ValueError('batch concurrency must be at least 1')
    effective = min(normalized, policy['system_max_concurrency'], max(task_count, 1))
    return (normalized, effective, policy)

def write_json_file_atomic(path: Path, payload: dict) -> None:
    """Atomically write JSON: same-directory temp file + fsync + os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f'.{path.name}.{os.getpid()}.{time.time_ns()}.tmp')
    try:
        with open(tmp_path, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


@contextmanager
def batch_status_lock(status_path: Path) -> Iterator[None]:
    """Serialize read-modify-write of batch-status so the watchdog cannot overwrite the worker's final state."""
    status_path = Path(status_path)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = status_path.with_name(status_path.name + '.lock')
    lock_handle = open(lock_path, 'a+', encoding='utf-8')
    try:
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        lock_handle.close()


def read_batch_status_file(status_path: Path) -> dict | None:
    try:
        data = json.loads(Path(status_path).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def write_batch_status_file_unlocked(status_path: Path, payload: dict) -> None:
    write_json_file_atomic(status_path, payload)


def write_batch_status_file(status_path: Path, payload: dict) -> None:
    with batch_status_lock(status_path):
        current = read_batch_status_file(status_path)
        if current:
            for key in BATCH_STATUS_PRESERVED_KEYS:
                if key not in payload and key in current:
                    payload[key] = current[key]
        write_batch_status_file_unlocked(status_path, payload)


def update_batch_status_if_not_final(status_path: Path, updates: dict) -> dict:
    """Merge updates only while the scheduler is not final, so the parent cannot overwrite the worker's completed state."""
    with batch_status_lock(status_path):
        payload = read_batch_status_file(status_path) or {}
        if payload.get('scheduler_status') in {'completed', 'failed', 'cancelled'}:
            return payload
        payload.update(updates)
        write_batch_status_file_unlocked(status_path, payload)
        return payload


def write_worker_heartbeat(heartbeat_path: Path, *, status: str = 'running') -> None:
    try:
        write_json_file_atomic(heartbeat_path, {
            'pid': os.getpid(),
            'at': time.time(),
            'scheduler_status': status,
        })
    except Exception:
        # A failed heartbeat must not abort the task; the watchdog covers it with a timeout.
        pass


def _watchdog_env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except Exception:
        return default
    return value if value >= 0 else default


def pid_is_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def create_unique_batch_dirs() -> tuple[str, Path, Path]:
    """Atomically create a unique batch directory; retry with a random suffix on same-second collisions."""
    base_name = time.strftime('%Y%m%d-%H%M%S')
    runs_root = resolve_batch_runs_dir()
    outputs_root = resolve_batch_outputs_dir()
    for attempt in range(100):
        suffix = '' if attempt == 0 else f'-{secrets.token_hex(3)}'
        candidate_name = f'{base_name}{suffix}'
        batch_dir = runs_root / candidate_name
        batch_output_dir = outputs_root / candidate_name
        try:
            batch_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        if batch_output_dir == batch_dir:
            return candidate_name, batch_dir, batch_output_dir
        try:
            batch_output_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            try:
                batch_dir.rmdir()
            except OSError:
                pass
            continue
        except Exception:
            try:
                batch_dir.rmdir()
            except OSError:
                pass
            raise
        return candidate_name, batch_dir, batch_output_dir
    raise RuntimeError('unable to allocate a unique batch directory after 100 attempts')


def launch_batch_watchdog(status_path: Path, worker_pid: int, watchdog_log: Path) -> subprocess.Popen:
    """Start a watchdog in its own session; it settles the final state even if the batch-run parent exits."""
    # Must use the real dispatch.py, not DISPATCH_SCRIPT which tests/schedulers may replace.
    dispatch_script = Path(__file__).resolve().with_name('dispatch.py')
    watchdog_env = dict(os.environ)
    watchdog_env.pop('BIZYAIR_API_KEY', None)
    watchdog_env[RUNTIME_STATE_DISABLED_ENV] = '1'
    watchdog_env['PYTHONDONTWRITEBYTECODE'] = '1'
    log_handle = open(watchdog_log, 'a', encoding='utf-8')
    try:
        return subprocess.Popen(
            [
                sys.executable,
                str(dispatch_script),
                '--batch-watchdog-file', str(status_path.resolve()),
                '--batch-worker-pid', str(worker_pid),
            ],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding='utf-8',
            errors='replace',
            env=watchdog_env,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_handle.close()


def handle_batch_watchdog(status_file: str, worker_pid: int | None = None) -> None:
    """Reconcile the worker heartbeat / PID; when the worker disappears unexpectedly, settle queued and running tasks as failed."""
    status_path = Path(status_file).resolve()
    supervisor_log = status_path.with_name('batch-supervisor.log')
    interval = _watchdog_env_float('BIZYAIR_BATCH_WATCHDOG_INTERVAL', BATCH_WATCHDOG_DEFAULT_INTERVAL)
    grace = _watchdog_env_float('BIZYAIR_BATCH_WATCHDOG_GRACE', BATCH_WATCHDOG_DEFAULT_GRACE)
    heartbeat_timeout = _watchdog_env_float('BIZYAIR_BATCH_WATCHDOG_HEARTBEAT_TIMEOUT', BATCH_WATCHDOG_DEFAULT_HEARTBEAT_TIMEOUT)
    max_seconds = _watchdog_env_float('BIZYAIR_BATCH_WATCHDOG_MAX_SECONDS', BATCH_WATCHDOG_DEFAULT_MAX_SECONDS)
    started_at = time.time()
    last_heartbeat_at = started_at
    while time.time() - started_at < max_seconds:
        with batch_status_lock(status_path):
            payload = read_batch_status_file(status_path)
            if payload:
                if payload.get('scheduler_status') in {'completed', 'failed', 'cancelled'}:
                    if payload.get('watchdog_status') in {None, 'starting', 'running'}:
                        payload['watchdog_status'] = 'completed'
                        payload['watchdog_finished_at'] = int(time.time())
                        write_batch_status_file_unlocked(status_path, payload)
                    return
                heartbeat_raw = payload.get('worker_heartbeat_path')
                heartbeat_path = Path(heartbeat_raw) if heartbeat_raw else status_path.with_name(BATCH_HEARTBEAT_FILENAME)
                try:
                    last_heartbeat_at = max(last_heartbeat_at, heartbeat_path.stat().st_mtime)
                except OSError:
                    pass
                pid = worker_pid if worker_pid is not None else payload.get('scheduler_pid')
                pid_alive = pid_is_alive(pid)
                heartbeat_age = time.time() - last_heartbeat_at
                pid_gone = (not pid_alive) and (time.time() - started_at >= grace)
                heartbeat_lost = heartbeat_age > heartbeat_timeout
                if pid_gone or heartbeat_lost:
                    reason = 'worker_process_lost'
                    detail = (
                        'batch worker process exited unexpectedly'
                        if pid_gone
                        else f'batch worker heartbeat stopped for {heartbeat_age:.1f}s'
                    )
                    mark_batch_tasks_failed(payload, reason=reason)
                    payload['scheduler_status'] = 'failed'
                    payload['worker_error'] = detail
                    payload['watchdog_status'] = 'failed'
                    payload['watchdog_finished_at'] = int(time.time())
                    write_batch_status_file_unlocked(status_path, payload)
                    append_supervisor_log(supervisor_log, f'watchdog marked batch failed: {detail}')
                    return
        time.sleep(interval)
    append_supervisor_log(supervisor_log, 'watchdog timed out without a final scheduler status')

def build_public_batch_payload(payload: dict[str, Any]) -> dict[str, Any]:
    public_payload = dict(payload)
    public_payload.pop('system_max_concurrency', None)
    public_payload.pop('system_max_tasks', None)
    return public_payload

def resolve_batch_status_path(target: str | None) -> Path | None:
    """Resolve a user-supplied batch directory / status file / empty value into a concrete batch-status.json path.

    - Empty: take the newest batch under the runtime batch_runs directory.
    - Directory: append batch-status.json.
    - Anything else: treat it as a file path.
    Returns None when no batch can be found.
    """
    if target is None or str(target).strip() == '':
        runs_root = resolve_batch_runs_dir()
        if not runs_root.exists():
            return None
        candidates = sorted(path for path in runs_root.iterdir() if path.is_dir())
        if not candidates:
            return None
        return candidates[-1] / BATCH_STATUS_FILENAME
    candidate = Path(str(target)).expanduser()
    if candidate.is_dir():
        return candidate / BATCH_STATUS_FILENAME
    return candidate

def build_batch_status_summary(status_path: Path, payload: dict) -> dict:
    """Reduce the raw batch-status.json into a compact query-facing summary (no internal caps / secrets)."""
    tasks = [task for task in (payload.get('tasks') or []) if isinstance(task, dict)]
    task_status_counts: dict[str, int] = {}
    for task in tasks:
        status = str(task.get('status') or 'unknown')
        task_status_counts[status] = task_status_counts.get(status, 0) + 1
    return {
        'mode': 'batch_status',
        'status_path': str(Path(status_path).resolve()),
        'batch_dir': payload.get('batch_dir'),
        'batch_output_dir': payload.get('batch_output_dir'),
        'scheduler_status': payload.get('scheduler_status'),
        'watchdog_status': payload.get('watchdog_status'),
        'worker_error': payload.get('worker_error'),
        'task_count': payload.get('task_count') if payload.get('task_count') is not None else len(tasks),
        'task_status_counts': task_status_counts,
        'reply_markdown': payload.get('reply_markdown'),
        'tasks': [
            {
                'index': task.get('index'),
                'label': task.get('label'),
                'model': task.get('model'),
                'status': task.get('status'),
                'exit_code': task.get('exit_code'),
                'request_id': task.get('request_id'),
                'failure_reason': task.get('failure_reason'),
                'output_path': task.get('output_path'),
                'saved_files': task.get('saved_files') or [],
            }
            for task in tasks
        ],
    }

def handle_batch_status(target: str | None) -> None:
    """`batch-status`: read and summarize one batch's batch-status.json."""
    status_path = resolve_batch_status_path(target)
    if status_path is None:
        print(json.dumps({
            'error': 'BATCH_STATUS_NOT_FOUND',
            'message': 'No batch run directory was found on this machine; pass a specific batch directory or batch-status.json path.',
            'target': target,
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(2)
    payload = read_batch_status_file(status_path)
    if payload is None:
        print(json.dumps({
            'error': 'BATCH_STATUS_NOT_FOUND',
            'message': 'The specified batch-status.json does not exist, is unreadable, or is not a JSON object.',
            'status_path': str(status_path),
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(2)
    print(json.dumps(build_batch_status_summary(status_path, payload), ensure_ascii=False, indent=2))

def enforce_batch_task_limits(base_args: argparse.Namespace, *, task_count: int, policy: dict, phase: str) -> None:
    if task_count > policy['system_max_tasks']:
        print(json.dumps({'error': 'BATCH_TASK_LIMIT_EXCEEDED', 'message': f"Batch task count {task_count} exceeds hard system max {policy['system_max_tasks']} during {phase}. Split into smaller batches.", 'phase': phase, 'task_count': task_count, 'configured_max_tasks': policy['max_tasks'], 'system_max_tasks': policy['system_max_tasks']}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(2)
    if task_count > policy['max_tasks'] and (not getattr(base_args, 'batch_task_count_approved', False)):
        print(json.dumps({'error': 'BATCH_TASK_COUNT_REQUIRES_APPROVAL', 'message': f"Batch task count {task_count} exceeds configured max_tasks {policy['max_tasks']} during {phase}. Ask for explicit user approval, then rerun with --batch-task-count-approved.", 'phase': phase, 'task_count': task_count, 'configured_max_tasks': policy['max_tasks'], 'system_max_tasks': policy['system_max_tasks']}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(2)

def enforce_task_output_paths(tasks: list[dict], *, phase: str) -> None:
    """Reject a task whose explicit `output` points into a protected directory
    (skill install directory / runtime batch directory).

    A legitimate workspace explicit output (including CJK, spaces, relative paths) is unaffected;
    only paths under a protected root are rejected.
    """
    for (index, task) in enumerate(tasks, start=1):
        raw = task.get('output') if isinstance(task, dict) else None
        if raw is None or str(raw).strip() == '':
            continue
        candidate = Path(str(raw))
        if is_protected_output_path(candidate):
            print(json.dumps({
                'error': 'BATCH_OUTPUT_PATH_FORBIDDEN',
                'message': (
                    f'batch task #{index} has an output inside a protected directory (skill install directory or runtime batch directory), so it was rejected. '
                    'Use a workspace output directory instead, or omit the field to use the default batch output directory.'
                ),
                'task_index': index,
                'output': str(candidate),
                'phase': phase,
            }, ensure_ascii=False, indent=2), file=sys.stderr)
            sys.exit(2)

def build_batch_child_command(task: dict) -> list[str]:
    """Assemble the dispatch.py subprocess command for one batch task.
    Must use DISPATCH_SCRIPT, not Path(__file__) (batch.py has no main and would exit silently).
    """
    command = [sys.executable, str(dispatch_common.DISPATCH_SCRIPT), '--model', str(task['model'])]
    if task.get('prompt'):
        command += ['--prompt', str(task['prompt'])]
    command += build_cli_arg_list('--image', task.get('image'))
    command += build_cli_arg_list('--audio', task.get('audio'))
    command += build_cli_arg_list('--video', task.get('video'))
    if task.get('aspect_ratio'):
        command += ['--aspect-ratio', str(task['aspect_ratio'])]
    if task.get('resolution'):
        command += ['--resolution', str(task['resolution'])]
    if task.get('steps') is not None:
        command += ['--steps', str(task['steps'])]
    if task.get('duration') is not None:
        command += ['--duration', str(task['duration'])]
    if task.get('seed') is not None:
        command += ['--seed', str(task['seed'])]
    if task.get('random_seed'):
        command += ['--random-seed']
    if task.get('model_name'):
        command += ['--model-name', str(task['model_name'])]
    if task.get('width') is not None:
        command += ['--width', str(task['width'])]
    if task.get('height') is not None:
        command += ['--height', str(task['height'])]
    command += build_cli_arg_list('--param', task.get('param'))
    command += ['--output', str(task['output_path'])]
    if task.get('sync'):
        command += ['--sync']
    if task.get('run_mode') == 'run_with_defaults':
        command += ['--run-with-defaults']
    else:
        command += ['--confirm-run']
    return command

def launch_logged_process(command: list[str], log_path: Path, *, env: dict[str, str] | None=None) -> tuple[subprocess.Popen, object]:
    log_handle = open(log_path, 'w', encoding='utf-8')
    proc = subprocess.Popen(command, stdout=log_handle, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL, env=env)
    return (proc, log_handle)

def append_supervisor_log(log_path: Path, message: str) -> None:
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, 'a', encoding='utf-8') as handle:
        handle.write(f'[{timestamp}] {message}\n')

def mark_batch_tasks_failed(payload: dict, *, reason: str, running_only: bool=False) -> None:
    for task in payload.get('tasks') or []:
        if not isinstance(task, dict):
            continue
        if running_only and task.get('status') != 'running':
            continue
        if not running_only and task.get('status') not in {'queued', 'running'}:
            continue
        task['status'] = 'failed'
        task.setdefault('failure_reason', reason)

def handle_batch_worker(worker_file: str) -> None:
    worker_path = Path(worker_file)
    payload = json.loads(worker_path.read_text(encoding='utf-8'))
    tasks = payload.get('tasks') or []
    concurrency = int(payload.get('effective_concurrency') or CONFIGURED_MAX_BATCH_CONCURRENCY_FALLBACK)
    status_path = Path(payload['status_path'])
    supervisor_log = Path(payload.get('supervisor_log') or status_path.with_name('batch-supervisor.log'))
    heartbeat_path = Path(payload.get('worker_heartbeat_path') or status_path.with_name(BATCH_HEARTBEAT_FILENAME)).resolve()
    payload['worker_heartbeat_path'] = str(heartbeat_path)
    payload['worker_pid'] = os.getpid()
    payload['scheduler_pid'] = os.getpid()
    payload['scheduler_status'] = 'running'
    payload['started_at'] = int(time.time())
    write_batch_status_file(status_path, payload)
    write_worker_heartbeat(heartbeat_path)
    append_supervisor_log(supervisor_log, f'worker started with {len(tasks)} task(s), concurrency={concurrency}')
    active: list[dict] = []
    next_index = 0
    try:
        while next_index < len(tasks) or active:
            write_worker_heartbeat(heartbeat_path)
            while next_index < len(tasks) and len(active) < concurrency:
                task = tasks[next_index]
                child_env = dict(os.environ)
                child_env[RUNTIME_STATE_DISABLED_ENV] = '1'
                (proc, log_handle) = launch_logged_process(build_batch_child_command(task), Path(task['log_path']), env=child_env)
                task['pid'] = proc.pid
                task['status'] = 'running'
                append_supervisor_log(supervisor_log, f"launched task#{task.get('index')} pid={proc.pid} label={task.get('label')}")
                active.append({'proc': proc, 'log_handle': log_handle, 'task': task})
                next_index += 1
                write_batch_status_file(status_path, payload)
            if not active:
                break
            time.sleep(1)
            write_worker_heartbeat(heartbeat_path)
            still_active: list[dict] = []
            for entry in active:
                proc = entry['proc']
                task = entry['task']
                ret = proc.poll()
                if ret is None:
                    still_active.append(entry)
                    continue
                entry['log_handle'].close()
                task['exit_code'] = ret
                if ret != 0:
                    task['status'] = 'failed'
                else:
                    manifest, manifest_error, manifest_detail = inspect_task_run_manifest(task.get('output_path'))
                    if manifest_error:
                        task['status'] = 'failed'
                        task['failure_reason'] = manifest_error
                        task['manifest_error'] = manifest_detail
                        append_supervisor_log(
                            supervisor_log,
                            f"task#{task.get('index')} pid={task.get('pid')} exit_code=0 but run.json check failed: {manifest_error}: {manifest_detail}",
                        )
                    else:
                        task['status'] = 'completed'
                        task['request_id'] = manifest.get('request_id')
                        task['saved_files'] = manifest.get('saved_files') or []
                        task['run_manifest'] = manifest.get('manifest_path') or manifest_detail
                        task['download_errors'] = manifest.get('download_errors') or []
                append_supervisor_log(supervisor_log, f"task#{task.get('index')} pid={task.get('pid')} finished status={task['status']} exit_code={ret}")
                write_batch_status_file(status_path, payload)
            active = still_active
    except Exception:
        mark_batch_tasks_failed(payload, reason='worker_crashed')
        payload['scheduler_status'] = 'failed'
        payload['worker_error'] = traceback.format_exc()
        write_batch_status_file(status_path, payload)
        append_supervisor_log(supervisor_log, payload['worker_error'].rstrip())
        raise
    finally:
        for entry in active:
            try:
                entry['log_handle'].close()
            except Exception:
                pass
    payload['scheduler_status'] = 'completed'
    payload['finished_at'] = int(time.time())
    write_batch_status_file(status_path, payload)
    append_supervisor_log(supervisor_log, 'worker finished')

def handle_batch_prefill(base_args: argparse.Namespace) -> None:
    """Batch prefill cards. Every task is rendered via route B (driven by the remote contract).

    per-batch contract cache (_info_cache): the same model id is fetched only once per batch.
    For example, 5 tasks all using ChatGPT Image2 hit the BizyAir API only once for the contract.
    """
    (tasks, _) = parse_batch_payload_or_exit(base_args.batch_json)
    policy = resolve_batch_policy()
    enforce_batch_task_limits(base_args, task_count=len(tasks), policy=policy, phase='batch_prefill')
    dispatch_common.ensure_api_key_ready(base_args.api_key)
    _info_cache: dict[str, dict] = {}
    payload_tasks = []
    for (index, task) in enumerate(tasks, start=1):
        task_args = build_task_args(base_args, task)
        model = task_args.model
        if task_args.scene:
            model = SCENE_NUMBER_MAP.get(task_args.scene, task_args.scene)
        target = dispatch_common.resolve_model(task_args.task, model)
        reject_search_flow_target(target, task_index=index, phase='batch_prefill')
        card.apply_compiled_selection(task_args, target)
        is_endpoint_target = dispatch_common.is_modelzoo_endpoint(target)
        if target in ROUTE_TABLE or is_endpoint_target:
            # Fixed model or arbitrary ModelZoo endpoint: render the prefill card from remote detail.
            import modelzoo
            import api as _api
            endpoint = ROUTE_TABLE[target]['endpoint'] if target in ROUTE_TABLE else target
            display_name = dispatch_common.display_model_name(target) if target in ROUTE_TABLE else endpoint
            cache_key = endpoint
            if cache_key not in _info_cache:
                key = _api.require_api_key(base_args.api_key)
                detail_result = modelzoo.get_detail(key, endpoint)
                _api.require_ok(detail_result, f'batch prefill detail (task {index})')
                detail_data = (detail_result.get('data') or {}).get('data') or detail_result.get('data') or {}
                if not (detail_data.get('input_params') or []):
                    print(json.dumps({'error': 'MODELZOO_DETAIL_EMPTY', 'message': f'Cannot fetch the parameter definition for {endpoint}; the batch parameter cards were not generated', 'task_index': index, 'endpoint': endpoint}, ensure_ascii=False), file=sys.stderr)
                    sys.exit(1)
                _info_cache[cache_key] = detail_data
            detail_data = _info_cache[cache_key]
            input_params = detail_data.get('input_params') or []
            try:
                user_params = card.collect_modelzoo_params(target, task_args)
            except modelzoo.ParamError as exc:
                fail_invalid_params(exc, task_index=index, endpoint=endpoint)
            normalized_params, issues = modelzoo.validate_user_params(
                detail_data,
                user_params,
                require_all=False,
                supplied_media_types=_supplied_media_types(task_args),
            )
            if issues:
                fail_invalid_params(modelzoo.ParamError(issues), task_index=index, endpoint=endpoint)
            param_lines = card.render_modelzoo_param_lines(input_params, normalized_params)
            card_markdown = f"**{display_name}** (ModelZoo: `{endpoint}`)\n" + '\n'.join(param_lines)
        else:
            # Not a fixed model: legacy webapp path
            app_id = target
            if app_id not in _info_cache:
                _info_cache[app_id] = remote.build_remote_info_output(app_id, api_key_arg=base_args.api_key, include_workflow=True)
            info_out = _info_cache[app_id]
            summary = info_out.get('summary') or {}
            card_markdown = remote.build_remote_prefilled_card(summary, task_args, resolved_object_id=app_id)
        payload_tasks.append({'index': index, 'label': task_label(task, index), 'model': target, 'display_name': dispatch_common.display_model_name(target), 'card_markdown': card_markdown})
    print(json.dumps({'mode': 'batch_prefill', 'task_count': len(payload_tasks), 'tasks': payload_tasks, 'reply_markdown': build_batch_prefill_reply(payload_tasks)}, ensure_ascii=False, indent=2))

def handle_batch_run(base_args: argparse.Namespace) -> None:
    (tasks, requested_concurrency) = parse_batch_payload_or_exit(base_args.batch_json)
    enforce_task_output_paths(tasks, phase='batch_run')
    try:
        (requested_concurrency, effective_concurrency, policy) = resolve_batch_concurrency(base_args, requested_concurrency, len(tasks))
    except ValueError as exc:
        print(json.dumps({
            'error': 'BATCH_CONCURRENCY_INVALID',
            'message': str(exc) or 'batch concurrency is invalid',
            'requested_concurrency': getattr(base_args, 'batch_concurrency', None),
            'phase': 'batch_run',
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(2)
    enforce_batch_task_limits(base_args, task_count=len(tasks), policy=policy, phase='batch_run')
    if requested_concurrency > policy['system_max_concurrency']:
        print(json.dumps({'error': 'BATCH_CONCURRENCY_LIMIT_EXCEEDED', 'message': f"Requested concurrency {requested_concurrency} exceeds hard system max {policy['system_max_concurrency']}. Lower the concurrency and try again.", 'requested_concurrency': requested_concurrency, 'configured_max_concurrency': policy['max_concurrency'], 'system_max_concurrency': policy['system_max_concurrency']}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(2)
    if requested_concurrency > policy['max_concurrency'] and (not getattr(base_args, 'batch_concurrency_approved', False)):
        print(json.dumps({'error': 'BATCH_CONCURRENCY_REQUIRES_APPROVAL', 'message': f"Requested concurrency {requested_concurrency} exceeds configured max_concurrency {policy['max_concurrency']}. Ask for explicit user approval, then rerun with --batch-concurrency-approved.", 'requested_concurrency': requested_concurrency, 'configured_max_concurrency': policy['max_concurrency'], 'system_max_concurrency': policy['system_max_concurrency']}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(2)
    batch_name, batch_dir, batch_output_dir = create_unique_batch_dirs()
    # Status / logs are runtime state; task-XX-output is user output and goes to the workspace output root.
    status_path = batch_dir / 'batch-status.json'
    worker_file = batch_dir / 'batch-payload.json'
    supervisor_log = batch_dir / 'batch-supervisor.log'
    heartbeat_path = batch_dir / BATCH_HEARTBEAT_FILENAME
    watchdog_log = batch_dir / BATCH_WATCHDOG_LOG_FILENAME
    payload_tasks = []
    _info_cache: dict[str, dict] = {}
    for (index, task) in enumerate(tasks, start=1):
        task_args = build_task_args(base_args, task)
        model = task_args.model
        if task_args.scene:
            model = SCENE_NUMBER_MAP.get(task_args.scene, task_args.scene)
        target = dispatch_common.resolve_model(task_args.task, model)
        reject_search_flow_target(target, task_index=index, phase='batch_run')
        card.apply_compiled_selection(task_args, target)
        is_endpoint_target = dispatch_common.is_modelzoo_endpoint(target)
        if target in ROUTE_TABLE or is_endpoint_target:
            # Free preflight before submitting a batch: parameter errors must surface before any subprocess starts.
            import modelzoo
            import api as _api
            endpoint = ROUTE_TABLE[target]['endpoint'] if target in ROUTE_TABLE else target
            if endpoint not in _info_cache:
                key = _api.require_api_key(base_args.api_key)
                detail_result = modelzoo.get_detail(key, endpoint)
                _api.require_ok(detail_result, f'batch run preflight detail (task {index})')
                detail_data = (detail_result.get('data') or {}).get('data') or detail_result.get('data') or {}
                if not (detail_data.get('input_params') or []):
                    print(json.dumps({'error': 'MODELZOO_DETAIL_EMPTY', 'message': f'Cannot fetch the parameter definition for {endpoint}; the batch was not submitted', 'task_index': index, 'endpoint': endpoint}, ensure_ascii=False), file=sys.stderr)
                    sys.exit(1)
                _info_cache[endpoint] = detail_data
            try:
                user_params = card.collect_modelzoo_params(target, task_args)
            except modelzoo.ParamError as exc:
                fail_invalid_params(exc, task_index=index, endpoint=endpoint)
            _, issues = modelzoo.validate_user_params(
                _info_cache[endpoint],
                user_params,
                supplied_media_types=_supplied_media_types(task_args),
            )
            if issues:
                fail_invalid_params(modelzoo.ParamError(issues), task_index=index, endpoint=endpoint)
        output_path = str((batch_output_dir / f'task-{index:02d}-output').resolve())
        output_candidate = Path(task.get('output') or output_path)
        if not output_candidate.suffix:
            # app.py downloads before creating the directory; pre-create the task dir so
            # files and run.json stay together. A suffix path is a file base and is left alone.
            output_candidate.mkdir(parents=True, exist_ok=True)
        log_path = batch_dir / f'task-{index:02d}.log'
        if task_args.sync:
            child_sync = True
        else:
            child_sync = False
        if should_batch_use_defaults(task_args, target):
            run_mode = 'run_with_defaults'
        else:
            run_mode = 'confirm_run'
        payload_tasks.append({'index': index, 'label': task_label(task, index), 'model': target, 'display_name': dispatch_common.display_model_name(target), 'status': 'queued', 'log_path': str(log_path.resolve()), 'output_path': str(Path(task.get('output') or output_path).resolve()), 'prompt': task_args.prompt, 'image': list(task_args.image or []), 'audio': list(task_args.audio or []), 'video': list(task_args.video or []), 'aspect_ratio': task_args.aspect_ratio, 'resolution': task_args.resolution, 'steps': task_args.steps, 'duration': task_args.duration, 'seed': task_args.seed, 'random_seed': bool(task_args.random_seed), 'model_name': task_args.model_name, 'width': task_args.width, 'height': task_args.height, 'param': list(task_args.param or []), 'sync': child_sync, 'run_mode': run_mode, 'command_preview': redact_command_args(build_batch_child_command({'model': target, 'prompt': task_args.prompt, 'image': list(task_args.image or []), 'audio': list(task_args.audio or []), 'video': list(task_args.video or []), 'aspect_ratio': task_args.aspect_ratio, 'resolution': task_args.resolution, 'steps': task_args.steps, 'duration': task_args.duration, 'seed': task_args.seed, 'random_seed': bool(task_args.random_seed), 'model_name': task_args.model_name, 'width': task_args.width, 'height': task_args.height, 'param': list(task_args.param or []), 'output_path': str(Path(task.get('output') or output_path).resolve()), 'sync': child_sync, 'run_mode': run_mode}))})
    reply_markdown = f"These tasks are queued for batch execution as separate tasks.\n\n- Configured task limit: {policy['max_tasks']}\n- Task count this time: {len(payload_tasks)}\n- Configured concurrency limit: {policy['max_concurrency']}\n- Concurrency you requested: {requested_concurrency}\n- Effective concurrency limit: {effective_concurrency}\n\n" + build_batch_run_reply(payload_tasks)
    payload = {'mode': 'batch_run', 'version': 2, 'task_count': len(payload_tasks), 'requested_concurrency': requested_concurrency, 'effective_concurrency': effective_concurrency, 'configured_max_concurrency': policy['max_concurrency'], 'system_max_concurrency': policy['system_max_concurrency'], 'configured_max_tasks': policy['max_tasks'], 'system_max_tasks': policy['system_max_tasks'], 'batch_dir': str(batch_dir.resolve()), 'batch_output_dir': str(batch_output_dir.resolve()), 'status_path': str(status_path.resolve()), 'supervisor_log': str(supervisor_log.resolve()), 'worker_heartbeat_path': str(heartbeat_path.resolve()), 'scheduler_status': 'queued', 'reply_markdown': reply_markdown, 'tasks': payload_tasks}
    write_batch_status_file(status_path, payload)
    write_json_file_atomic(worker_file, payload)
    worker_env = dict(os.environ)
    if base_args.api_key:
        worker_env['BIZYAIR_API_KEY'] = base_args.api_key
    worker_env[RUNTIME_STATE_DISABLED_ENV] = '1'
    log_handle = open(supervisor_log, 'a', encoding='utf-8')
    try:
        worker_proc = subprocess.Popen([sys.executable, str(dispatch_common.DISPATCH_SCRIPT), '--batch-worker-file', str(worker_file.resolve())], stdout=log_handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace', env=worker_env, start_new_session=True, close_fds=True)
    except Exception as exc:
        log_handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] failed to launch batch worker: {exc}\n")
        log_handle.flush()
        payload['scheduler_status'] = 'failed'
        payload['worker_error'] = f'failed to launch batch worker: {exc}'
        payload['watchdog_status'] = 'not_started'
        mark_batch_tasks_failed(payload, reason='worker_launch_failed')
        write_batch_status_file(status_path, payload)
        raise
    finally:
        log_handle.close()
    # The watchdog must be started before the parent process returns; it runs in its own session,
    # so it can still settle the final state after the worker is SIGKILLed.
    try:
        watchdog_proc = launch_batch_watchdog(status_path, worker_proc.pid, watchdog_log)
        watchdog_pid = watchdog_proc.pid
        watchdog_status = 'running'
    except Exception as exc:
        watchdog_pid = None
        watchdog_status = 'launch_failed'
        append_supervisor_log(supervisor_log, f'failed to launch batch watchdog: {exc}')
    payload = update_batch_status_if_not_final(status_path, {
        'scheduler_pid': worker_proc.pid,
        'scheduler_status': 'running',
        'watchdog_pid': watchdog_pid,
        'watchdog_status': watchdog_status,
        'watchdog_log': str(watchdog_log.resolve()),
        'reply_markdown': reply_markdown,
    })
    print(json.dumps(build_public_batch_payload(payload), ensure_ascii=False, indent=2))
