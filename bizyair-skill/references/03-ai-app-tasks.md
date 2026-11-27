# 03 AI app tasks

For parameter preparation, task creation, status queries and result retrieval for AI apps.

## Paid confirmation and reruns (v1.12.6)

- **One confirmation submits once**: when the user says "start", it authorizes only the current parameters and this one run, not "run until satisfied".
- **Do not rerun automatically on dissatisfaction**: show the output and the problem first; only after the user explicitly says "rerun / run again / change parameters" may you submit again, and reconfirm the cost.
- **Check first when the submit result is unknown**: if no request_id was obtained, check `credits` for a charge (and `batch-status` for a batch); `status` needs a task ID, so ask the user for it or use the BizyAir task page. Never rerun directly. `run` has automatic submit retries disabled (`WEBAPP_RETRY_LIMIT=0`).

## Standard flow

1. `GET https://meta.bizyair.ai/v1/webapp/:webapp_id/detail` → get `data.id` (= web_app_id) + `data.input_nodes`
2. Build `input_values` from `input_nodes` (variable_name → value)
3. `POST https://api.bizyair.ai/v1/webapp/task/openapi/create` to create the task
4. Poll `GET https://api.bizyair.ai/v1/webapp/task/openapi/{request_id}` (**path-segment style**, not `?requestId=`)
5. On success, `GET https://api.bizyair.ai/v1/webapp/task/openapi/{request_id}/outputs` to get the results
6. When downloading result URLs you must send a `User-Agent` (`storage.bizyair.ai` is behind Cloudflare; bare urllib gets 403)

## Key rules

### web_app_id source

- `/v1/webapp/{id}/detail` expects the `web_app_id` / version ID (e.g. 47362 or 57978), **not** the model asset `bizy_model_id`.
- The returned `data.id` is the `web_app_id`; create must use `data.id`.
- If you only have the asset ID, first resolve `versions[0].id` from the community/official list, then call the webapp endpoint with that version ID.

### Two ID namespaces (fixed in v1.10.1)

| ID | Example | Where it works |
|---|---|---|
| `bizy_model_id` | 56818 | Community list object ID; `/v1/webapp/{id}/detail` **does not recognize it** |
| `web_app_id` / `version_id` | 57978 | `/v1/webapp/{id}/detail`, `/v1/bizy_models/versions/{id}`, web `/community/app/{id}` |

- Querying webapp detail with `bizy_model_id` returns data without `id`, so the old logic judged `missing_webapp_mapping`
- Now `resolve_remote_execution_target` retries automatically with the already-resolved version ID, and `preflight` also
  recovers `versions[0].id` from the community/official list when `/v1/bizy_models/{id}/detail` returns 400 (code 20015)
- When a web URL is available, prefer the version ID in the URL; it saves one list lookup (about 7 seconds saved)

### Numeric ID collisions (v1.12.2)

The same number can be both an app version ID and a model hub asset ID. Measured on `56747`:

- `/v1/webapp/56747/detail` → app "Replace White Background - QWen Image Edit 2511"
- `/v1/bizy_models/56747/detail` → LoRA `minimax_h3_flf2v_lora_v1.safetensors`
- Both endpoints return 200; the old logic took model detail first, so `info` resolved the app as a LoRA.

Now `info` / `prefill` / `run` prefer resolving numeric IDs as webapp app versions; to look up the model hub object use
`cli.py model-detail 56747`. `pick-image`'s `id` shares its source with `page_url` (the version ID for apps); the
model hub asset ID is in `bizy_model_id` / `asset_id`; run/prefill use `id` or `page_url`.

### Single source of truth for IDs (v1.12.3)

- The app main ID on the `prefill` card is the execution version ID; when the input is an asset ID it is marked separately as "input ID".
- `info`'s top-level `page_object_id` / `execution_object_id` are both the app version ID, while `resolved_object_id` keeps the input ID for traceability.
- `download-workflow` likewise prefers resolving as a webapp app; the exported `page_url` uses the version ID.

### input_nodes → input_values

`input_nodes` is an ordered list, each item containing `variable_name` (e.g. `93:CLIPTextEncode.text`).

At create time `input_values` is a dict of `{variable_name: value}`:
```json
{
  "web_app_id": 47362,
  "input_values": {
    "93:CLIPTextEncode.text": "1girl, smile",
    "47:EmptyLatentImage.width": 960
  }
}
```

- You do not need to fill everything: the server falls back to the webapp's default for fields not passed
- But `input_values` cannot be an empty dict `{}` (it reports 40025)
- Pass at least 1 key (usually prompt)

