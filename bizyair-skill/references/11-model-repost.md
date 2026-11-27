# 11 · Reposting models from external sites (model-repost, v1.10.2)

Move models from Civitai / HuggingFace / ModelScope to BizyAir. **This is done by the official `bizyair-community` CLI**;
this skill only normalizes parameters, applies the confirmation gate, invokes it and reshapes the output — signature, OSS, metadata and cover generation are all maintained by the official tool.

Division of labor with `model-publish`:

| Input | Command | Who does the work |
|---|---|---|
| Local `.safetensors` / `.ckpt` etc. files | `model-publish` | this skill (the official CLI does not accept local paths) |
| `civitai.com` / `huggingface.co` / `modelscope.cn` links | `model-repost` | official `bizyair-community repost` |

## Installing the official CLI

```bash
curl -fsSL https://storage.bizyair.ai/cli-community/install.sh | sh
bizyair-community --version    # currently latest v0.0.11
```

- macOS / Linux only; matches amd64/arm64 automatically and verifies SHA256.
- Already installed: `bizyair-community upgrade [--check]`.
- Command lookup order: `PATH` → `~/bizyair-community` → `~/.local/bin` → `~/bin` → `/usr/local/bin`.
- When it is not installed, `model-repost` prints the install command and exits with code 1 (it never runs a remote script via curl automatically).

## Commands

```bash
# show the parameter card first (no execution)
python3 scripts/cli.py model-repost --source-url https://huggingface.co/owner/repo

# actually repost after confirmation
python3 scripts/cli.py model-repost --source-url https://huggingface.co/owner/repo --confirm-publish

# common: rename / specify type and base model / generate covers when the source site has none
python3 scripts/cli.py model-repost \
  --source-url https://huggingface.co/owner/repo \
  --name "My Model" --type LoRA --base-model "FLUX.1 D" \
  --generate-covers --confirm-publish
```

| Parameter | Description |
|---|---|
| `--source-url` | required; an external site link. Local paths are rejected (exit code 2, suggests `model-publish`) |
| `--source` | `civitai` / `huggingface` / `modelscope`; inferred from the host when omitted |
| `-n` / `--name` | override the model name (official `--model-name`) |
| `-t` / `--type` | override the type (platform enum; `Controlnet`, not `ControlNet`) |
| `-b` / `--base-model` | override the base model; must be a platform dictionary value |
| `--public` | publish publicly (private by default; the official default is already private) |
| `--generate-covers` | generate covers with text-to-image when the source site has no images (**deducts credits**) |
| `--cover-model` | specify the cover model, comma-separated; must be used together with `--generate-covers` |
| `--allow-nsfw-covers` | keep Mature covers from the source site (by default they are dropped when a PG cover exists) |
| `--max-description-length` | description truncation length, default 5000 |
| `--work-dir` | job cache directory for the official CLI, default `~/.bizyair-community/jobs` |
| `--source-api-key` | key for a private Civitai / HF source (HF can also use `HF_TOKEN`) |
| `--concurrency` / `--fail-fast` | passed through to the official CLI |
| `--confirm-publish` | second confirmation; without it only the parameter card is printed and the exit code is 3 |
| `--reply-format` / `--api-key` | same as other commands |

## Official CLI behavior (measured / confirmed from docs)

- **URL shapes**
  - Civitai: `https://civitai.com/models/{id}/{slug}[?modelVersionId={vid}]`
  - HF repo: `https://huggingface.co/{owner}/{repo}` → top-level weights, one version per file
  - HF tree: `.../tree/{rev}[/{subdirectory}]`
  - HF single file: `.../blob|resolve/{rev}/{file}`
- **Idempotent**: the same name and type already exists → `already published, skipping`; if the model was deleted → it is republished automatically.
- **Covers**: scans the source site's top level + `examples/` `images/` `assets/` (png/jpg/jpeg/webp, at most 5 images);
  if there is not a single one you must pass `--generate-covers`, otherwise it reports `no cover images: use --generate-covers`.
- **Description**: Civitai HTML→Markdown; HF uses the README; external images/videos are uploaded to OSS and the references rewritten; over-length text is truncated and gets a source-link footer.
- **Type/base model inference** (HF): `--type` → platform dictionary → keyword table → fallback `Other`; if the base model cannot be inferred it aborts immediately.

## This skill's output

Success (official output `published model id=... version_ids=...`):

```json
{
  "status": "published",
  "model": {"name": "...", "id": 61234, "version_ids": [70001, 70002],
            "version_count": 2, "page_url": "https://www.bizyair.ai/community/models/my/61234"},
  "skipped": false,
  "log_tail": ["..."]
}
```

Skipped: `status: "skipped"` + `skipped: true`.
Failure: `status: "failed"` + `message` + `hints` + `log_tail` (last 20 lines), exit code 1.

**Note**: the official CLI's `--json` has no effect in practice (it still prints plain text), so milestones are matched from text;
if the official output format changes, `parse_output()` must be updated in step.

## Common errors

| Official output | Our hint |
|---|---|
| `cannot infer base model ... use --base-model` | add `--base-model` (a platform dictionary value) |
| `no cover images: use --generate-covers` | add `--generate-covers` (deducts credits) |
| `invalid --type ... must be one of ...` | use the enum spelling `Controlnet` |
| `--cover-model requires --generate-covers` | add both |
| `flag needs an argument: --publish-visible` | we only ever pass `--publish-visible` as a boolean, so it cannot occur; only relevant when calling the official CLI by hand |
| `HTTP 401: "Api key is invalid"` | check `config.json` / `--api-key`; publish permission is required |
| `already published, skipping` | delete the old model first, or change `--name` |
| `unsupported ... host ""` | not an external site URL; use `model-publish` for local files |

## Safety boundaries

- Reposting **downloads the source file and uploads it**, which is an account write operation: `--confirm-publish` is mandatory.
- Private by default; add `--public` only when it should be public.
- `--generate-covers` really calls a text-to-image model and deducts credits; the parameter card states this clearly.
- Never runs `curl | sh` automatically; when the binary is missing it only prints the install command and the user installs it themselves.
