# 04 ModelZoo tasks

For modelzoo list search, detail queries, price queries, task creation and status queries.

## Prerequisites

ModelZoo is the **default entry for vague/short generation needs** (since v1.12.4; capability mapping added in v1.12.5):

1. **Default entry (menu 6 / v6)**: when the user has not named an AI app / workflow / link / ID and only describes short needs such as "retouch, generate an image, make a video, voiceover", first go through `cli.py pick-modelzoo-image` / `pick-modelzoo-video`, translating the need into model words/task types before searching; for audio endpoints use `modelzoo-list --keyword tts`.
2. **Explicit entry**: the user gave a modelzoo link, explicitly said the endpoint name, or picked a specific endpoint from the menu 6 search results.
3. **Exception**: only when a community AI app / a specific play / a workflow is explicitly named, or ModelZoo truly lacks the capability, switch to `pick-image` / `pick-video` / `search-workflows`.

Allowed: creating tasks, querying status, querying prices.
Forbidden: scan-style batch calls, stress testing.

## Capability mapping (v1.12.5)

The ModelZoo list is literal keyword matching, and the user's colloquial wording does not necessarily hit the endpoint name. Check `config/capabilities.json` first:

| Capability | Typical wording | Preferred endpoint | Prompt |
|---|---|---|---|
| `background_removal` | remove background / background removal / cutout / product cutout | `bizyair/image-tools/background-remover` | **Not needed**, pass only `image_urls` |
| `image_edit` | retouch / edit image / change background / image edit / image-to-image | Seedream 5.0 Flash / Qwen Image 2.1 image-to-image | Needed |
| `image_to_video` | image to video / make a photo move / image to video | `pixverse-v6-official/image-to-video` | Needed |
| `watermark_removal` | remove watermark | `bizyair/image-tools/watermark-remover` | Not needed |
| `outpaint` | outpaint / extend canvas / add scenery on the sides | `bizyair/image-tools/outpaint` / `bria-expand-official/image-tools/outpaint` | Optional (recommend giving direction/ratio) |
| `style_transfer` | image style transfer / art style transfer / style conversion / style transfer | generic `image-to-image` / `image edit` (Seedream, Qwen Image Edit) | Needed (recommend giving style constraints) |

- Dedicated endpoints are preferred; `pick-modelzoo-image` returns `match_quality=dedicated` / `prompt_required=false`.
- When there is no dedicated endpoint, fall back to a generic editing endpoint + `suggested_prompt`; **do not automatically switch to an AI app**.
- When a capability hits, the picker returns `fallback_policy=do_not_search_apps`; switch only if the user explicitly wants an app/workflow.
- Only when background removal must keep the shadow / swap in a new background, switch to `image_edit`.

## Paid confirmation and reruns (v1.12.6)

- **One confirmation submits once**: when the user says "start", it authorizes only the current parameters and this one run, not "run until satisfied".
- **Do not rerun automatically on dissatisfaction**: show the output and the problem first; only after the user explicitly says "rerun / run again / change parameters" may you submit again, and reconfirm the cost.
- **Check first when the submit result is unknown**: if `run` did not get a request_id, check `credits` for a charge; `status` and `modelzoo-status` require a task ID, so ask the user for it or use the BizyAir task page. Never rerun directly.
- `WEBAPP_RETRY_LIMIT=0`: automatic submit retries for apps are disabled; when the response is uncertain, a second task will not be created automatically.

## Inquiry-type needs: ModelZoo first, then ask about expanding (v1.12.7)

When the user asks "what services are there / can it do this / what options exist", do not execute directly, and do not automatically search across sources:

1. `pick-modelzoo-image "<capability word>"` queries ModelZoo first.
2. The response has `discovery_mode=true`, `followup_sources=["ai_apps","workflows"]`, and `followup_query` is the short phrase with the question removed (e.g. `image style transfer`).
3. The end of reply_markdown asks "do you want to continue searching AI apps / workflows?"; after the user replies "search apps / search workflows / search both",
   then run `pick-image "<followup_query>" --remote` / `search-workflows "<followup_query>"` separately.
