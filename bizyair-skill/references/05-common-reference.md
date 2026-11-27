# 05 Common conventions

## Environment variables

BizyAir splits its API across three hosts, and there are **no `/w` `/x` `/y` prefixes**:

```bash
export API_BASE="https://api.bizyair.ai"          # tasks / upload / wallet / modelzoo tasks
export META_BASE="https://meta.bizyair.ai"        # AI app details / model hub / modelzoo list·detail
export PRICING_BASE="https://pricing.bizyair.ai"  # price table
export TOKEN="<api-key>"
```

Auth headers: `Authorization: Bearer ${TOKEN}` + `lang: en` + `User-Agent: Mozilla/5.0` (required by Cloudflare)

### Language and host overrides (v1.11)

`lang` defaults to **en** (BizyAir); to temporarily switch to Chinese or another language, use the environment variable `BIZYAIR_LANG=zh`, or write this in `config.json`:

```json
{"client": {"lang": "zh"}}
```

The hosts default to `api.bizyair.ai` / `meta.bizyair.ai` / `pricing.bizyair.ai`, and can be rewritten as a whole (staging, self-hosted proxy):

```bash
export BIZYAIR_API_HOST="https://api-staging.example.com"
export BIZYAIR_META_HOST="https://meta-staging.example.com"
export BIZYAIR_PRICING_HOST="https://pricing-staging.example.com"
```

They can also be set in `client.api_host` / `client.meta_host` / `client.pricing_host`. The override only changes the request URL; command usage is unchanged.

### ModelZoo task paths

Create/query must use the `openapi` path: `/v1/modelzoo/tasks/openapi/{endpoint}`, `/v1/modelzoo/tasks/openapi/{request_id}`.
Non-openapi paths reject base64 data URLs (`value must be an absolute URL`).

### Local path variables

The skill install directory is read-only; runtime state and outputs are not written there by default.

| Variable | Purpose | Default |
|---|---|---|
| `BIZYAIR_STATE_DIR` | Runtime state / batch state root (highest priority) | macOS `~/Library/Application Support/bizyair`; Linux `$XDG_STATE_HOME/bizyair` or `~/.local/state/bizyair` |
| `BIZYAIR_HOME` | Serves as both the runtime state root and the output root | Uses the system state directory when unset |
| `BIZYAIR_OUTPUT_DIR` | Single-task output root | Current workspace `bizyair-outputs/`; falls back to the state directory when run from inside the skill directory |

When `BIZYAIR_HOME` is set, outputs go to `$BIZYAIR_HOME/outputs` by default.

## Resume commands (v1.11)

| Command | Purpose |
|---|---|
| `cli.py status <request_id>` | Check webapp task status once (no polling, no billing) |
| `cli.py outputs <request_id> [-o dir]` | Fetch webapp task outputs; downloads when `-o` is given |
| `cli.py upload <local file>` | Upload any local file → OSS URL (for assembling your own payload) |
| `cli.py modelzoo-md [--save]` | Export the full ModelZoo catalog (235 entries as currently measured; the count varies with the platform) |
| `cli.py app-md [--save]` | Export the full community AI app catalog (215 entries as currently measured; the count varies with the platform) |

## Pagination

- `current`: page number, starting at 1
- `page_size`: items per page (10/20/50 recommended)
- Returns: `{list, total, current, page_size}` (note it is `list`, not `records`)

## Interpreting responses

```
HTTP 200 + code=20000 → request succeeded (but the task may be Failed; check data.status)
HTTP 202             → async-mode webapp create succeeded (bare body {request_id})
HTTP 400 + code=20015 → invalid parameter
HTTP 400 + code=40025 → webapp is missing input_values
HTTP 500 + empty body → parameter type error (the server gave no message)
```

## CLI exit codes

| Exit code | Meaning | Typical case |
|---|---|---|
| 0 | Success | Task completed, query succeeded |
| 1 | Dependency / network / server failure | `REMOTE_REQUEST_FAILED`, task `Failed`, key check failed |
| 2 | Invalid input or parameter | Unknown `--param`, invalid combo, missing required prompt, invalid link / ID, missing subcommand argument |
| 3 | Parameter card produced but not confirmed | `modelzoo-run` / `batch-run` without `--confirm-run` |

Agents must not treat exit codes 2 / 3 as network faults that need a retry.

## Common enums

- `model_types`: Model / Workflow / Application
- `sort` (bizy_models): the platform accepts Recently / Most Forked / Most Used / Most Liked / Most Downloaded / Auto (**required**). `cli.py models` currently accepts only **Most Used / Recently / Auto**; other values are rejected locally with exit 2.
- `sort` (modelzoo/list): Auto / Recently / Most Used etc. (**optional**)
- `sort` (creations): the platform accepts Recently / Recommend / Hotest. `cli.py gallery` currently accepts only **Recently / Recommend**; other values are rejected locally with exit 2.
- `call_type`: comfy_task / trd_api_record

## Error codes (retryable vs non-retryable)

### Retryable (may recover with another key manually, or a later retry)

| Code | Meaning | Suggestion |
|---|---|---|
| 20049/20050/20051 | Insufficient balance | Switch to a key that has balance |
| 30039 | Maximum queue size reached | Retry later |
| 30040 | Maximum concurrency reached | Wait for the current tasks to finish |
| 30015/30016/30018 | No available nodes | That model is temporarily unavailable |
| 50600-50604 | Rate limited | Too many requests; retry later |
| HTTP 429 | Gateway rate limit | Same as above |
| HTTP 402 | Billing problem | Same as insufficient balance |

### Non-retryable (client-side problem)

| Code | Meaning |
|---|---|
| 20015 | Invalid parameter |
| 20251 | Third-party API node not found |
| 40025 | Missing input_values |

## When authentication is needed

- **No key:** menus, `browse`, `models`, `model-types`, `search`, `search-workflows`, `pick-image` / `pick-video`, `batch-status`.
- **Key required:** task runs (`run`, `prefill`, `modelzoo-run`, batch), account reads/writes, `upload`, `status` / `outputs`, `download-workflow`.
- **Public but skill-gated:** `gallery*` reads, `modelzoo-list/detail/price`, `pick-modelzoo-*`, `info`, `model-detail`, `app-md`, `modelzoo-md`. The platform serves them anonymously; the skill asks for a key as a safety default.


## Output URL caveats

- Most land in BizyAir's own OSS (`bizyair-prod.oss-cn-shanghai.aliyuncs.com` / `storage.bizyair.ai`)
- Some channel variants land in third-party temporary storage (`tempfile.aiquickdraw.com` / `s3.6scloud.com`)
- AI app tasks have `expired_at` (expires after ~15 days)
- **Download promptly; never rely on the original URL staying accessible forever**

## API Key display

Keys in responses are always masked: `sk-wmul****...phzh`. Keep the same format when the skill displays its own key.

## cancel / interrupt

Neither webapp nor modelzoo has a public cancel endpoint (404). When a user asks how to cancel, say "the BizyAir public API does not support cancellation".
