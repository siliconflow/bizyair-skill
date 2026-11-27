# 08 · Model hub (Model Hub) v1.9

The model hub (bizy_models) and ModelZoo are two different things; tell them apart before choosing a command:

| | Model hub bizy_models | ModelZoo endpoints |
|---|---|---|
| What it is | Downloadable/loadable model assets with versions (LoRA, VAE, UNet, Workflow…) | Per-call inference endpoints (image / video / audio / LLM) |
| Has author and cover | Yes | No (has official categories) |
| Billing | Only tasks that use the asset are billed | Every call deducts credits per the price table |
| Commands | `models` / `model-detail` / `my-profile published --kind model` / `model-types` | `modelzoo-list` / `modelzoo-run` / `pick-modelzoo-*` |

## Commands

| Command | Purpose |
|---|---|
| `models ["keyword"]` | Model hub list / search (community by default; `--source official` shows official models) |
| `model-detail <model ID or link>` | A single model plus all versions |
| `my-profile published --kind model` | **Models** I published (recommended personal-center entry point; use `--kind app\|workflow` for apps/workflows) |
| `my-models` | Legacy entry point, models only; `--include-non-models` lists the others as well |
| `model-types` | Samples which types currently exist, as a reference for `--type` |

Common parameters:

- `--type LoRA` / `--type "LoRA,VAE"`: filter by model type; repeatable or comma-separated
- `--base-model Qwen-Image`: filter by base model; comma-separated is allowed
- `--source community|official`
- `--sort "Most Used"|Recently|Auto` (only these three values are valid; anything else exits locally with code 2)
- `--page` / `--page-size` (≤100) / `--limit` (≤100)
- `--include-non-models`: the legacy `my-models` lists only model assets by default; add this to list AI apps / workflows too (the new entry point uses `my-profile published --kind app|workflow` directly)
- `--reply-format markdown|json`

## Endpoints

| Operation | Method | Path | Auth |
|---|---|---|---|
| Community model list | GET | `meta.bizyair.ai/v1/bizy_models/community` | public (no key) |
| Official model list | GET | `meta.bizyair.ai/v1/bizy_models/community?mode=official` | public |
| Model details | GET | `meta.bizyair.ai/v1/bizy_models/{id}/detail` | API Key |
| Models published by me / by a user | GET | `meta.bizyair.ai/v1/userspace/{user_id}/bizy_models` | API Key |

List parameters: `current`, `page_size`, `sort`, optionally `keyword`, `model_types`, `base_models` (repeated parameters).

## Page link rules (measured)

| Type | URL that actually opens |
|---|---|
| Public model | `https://www.bizyair.ai/community/models/publicity/{id}` |
| Model I published | `https://www.bizyair.ai/community/models/my/{id}` |
| Workflow | `https://www.bizyair.ai/community/models/publicity/{id}` |
| Application | `https://www.bizyair.ai/community/app/{id}` |

- `/community/models/{id}` (without `publicity`) is **404**.
- Using `/community/models/*` for an Application renders the generic "Model Details" page, not the app page.
- Do not add a `/zh` prefix on BizyAir.

## Measured behavior

1. Community models total **3599**, official count **5098** (total with `page_size=1`).
   **Note**: the standalone `/v1/bizy_models/official` endpoint has only 1 entry; official models are actually returned by
   `community` + `mode=official`; `hub.py` implements the latter.
2. `sort` accepts only `Most Used` / `Recently` / `Auto`; `Most Used Today` etc. return `400 invalid parameter: Sort`.
3. `userspace/{user_id}/bizy_models` requires `sort`, otherwise 400; the response structure matches the community list.
4. Model details return `versions[]`; each version has `id`, `version`, `base_model`, `file_size`, `sign`, `cover_urls`, `available`, `description` (possibly the raw HuggingFace README).
5. List items do not carry `cover_url`; take the cover from `versions[0].cover_urls`.
6. Types are free-form strings; `model-types` is only a sample (first 100 items), not a full enumeration.

## Response structure (a single list item)

```json
{
  "id": 57031,
  "name": "PrunaAI/P-Q-Image-2.1",
  "type": "LoRA",
  "owner": "Gugugaga",
  "owner_id": "01kwv421t...",
  "base_models": ["Qwen-Image"],
  "version_count": 4,
  "latest_version": "p_q_image_2.1_5_steps_v0.1.safetensors",
  "file_size": 335606144,
  "cover_url": "https://storage.bizyair.ai/img/...",
  "page_url": "https://www.bizyair.ai/community/models/publicity/57031"
}
```

## What it does not do

- Publish / edit / delete models (needs the web page + review; this skill does not provide it)
- Download model files locally (the model hub is a cloud asset; use the web page or CLI tools when you need local files)

## Community / official filtering (v1.12.3)

`models --source official` uses `/v1/bizy_models/official`. Do not use `mode=official` on the community endpoint: the server ignores that parameter
and mislabels community models as official. There may currently be very few official public objects (measured: keyword=flux has only 1), which is the current state of the platform's data.

