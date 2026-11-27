# 06 Batch processing rules

## When this applies

Use this document when the user says "multiple tasks / handle them separately / do not merge / in parallel / batch".

## Core rules

1. **No execution without a go-ahead**: without "start / just run it / confirmed, execute", the skill must stop at the `batch-prefill` stage; `batch-run` without `--confirm-run` only produces the card and exits with code 3, and creates no tasks
2. **Do not loop single-task calls**: multiple tasks must go through `batch-prefill`; do not loop the single-task `--run`
3. **Concurrency cap**: default 3 on the user side, read from `batch.max_concurrency` in `config.json`
4. **Task count cap**: default 5 on the user side, read from `batch.max_tasks` in `config.json`
5. **Over-limit needs confirmation**: when the configured limits are exceeded, the user must confirm first
6. **Cards must carry actual values**: the parameter card from `batch-prefill` / `batch-run` shows the actual value of each field (including remote defaults), not just the field names
7. **Parameter pre-check before submission**: `batch-run --confirm-run` first schema-validates every ModelZoo task (fixed route or arbitrary endpoint); unknown parameters, invalid combos, missing prompts etc. exit with code 2 immediately, and **no child process is started**
8. **Results are written per task**: each task's output directory contains `run.json`; the batch state file is backfilled with `saved_files` / `run_manifest` / `request_id`
9. **Validate the manifest even on exit code 0**: when the child process exits successfully but `run.json` is missing, empty, not an object or has broken JSON, the task must be `failed`, with `failure_reason` (`run_manifest_missing` / `run_manifest_invalid`) and `manifest_error` written
10. **Crashes must converge**: batch directories are created with `mkdir(exist_ok=False)`; a same-second collision gets a random suffix appended automatically; `batch-status.json` is written atomically with a lock + temporary file + `fsync` + `os.replace()`. An independent watchdog monitors worker heartbeats; after a worker is SIGKILLed or its heartbeat times out, scheduler / queued / running tasks are marked `failed`
11. **Invalid concurrency fails structurally**: `--batch-concurrency 0` or a non-integer outputs `BATCH_CONCURRENCY_INVALID` and exits 2, without a Python traceback
12. **Per-task output is bounded**: if a task's explicit `output` resolves inside the skill install directory or the runtime batch directory, the command outputs `BATCH_OUTPUT_PATH_FORBIDDEN` and exits 2, and **no child process starts**; ordinary workspace paths (including relative paths, Chinese characters, spaces) are allowed as usual
13. **Query batches with the command**: use `batch-status` to check batch status; do not page through `batch-status.json` by hand

Batch tasks support fixed menu models, arbitrary ModelZoo endpoint IDs, and AI apps. Put an endpoint ID in `--model` (or in a task's `model` field). BizyAir membership can unlock higher concurrency;
tune the local defaults in `config.json` to match your workflow.

## Commands

### Batch prefill

```bash
python3 scripts/cli.py batch-prefill --model <ROUTE_ID_OR_ENDPOINT> --batch-json '[{"label":"A","prompt":"white cat"},{"label":"B","prompt":"black dog"}]'
```

For long payloads, write the JSON to a UTF-8 file instead of escaping it inline:

```bash
python3 scripts/cli.py batch-prefill --model <ROUTE_ID_OR_ENDPOINT> --batch-json-file ./batch.json
python3 scripts/cli.py batch-run --model <ROUTE_ID_OR_ENDPOINT> --batch-json @./batch.json --confirm-run
```

`--batch-json` and `--batch-json-file` are mutually exclusive; `@path` is a shorthand for the file form.

### Batch execution (after user confirmation)

```bash
python3 scripts/cli.py batch-run --model <ROUTE_ID_OR_ENDPOINT> --batch-json '[...]' --confirm-run
```

### Raise concurrency (requires user confirmation)

```bash
python3 scripts/cli.py batch-run --model <ROUTE_ID_OR_ENDPOINT> --batch-concurrency 4 --batch-concurrency-approved --confirm-run --batch-json '[...]'
```

### Exceed the task count (requires user confirmation)

```bash
python3 scripts/cli.py batch-run --model <ROUTE_ID_OR_ENDPOINT> --batch-task-count-approved --confirm-run --batch-json '[...]'
```

### Query batch status

```bash
python3 scripts/cli.py batch-status <batch directory or batch-status.json>
python3 scripts/cli.py batch-status            # omit the path: take the latest batch under the runtime batch_runs
```

Outputs condensed JSON: `scheduler_status`, `watchdog_status`, `task_status_counts`, plus each task's
`status` / `exit_code` / `request_id` / `failure_reason` / `saved_files`. When the path is not found it outputs
`BATCH_STATUS_NOT_FOUND` and exits 2.

## Configuration

```json
{
  "batch": {
    "max_concurrency": 3,
    "max_tasks": 5
  }
}
```

Higher hard system limits are enforced inside the scripts and are not exposed as user configuration.

## Output and state

- Batch directory: `<runtime>/batch_runs/<YYYYMMDD-HHMMSS>[-random suffix]/`
  - If a batch already exists in the same second, a random suffix is appended; old state is never overwritten.
  - Holds `batch-status.json`, `batch-payload.json`, `batch-worker-heartbeat.json`, `batch-watchdog.log`, `task-XX.log`, `batch-supervisor.log`.
  - `<runtime>` defaults to the user state directory; `BIZYAIR_STATE_DIR` can override it.
  - Never written into the skill install directory.
- Task output directory: `<output_root>/batch_runs/<YYYYMMDD-HHMMSS>[-random suffix]/task-XX-output/`
  - `<output_root>` defaults to `bizyair-outputs/` in the current workspace; `BIZYAIR_OUTPUT_DIR` can override it.
  - Can be overridden per task with the `output` field; but if it resolves inside the skill install directory or the runtime batch directory it is rejected (`BATCH_OUTPUT_PATH_FORBIDDEN`, exit 2).
- Per task: `task-XX.log` + `task-XX-output/`
- `task-XX-output/run.json`: `request_id`, `outputs`, `saved_files`, `download_errors`, `params`, `manifest_path`
- `batch-status.json`: after each task finishes it is backfilled with `saved_files`, `run_manifest`, `request_id`
  - `scheduler_status`: `queued` / `running` / `completed` / `failed`
  - Worker SIGKILLed or heartbeat timeout: `scheduler_status=failed`, `watchdog_status=failed`, `worker_error` explains why; queued and running tasks are marked `failed`
  - Manifest problem: the task is `failed`, carrying `failure_reason` / `manifest_error`
  - State writes are atomic; other processes can read at any time and never see half-written JSON
- `batch-supervisor.log`: worker scheduling log
- `batch-watchdog.log`: watchdog heartbeat / crash-convergence log

> Tip: use `cli.py batch-status` (latest batch by default) to check batch status; you do not need to page through `batch-status.json`.
> `--batch-concurrency 0` returns `BATCH_CONCURRENCY_INVALID` and exits 2, no longer throwing a traceback.

When replying to the user, prefer the local paths in `saved_files`; remote URLs in `outputs` expire.

