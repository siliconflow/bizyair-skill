# 07 · Works gallery (Gallery / creations) v1.8.2 / v1.10.1 (search by model) / v1.12 (personal center aggregation)

The works gallery shows the **final generated results** users publish (images / videos), not AI apps or workflows.
This skill covers gallery browsing, search, likes and favorites; publishing generated works is not available. Everything goes through `meta.bizyair.ai`
and authenticates with the same API Key (`Authorization: Bearer sk-…`), with no browser session required.

## Command overview

| Command | Purpose | Nature |
|---|---|---|
| `gallery` | Gallery list (most recent by default) | read |
| `gallery-search "<keyword>"` | Search works by name/description keyword | read |
| `gallery-app-works <AI app link or ID>` | **Look up works by AI app** (server queries directly by version ID; no per-item detail scan) | read (fast) |
| `gallery-search --model "<model name or numeric ID>"` | Scan details and match on the work's Related Resources (names must match exactly) | read (slow on the first run) |
| `gallery-detail <ID or link>` | Work details | read |
| `my-profile published --kind creation` | Works I published (recommended entry point, aggregated under the personal center) | read |
| `my-profile forked --kind creation` | Works I forked (recommended entry point) | read |
| `my-works` / `my-favorites` | Legacy entry points, each equivalent to one of the two above | read |
| `gallery-like <ID or link>` | Like | write (additive, cannot be undone) |
| `gallery-favorite <ID or link>` | Favorite | write (reversible) |
| `gallery-unfavorite <ID or link>` | Remove favorite | write (reversible) |

Common parameters: `--sort Recently|Recommend`, `--page`, `--page-size` (at most 50 for lists),
`--limit` (default 20), `--keyword`, `--reply-format markdown|json`, `--api-key`.
Search by model adds: `--model` (= `--generated-by`), `--max-scan` (2 pages by default, max 20), `--refresh` (ignore the cache).
`gallery-app-works` adds: `--page` / `--limit` / `--sort` (default Recommend).

## My generated outputs (v1.10.3; read-only)

The web page "Personal center → My generated" corresponds to the endpoint: `GET /v1/userspace/{user_id}/task/outputs`.
In the personal center prefer `my-profile generated`; it is equivalent to `my-generated`.
It is different data from "My published": this list contains both published and unpublished outputs.

```bash
python3 scripts/cli.py my-profile generated              # WEBAPP + API by default, all statuses
python3 scripts/cli.py my-profile generated --status unpublished --limit 50
python3 scripts/cli.py my-profile generated --status published
python3 scripts/cli.py my-profile generated --type PLAYGROUND    # only ModelZoo outputs
```

| Parameter | Description |
|---|---|
| `--status` | `all` (default) / `published` / `unpublished` |
| `--type` | `WEBAPP,API` by default; `PLAYGROUND` (ModelZoo, no work page) can be added |
| `--page` / `--page-size` | Which page to start from and how many per page (default 1 / 20) |
| `--limit` | Maximum number of items to return (default 20, cap 100) |

Each item is returned as:

```json
{"output_id": 11327, "comfy_output_id": 8291, "task_id": 6733,
 "task_name": "Krea2 Text to Image", "kind": "image",
 "url": "https://storage.bizyair.ai/outputs/....png",
 "created_at": "2026-10-30T03:54:24Z", "expired_at": "2026-11-14T00:00:00Z",
 "expired": false, "status": "unpublished", "work_id": null, "work_url": null}
```

### How published/unpublished is determined

In work details, `objects[].comfy_task_output_id` actually stores the **output row id** (`outputs[].id`),
therefore:

- Fetch `/v1/creations/my` to build the map `outputs[].id → work id`
- Output present in the map → `published` + `work_url`; otherwise → `unpublished`

Do **not** match against `outputs[].comfy_task_output_id` (that is a comfy-internal id, it does not line up, and it makes published works look unpublished).

### Notes

- Unpublished outputs are valid for about 15 days; when `expired: true`, the image/video URL may already be dead;
- `PLAYGROUND` (ModelZoo) outputs have no work object and no work page, so they are not listed by default;
  with an explicit `--type PLAYGROUND` they are marked `status: "not_publishable"`;
- Deleting a published work moves it back to `unpublished` (the map is computed live, there is no local cache).
- Publishing generated outputs is not available: `gallery-publish` and `my-profile generated --publish` return `GALLERY_PUBLISH_REMOVED` (exit 2) and send no request. This list stays read-only.

## Works by AI app (v1.12.1)

No more fetching details one by one. The server has an endpoint for works by model version:

```http
GET /v1/creations/bizy_model_version/{bizy_model_version_id}?current=1&page_size=100&sort=Recommend
```

Returns `{current,page_size,total,list[]}`; `list[]` has the same shape as gallery works and can be rendered directly.

```bash
python3 scripts/cli.py gallery-app-works 57827
python3 scripts/cli.py gallery-app-works https://www.bizyair.ai/community/app/57827 --limit 50
python3 scripts/cli.py gallery-app-works 56692          # AI app asset ID, resolved to the version ID first
```

- Input can be a `/community/app/{version_id}` link, a version ID, or an AI app asset ID; the command normalizes it to `bizy_model_version_id`.
- A version ID is the fastest path; an AI app asset ID first needs `versions[0].id` looked up from the community/official list, which is slower but only queries the list once.
- Measured: version `57827` returns 26 works; AI app asset `56692` resolves to `57827`.
- Private/unpublished versions return 403 `Resource version is not public.`; that is not "0 works", report it honestly as a permission error.