4. **Do not search automatically before the user agrees**; each source gets at most one round, with no consecutive paging.
5. Execution-type requests ("do it for me / start the run / generate an image") do not take this branch and still follow ModelZoo-first directly.

## Standard flow

1. `POST https://meta.bizyair.ai/v1/modelzoo/list` to search for the target capability
2. `GET https://meta.bizyair.ai/v1/modelzoo/detail/:endpoint` to get `input_params` + `outputs_example`
3. `GET https://pricing.bizyair.ai/v1/modelzoo/price_table/:endpoint` to see the price
4. `POST https://api.bizyair.ai/v1/modelzoo/tasks/openapi/:endpoint` to create the task
5. `GET https://api.bizyair.ai/v1/modelzoo/tasks/openapi/:request_id` to check status

> All requests must carry a `User-Agent` (Cloudflare blocks Python's default UA).

## List

```bash
curl -sS -X POST "https://meta.bizyair.ai/v1/modelzoo/list?current=1&page_size=20&sort=Auto" \
  -H "lang: en" -H "Content-Type: application/json" -H "User-Agent: Mozilla/5.0" \
  -d '{"tags":[],"categories":[],"show_deprecated":false}'
```

`sort` is not strictly validated on modelzoo/list (unlike bizy_models/community).

## Detail

```bash
curl -sS "https://meta.bizyair.ai/v1/modelzoo/detail/${ENDPOINT}" \
  -H "lang: en" -H "User-Agent: Mozilla/5.0"
```

> CLI usage: `python3 scripts/cli.py modelzoo-detail <endpoint>`. The output is **complete parseable JSON**;
> it is truncated with a marker only when a single field exceeds 4000 characters, in which case use `--full` for the full text.

Key fields: `display_name` / `input_params` / `outputs_example`

### input_params field type → payload type

| field_type | payload type | Note |
|---|---|---|
| customtext | string | |
| combo | string or number | must be one of field_options.values |
| boolean | bool | |
| number / slider / slides | number | |
| seed | number (-1 = random) | |
| images | **list[str]** | even a single image must be wrapped in an array |
| audios | string URL | |

Note: modelzoo's `field_options` is already a dict (not a JSON string) and needs no extra parsing.

### Parameter validation and defaults (v1.4)

`modelzoo-run` / fixed-model execution / batch preflight all validate against the remote schema before submitting:

| Case | Behavior |
|---|---|
| `--param` missing `=` / empty key | `INVALID_PARAM` + exit code 2 |
| Unknown parameter (typo / not on this endpoint) | `INVALID_PARAM`, and `available_params` is listed |
| combo not in `field_options.values` | `INVALID_PARAM`, and the allowed values are listed |
| number/slider out of range or not matching step | `INVALID_PARAM` |
| boolean is not a true/false kind | `INVALID_PARAM` |
| `image_size` is not `widthxheight` | `INVALID_PARAM` |
| required prompt missing | `INVALID_PARAM`, with no fallback to the vendor example prompt |
| `--image` passed to an endpoint without an images field | `INVALID_PARAM` |

- combo comparison is case-insensitive and returns the canonical form from the schema (e.g. `1k` → `1K`).
- `seed=-1` is always allowed (random seed), even if the schema's min is 1.
- **Defaults come only from `input_params[].field_value` in detail**; `dispatch_common.COMPILER_RULES`
  only keeps the slug list of "which models need a parameter card first", and `defaults` is always empty. Do not restore hard-coded defaults.
- The prefill-card stage allows prompt to be empty for the time being (shows "please enter"); if it is still missing at real submission, it exits with code 2.

## Price

```bash
curl -sS "https://pricing.bizyair.ai/v1/modelzoo/price_table/${ENDPOINT}" \
  -H "lang: en" -H "User-Agent: Mozilla/5.0"
```

Returns `data.price_tables[]` (**plural, with no `simple_price_text` field**; the `simple_price_text` from the upstream docs
does not exist on BizyAir). `pricing_values` are integers in **1/1000 USD**
(`28` = `$0.028`), and the official price page uniformly shows **USD/run**; it is not the credits in the wallet balance.
The CLI outputs USD only, to avoid conflating the "price unit" with the "balance unit". Each table has the structure:

```json
{"columns": [{"variable_name": "resolution", "field_label": "Resolution"}],
 "cells_v2": [["1k"], ["1.5k"], ["2k"]],
 "pricing_values": ["28", "48", "95"],
 "pricing_name": "Output", "unit_name": "run"}
```

The CLI already renders it as a one-line summary:

```bash
python3 scripts/cli.py modelzoo-price qwen-image-2-1-official/text-to-image
# Price: output (resolution): 1k=$0.028/run, 1.5k=$0.048/run, 2k=$0.095/run
```

When `price_tables` is empty it shows the fallback: "no price information is available for this model, charged per actual usage".
Add `--full` to also print the raw JSON.

The `indicative_price` endpoint currently returns 500 and is unavailable; do not call it.

## Create task

```bash
curl -sS -X POST "https://api.bizyair.ai/v1/modelzoo/tasks/openapi/${ENDPOINT}" \
  -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
  -H "lang: en" -H "User-Agent: Mozilla/5.0" \
  -d '{"prompt":"a cat","image_size":"1024x1024"}'
```

- Always async, returns `{"code":20000,"data":{"request_id":"..."}}` in < 5s
- Does not need the `X-Bizyair-Task-Async` header (that one is webapp-only)
- Body field names use the `field_name` from `input_params` directly (no contract mapping needed)

### Media file upload

Pass local file paths via `--image`/`--audio`/`--video`; the script uploads them automatically and **replaces the default example materials entirely**:

```bash
python3 scripts/cli.py modelzoo-run <endpoint> \
  --image /path/to/user-photo.jpg \
  --param prompt="a cat" \
  --confirm-run
```

> ⚠️ **Confirmation gate**: without `--confirm-run`, `modelzoo-run` only prints the parameter card and **does not create a task** (exit code 3).
> This is part of the compute red line: you must get the user's "start / just run / confirm execution" before adding `--confirm-run` and running again.

Rules:
- `--image` maps to `field_type=images` fields and replaces the default value entirely (it is not merged with the example materials)
- `--audio` maps to `field_type=audios` fields
- `--video` maps to `field_type=videos` fields
- If both `--image` and `--param image_urls=...` are passed, `--image` wins
- Multiple images are supported: `--image a.jpg --image b.jpg`
- **Images go through base64 directly by default** (measured in v1.11): local images ≤1.5 MB are converted to `data:image/<mime>;base64,...` and put straight into `image_urls`, with no OSS round trip; larger images fall back to OSS automatically
- `--image-mode auto|base64|oss` forces the mode (`auto` is the default)
- Audio/video always go through OSS upload to get a real HTTP URL
- Prerequisite: you must hit the `/v1/modelzoo/tasks/openapi/` path; non-openapi paths report `value must be an absolute URL`

**Important: do not hand-assemble media parameters**

When the user provides image/audio/video files, you **must** pass them via `--image`/`--audio`/`--video`;
**do not** build `--param image_urls=[...]` yourself. Reasons:
1. The `field_value` in detail is an example material URL; do not copy it into the parameters
2. The script uploads the files and replaces the defaults automatically; no manual handling is needed
3. Hand-assembled parameters easily mix in example materials and cause abnormal results

## Query status

```bash
curl -sS "https://api.bizyair.ai/v1/modelzoo/tasks/openapi/${REQUEST_ID}" \
  -H "Authorization: Bearer ${TOKEN}" -H "lang: en" -H "User-Agent: Mozilla/5.0"
```

Returns `data.status`: Running / Success / Failed

### Four output shapes

```json
{"texts": [...], "images": [...], "videos": [...], "audios": [...]}
```

All four fields coexist; only the one matching the type is a non-empty array.

### Writing outputs to disk (v1.4+)

After a task succeeds the script downloads images/videos/audio automatically and writes text outputs as `.txt`:

- A single task defaults to `bizyair-outputs/<time>-<slug>/` in the current workspace; `--output DIR` specifies a directory,
  or pass a path with an extension as the single-file base name. `BIZYAIR_OUTPUT_DIR` overrides the default output root.
- Runtime / batch status goes to the user state directory (`BIZYAIR_STATE_DIR` overrides); when run directly from inside
  the skill directory, outputs also fall back to `outputs` under the state directory and are never written back into the skill install directory.
- Batch tasks default to `<output_root>/batch_runs/<batch>/task-XX-output/`; batch status / logs are in
  `<runtime>/batch_runs/<batch>/`; neither writes to the skill install directory.
- The directory gets a `run.json` containing `request_id` / `outputs` / `saved_files` /
  `download_errors` / the actually submitted `params` / `manifest_path`.
- Downloads are idempotent GETs: transport layer reset / timeout / 5xx are retried at most 3 times; 4xx fails immediately.
- A download that still fails after retries does not turn a successful task into a failure: the task remains `Success`, the failing
  items are recorded in `download_errors`, and the original URLs remain in `outputs`.

### Failed tasks

- HTTP is still 200 + code=20000
- `data.status = "Failed"`, `data.message` contains the reason
- `data.outputs = {}` (empty dict, not null)
- "Third-party api response error" → upstream fault, retryable
   - Measured observation (2026-11-03, not a conclusion): `minimax-h3-channel/reference-to-video` reported this error twice
     in a row (only the prompt length changed, 1798/1318); switching to `minimax-h3-official/...` (768P 9:16 7s)
     succeeded once. **It may have been a transient/one-sided fault of that channel at the time**; when it happens, prefer switching to `-official` and retrying.
   - Failed tasks **are not charged** (balance unchanged after three failures, while the successful official run was charged $0.70),
     so retrying on another endpoint costs nothing; feel free to switch.

### Multi-image reference submission must explicitly use OSS (measured in v1.13)

- `--image-mode` defaults to `auto`: **images ≤1.5MB are converted to `data:image/png;base64,...`**,
  and only those above the threshold go to OSS. See `api.media_input_value()`.
- **H3-style multi-reference-image (`ref_images`) tasks cannot use the default auto**: base64 inputs make the workflow's
  `LoadImage` node report `av.error.InvalidDataError [Errno 1094995529] Invalid data
  found when processing input`, and the task fails right at the node level.
- Measured comparison (2026-11-03, same endpoint, same set of images, the only variable being mode):

  | Submission | Result |
  |---|---|
  | Default auto (3/4 images via base64) | ❌ `LoadImage: Invalid data found` |
  | `--image-mode oss` (all real URLs) | LoadImage passes, the task continues |
  | official + `--image-mode oss` | ✅ video produced |

- **Operating rule**: whenever `ref_images` gets multiple images (especially mixed sizes), always add `--image-mode oss`
  so that every image becomes a real `https://storage.bizyair.ai/inputs/...` file; do not rely on the automatic threshold.
- Note: this limitation is **not site-wide** — Qwen image-to-image and Gemini Omni work fine with the default base64;
  it is specific to the H3 reference pipeline. Isolation experiments still not done: H3 official + single image + default auto (not run).

### Parameter errors

- Missing required field → 400 `code=20015 "missing required parameter: xxx"`
- Illegal combo value → 400 `code=20015 "invalid parameter, please recheck: parameter xxx invalid"`
- Type error (e.g. number passed as string) → **500 + empty body** (no message)

The client must do type pre-validation, and on a 500 with an empty body prompt "the parameter type may be wrong".

## Call records (currently unavailable on BizyAir)

BizyAir's `https://meta.bizyair.ai/v1/modelzoo/mycalls` (GET / POST) **was measured returning 404**,
and single-record detail is also unavailable, so this edition has no call-records subcommand.

To see the actual consumption of a task, use:
`python3 scripts/cli.py modelzoo-status <request_id> --full` (it shows the exact charge when `usage` is included in the response).