### Async mode (default)

With the `X-Bizyair-Task-Async: enable` header:
- HTTP **202**, returns `{"request_id": "..."}` immediately in 345ms (bare body, no code/message wrapper)
- Then poll detail → outputs

Without the header (sync mode):
- HTTP 200, blocks for 30-120s and returns the complete result directly (including outputs)
- Only as a debugging fallback

### suppress_preview_output

- Not passed or `false`: outputs are returned normally
- `true`: the outputs endpoint returns null (**do not pass true**)

## Parameter card rules

### Ordering

1. Prompt / materials
2. Model tier (shown only when there are multiple choices)
3. Aspect ratio / size / resolution
4. Duration (video)
5. Randomness / seed
6. Other

### Default is not execution

"default" / "you decide" / "go with the recommendation" → only authorizes prefilling the parameter card, **not execution**.
"start" / "just run" / "confirm execution" → only then is real execution allowed.

### Enum fields

Fixed underlying enum parameters must list the full range of options; do not use open-ended questions.

## Prompt optimization

> The following rules are behavioral guidance for the agent; the script layer (build_prompt_bundle_for_args) is currently in passthrough mode and does not expand automatically.

After receiving a short description from the user, the agent should expand it into a model-friendly prompt. Do not over-engineer descriptions the user already gave in detail.

### When to expand

- Input < 15 characters with no concrete visual description → expand
- Already a detailed scene description → use as-is, at most minor format tweaks
- The user explicitly says "leave it as is" / "don't change it" → strictly as-is

### Expansion dimensions

Starting from the user's core intent, add as needed (not all are required):

1. **Subject**: what it is, what it is doing, count, pose
2. **Environment**: scene, background, lighting, weather, time
3. **Style**: realistic / illustration / 3D / cyberpunk / watercolor, etc.
4. **Composition**: close-up / full body / top-down / side view, etc.
5. **Image quality**: HD, rich detail (as appropriate, do not pile up)

### Language selection

| Model type | Recommended language | Notes |
|---|---|---|
| Flux family (Nano Banana, Klein) | English | English prompts are more stable |
| Chinese models (Jimeng, Qwen) | Chinese | Good Chinese understanding, no translation needed |
| GPT family (ChatGPT Image) | English | Good at complex English instructions |
| Video models | Follow the user's original | Both Chinese and English work for mainstream video models |

For English-model scenarios: the agent writes the user's Chinese into a natural English scene description, not a machine translation.

### Extra dimensions for video

- **Motion**: what action the subject performs, how it moves
- **Camera**: push in / pull out / pan / follow / static
- **Pace**: fast / slow / gradual

### Do not

- Pile up quality words (`masterpiece, best quality, 8k, award winning` in a row) → dilutes the subject
- Contradictory styles (`realistic photo, anime style`) → pick one
- Replace the user's intent (user says "a simple cat", changed to "cyberpunk mechanical cat") → expansion adds detail, it does not change the theme

## Execution and status

### Status enum

| status | Meaning |
|---|---|
| Queuing | In queue |
| Preparing | Preparing |
| Running | Running |
| Success | Success |
| Failed | Failure (see data.message) |

### Output delivery

- Images: forward as-is with `![generated result](url)`
- Video: bare URL on its own line (do not wrap it as a markdown link)
- One task may have multiple outputs (workflows with multiple save nodes)
- Intermediate images (e.g. rgthree.compare's _temp_ thumbnails) are not shown to the user
- Outputs have `expired_at` (~15 days until expiry); download them promptly

### Failure classification

1. Submit stage (auth/parameters/mapping)
2. Platform execution (timeout/service busy/upstream failure)
3. Materials and parameters (format/size/missing items)
4. Result retrieval (download failure/address unreachable)

### Keeping underlying fields quiet

REQUEST_ID / TASK_ID / STATUS / raw JSON are not proactively shown to the user unless the user explicitly wants troubleshooting.

## Resume checks (v1.11)

```bash
python3 scripts/cli.py status <request_id>                        # check once
python3 scripts/cli.py outputs <request_id> [-o output-dir] [--reply-format markdown]
```

- `status` does not poll: suitable when the task has been running for a long time, the session was interrupted, or the user separately asks "is that task done?".
- `outputs` only fetches outputs; with `-o` it downloads to a local directory.
- Both commands are read-only: no cost, no task created; `request_id` comes from the output of `run` or from the user.

## Execution permission

- Plain app → can be executed directly
- Workflow with a linked app → can be executed as-is
- Workflow without a linked app → look only, do not run (must be forked into an Application first)