### The three ID levels (measured)

| Field | Meaning | Used for |
|---|---|---|
| `bizy_models.id` (the `webapp.id` in a resource) | AI app / model **asset ID** | model hub, `my-profile`, asset lists |
| `bizy_models.versions[].id` (`resources_used.webapp.bizy_model_version_id`) | AI app / model **version ID** | AI app page `/community/app/{version_id}`, works by app, running an app |
| `resources_used.webapp.ref_bizy_model_id` | another app **referenced** by a Workflow | only used to find "the app this workflow references"; it is **not** the object's own ID |

Real `resources_used` sample (v1.12.1 keeps these three fields):

```json
{
  "webapp": {
    "id": 56692,
    "bizy_model_version_id": 57827,
    "ref_bizy_model_id": 56693,
    "name": "image_qwen_image_2_1_t2i",
    "type": "Application",
    "base_model": "Qwen-Image"
  },
  "models": [
    {"id": 56676, "bizy_model_version_id": 57811,
     "name": "qwen_image_2.1_int8_convrot", "type": "UNet"}
  ]
}
```

- Works by app uses **`bizy_model_version_id`** (57827), not `id` (56692).
- The earlier false "0 works" report came from passing `id` to the `bizy_model_version` endpoint; for an asset ID that endpoint may return an empty page, which looks like "no works".
- `ref_bizy_model_id` only appears on Workflow resources; do not treat it as the app's own ID.

### AI app version ID colliding with a model hub asset ID (v1.12.2)

The same numeric ID can exist in two namespaces at once. Measured with `56747`:

- `/v1/webapp/56747/detail` → the app "Replace White Background - QWen Image Edit 2511" (AI app version ID)
- `/v1/bizy_models/56747/detail` → `minimax_h3_flf2v_lora_v1.safetensors` (LoRA asset ID)
- Both return 200; resolving "model detail first" would turn the app into a LoRA.

Now `info` / `prefill` / `run` **prefer the webapp app version** for numeric IDs; to look up the model hub object use
`python3 scripts/cli.py model-detail 56747`. An app version ID can be `run` / `prefill`ed directly; the page is
`/community/app/56747`.

`pick-image` fields now come from the same source:

| Field | Meaning |
|---|---|
| `id` / `page_id` / `run_target_id` | App: version ID (same source as `page_url`, can be run/prefilled directly); Workflow/model: page object ID |
| `bizy_model_id` / `asset_id` | Model hub asset ID (for apps e.g. 55649; used only for the model hub, not for run) |
| `bizy_model_version_id` / `version_id` | Version ID (for apps e.g. 56747) |

## Scanning details by model name/ID (gallery-search --model)

`gallery-app-works` solves "works by app", so prefer it. Only scan details when you must find works by **model name** across apps:

1. Page through the list (`POST /v1/creations/community`)
2. Concurrently fetch `GET /v1/creations/{id}/detail` and read `resources_used`
3. Match locally against the `id` / `bizy_model_version_id` / `ref_bizy_model_id` / names of webapp and models

**Matching rules (tightened in v1.12.1)**:

- A numeric query: exact match on `id`, `bizy_model_version_id`, `ref_bizy_model_id`, `ref_bizy_model_version_id`;
- A name query: after normalization the values must be **exactly equal** to count as a hit (case/space/hyphen insensitive);
- No more substring matching, and `base_model` / `type` are no longer match targets.

So `--model "Qwen Image 2.1"` **will not** match `Qwen Image 2.1 Turbo ...` any more; to find works by app, use
`gallery-app-works` directly or pass the numeric version ID. A complete name such as `--model image_qwen_image_2_1_t2i` still matches.

**Cache**: detail `resources_used` and work fields are cached together for 7 days, path priority

1. `$BIZYAIR_CACHE_DIR/creation_resources.json`
2. `<current working directory>/.bizyair-cache/creation_resources.json`
3. `~/Library/Application Support/bizyair/creation_resources_cache.json` (may not be writable in a sandbox)

Old caches hold only `resources_used`; on a cache hit the work fields are filled in from the list item, so `(untitled)` never appears again.
`--refresh` forces a refetch. The first detail scan is still limited to 1.5–3 s per item; 2 pages by default, stopping on a hit.

**Returned**: each matching work gains these fields on top of the existing ones

| Field | Meaning |
|---|---|
| `matched_resource` | the name of the matched resource |
| `resource_labels` | all resource labels of that work |

**Cost and limits**: `gallery-search --model` still fetches details item by item; with thousands of works the first full scan is slow, and caching helps.
For works by app use `gallery-app-works`; do not scan details.

## Not covered yet (later versions)

- Searching across apps by model name still requires scanning details; for a known app use `gallery-app-works`.

- Unliking (no usable endpoint)
- "Hottest / trending" sort (enum not exposed)
- Deleting your own works, batch-changing public status (`DELETE /v1/creations/{id}`,
  `PUT /v1/creations/batch_update_public` endpoints exist, not exposed in this version)
- Publishing generated outputs to the gallery (`gallery-publish` / `--publish` are retired and return `GALLERY_PUBLISH_REMOVED`)

## Like / favorite (v1.12.3 semantic reminder)

The platform's "favorite" is really **forking a copy**: `gallery-favorite` creates a fork copy, `gallery-unfavorite` deletes that copy,
and the original work is unaffected. The command's success message says this explicitly; do not describe it as a plain bookmark; your own works cannot be forked, and the API returns a Chinese-language message.
Likes are additive and there is currently no un-like endpoint, so call them only when the user explicitly asks.

