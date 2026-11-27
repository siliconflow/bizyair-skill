# 01 Resource search and direct access

For keyword search of AI apps / workflows, detail queries of known objects, fixed menu routing, and the ModelZoo search entry.

> **v1.12.4 default routing / v1.12.5 capability mapping: vague/short generation needs go to ModelZoo first; do not search AI apps first.**
> Background removal/cutout is first normalized to `background_removal`, with the dedicated `background-remover` preferred;
> `pick-image` / `pick-video` are the exception channel, used only when the user explicitly names an app/play/workflow, or gives an app link/ID.
> App search is literal matching, slow and may find nothing; ModelZoo's common image/video/audio capabilities are more direct.

## International-site search capabilities

The community / official list endpoints support keyword search:

```text
GET https://meta.bizyair.ai/v1/bizy_models/community
GET https://meta.bizyair.ai/v1/bizy_models/official
```

Key parameters:

| Parameter | Description |
|---|---|
| `keyword` | Literal keyword matching fields such as name / model name; not semantic search |
| `sort` | **Required**. Commonly `Most Used`, `Recently`, `Most Liked`; the official list also allows `Auto` |
| `model_types` | Repeatable. App search must pass both `Application` and `Workflow` |
| `current` / `page_size` | Pagination |

Notes:

- You cannot write `model_types=Application,Workflow`; the comma is treated as one nonexistent type and returns 0 rows; repeat the parameter instead:
  `model_types=Application&model_types=Workflow`.
- The list endpoints are public; an **invalid** key yields 401, so do not force authentication on list queries. A key is only needed to actually execute tasks.
- Omitting `sort` returns `400 invalid parameter: Sort`.

## Workflow vs AI Application

They are different objects on the platform, and their search entries differ:

| Dimension | AI Application | Workflow |
|---|---|---|
| External form | Input form / ready to use | ComfyUI node graph (`nodes` / `links`) |
| Can it run directly | Yes; `prefill` → confirm → `run` | A pure workflow cannot run directly; a workflow with a linked app runs through the linked app |
| Search entry | `pick-image` / `pick-video --remote` | `search-workflows` |
| What you see | Input fields, required items, price, runtime | Node structure, versions, base model, dependencies, exportable JSON |
| Cost | Running a task is charged per platform rules | Search / view / export are all read-only, no charge |
| Export | Its source workflow can be exported (if a version exists) | Public and available workflows can be exported read-only |

> When the user explicitly wants a "workflow", do not use `pick-image` / `pick-video` to return a pile of AI apps;
> only when the user wants "a play I can fill in parameters and run" should you use AI app search.

## AI app search (exception channel, v1.12.4; even less used after v1.12.5 capability mapping)

```bash
python3 scripts/cli.py pick-image "<keyword>" --remote
python3 scripts/cli.py pick-video "<keyword>" --remote
```

Menu 7 / v7 are exactly these two entries. **Use them only when an app/play is explicitly wanted**; for vague short needs see the ModelZoo search below.
Search flow:

1. The server filters by `Application` + `Workflow`, excluding model assets such as LoRA / VAE / Checkpoint.
2. Chinese colloquial terms are expanded first, for example:
   - retouch → `edit`, `image edit`, `inpaint`, `retouch`
   - lip sync → `lip sync`, `lip-sync`, `audio to video`
   - image to video → `image to video`, `i2v`
   - text to video → `text to video`, `t2v`
   - face swap → `face swap`, `face replacement`
3. Multi-path keyword search, results deduplicated by ID.
4. The client does fuzzy sorting by name hits, `base_model`, and usage; `Application` is slightly preferred over `Workflow`.
5. For the same app, only the Application shell is kept between Application / Workflow shells.

Search is **literal matching**, not "descriptive semantic search". For example:

- Keywords such as `image`, `edit`, `flux`, `qwen` usually hit, as do the Chinese equivalent terms for video, face swap and edit.
- `retouch` and `cyberpunk` searched directly may return 0 rows and need expansion first.
- On 0 rows, first change/split the word or add English synonyms, then consider ModelZoo or curated models; do not jump to the conclusion "BizyAir does not support search".

## Workflow-specific search (v1.8.1+)

```bash
python3 scripts/cli.py search-workflows "qwen image 2.1 anime"
python3 scripts/cli.py search-workflows "qwen image 2.1 anime" --sort "Recently" --limit 20
python3 scripts/cli.py search-workflows "flux" --base-models "FLUX.1" --reply-format markdown
```

Behavior:

- Requests only `model_types=Workflow`; results **contain only Workflow** and never mix in `Application`.
- **Single keyword, single-round retrieval**; the normal path sends one request, with a short retry only on transport failure; no synonym expansion, no multi-round word splitting, so it stays fast and predictable.
- Default `sort=Recently`, default `page_size=50`, `limit=20`.
- Each result returns fields such as `id`, `name`, `version_id`, `version`, `base_model`, `sign`,
  `source_url`, `download_command`, `has_linked_app`.
- `has_linked_app` is metadata only: a value does not mean it can necessarily run directly; still confirm with `info` / `prefill` before executing.
- On 0 results it does not auto-expand; change to a more specific model name / task word, or send a link / ID directly.
- Search itself does not require an API Key; `download-workflow` needs the Key only when querying details.

> Exact-query example: `qwen image 2.1 anime` returns only Workflow `56755` (Qwen Image 2.1 Anime NoctQA V1),
> and does not return the AI Application under the same keyword.

## Direct access to known objects

```bash
python3 scripts/cli.py info <link-or-ID>
python3 scripts/cli.py prefill <link-or-ID> --prompt "..."
python3 scripts/cli.py run <link-or-ID> --prompt "..."
```

`<link-or-ID>` accepts a bare numeric ID or a full page link (e.g. `https://www.bizyair.ai/community/app/57862`).

Internally routed by type:

- **Application** → queries the webapp endpoint (returns `web_app_id` + `input_nodes`)
- **Workflow / Official** → queries the detail endpoint (returns `versions[]`, no `input_nodes`)
- **comfy-ui link** → reports that direct running is not supported and guides to the corresponding AI app link
- Unrecognized input → explicit error and exit code 2

## Workflow export (v1.8.1+)

Read-only export of public Workflow / AI app source workflows; creates no task and costs nothing.

```bash
python3 scripts/cli.py download-workflow <link-or-ID>
python3 scripts/cli.py download-workflow <link-or-ID> -o ./workflows/
python3 scripts/cli.py download-workflow <link-or-ID> --version v1.0
python3 scripts/cli.py download-workflow <link-or-ID> -o ./my-workflow.json --force
```

Behavior:

- Supports community `Workflow`, and also `Application` source workflows that have versions.
- Takes the first available version by default; `--version` accepts a version label (`v1.0`) or a version ID.
- Exports only versions with `public=true` and `available=true`.
- Defaults to `bizyair-outputs/workflows/<time>-<object-ID>-<version>.json`, and writes a same-named `.meta.json`.
- When `-o` points to an existing directory it writes into that directory; when it points to `.json` it is treated as a file; otherwise it is treated as a directory.
- Does not overwrite same-named files by default; it appends `-1`, `-2`; `--force` overwrites.
- The exported content is ComfyUI UI format (`nodes` / `links`), not the API prompt used for execution.

Limitations:

- `comfy-ui?id=...` draft links have no public mapping and return `WORKFLOW_LINK_NOT_SUPPORTED` (exit code 2).
- Model assets (Checkpoint / LoRA / VAE, etc.) are not workflows and return `NOT_A_WORKFLOW`.
- Export is done through the `resolve/BizyModelVersion` parse endpoint and is **not guaranteed to be fully equivalent** to the web page's "download" button; the platform download counter may not increase.
- Workflows may depend on BizyAir custom nodes/models; confirm that nodes and models are complete before importing into a local ComfyUI.
- Detail queries need an API Key; no task is submitted, so `--confirm-run` is not needed.

## Fixed model menus

Images have 7 items (5 curated models + 1 ModelZoo search + 1 AI app search) / video has 7 items with the same structure.
Routes are defined in `config/routes.json`, menu text in `config/menus.json`.

```bash
python3 scripts/cli.py image-menu
python3 scripts/cli.py video-menu
python3 scripts/cli.py image-menu --json   # structured options for automated agents
```

Menus must be output by actually calling the script; the agent must not compose them from memory.

Routing rules:

- 1-5 (images) / v1-v5 (video): fixed models, execute after showing a parameter card
- 6 (images) / v6 (video): ModelZoo search entry (see 04)
- 7 (images) / v7 (video): AI app keyword search; an existing link / ID can also be used for direct access

## When the user gives only a vague description (v1.12.4 ModelZoo-first / v1.12.5 capability mapping)

For example "retouch this for me", "make a video", "change the background of this image":

1. **First decide whether an app is named**: only if "AI app / community app / some app / Workflow / workflow / play" appears, or an app link/ID is given, go to AI app search.
2. **No app named → go to ModelZoo first**: translate the need into model words/task types (e.g. `qwen image edit`, `image-to-image`, `image-to-video`, `lip-sync`, `tts`), and search with `pick-modelzoo-image` / `pick-modelzoo-video`; for audio endpoints use `modelzoo-list --keyword tts`.
3. If an endpoint exists, `modelzoo-detail` + `modelzoo-price` + parameter card; after the user confirms, `modelzoo-run`.
4. Explicitly want an app/play → `pick-image` / `pick-video` for **one round**; explicitly want a workflow → `search-workflows` for one round, without mixing apps.
5. If app search returns 0 or clearly misses the mark, change the word or ask the user for a link; do not scan multiple rounds, and do not invent candidates.
6. Only when ModelZoo truly lacks the capability (multi-step, special nodes, a specific app play) fall back to app search.

## ModelZoo search

ModelZoo is a separate set of endpoints and remains available:

```bash
python3 scripts/cli.py pick-modelzoo-image "<model name / series name / task type>"
python3 scripts/cli.py pick-modelzoo-video "<model name / series name / task type>"
```

For search-word rules, candidate cleanup, prices and parameter details see `04-modelzoo-tasks.md`.
