# 10 · Model publishing (uploading local model files) v1.11

Corresponds to `bizyair upload` in the official [bizyair-cli](https://docs.bizyair.ai/cn/api-guide/bizyair-cli).
The flow replicates the official Go source (`lib/uploader.go`, `lib/filehash/hash.go`, `lib/client.go`);
validated end to end with real files on 2026-10-29 (create model → page 200 → delete the test model);
on 2026-11-03 v1.11.1 switched to OSS multipart upload; see `CHANGELOG.md` for measured results on multipart / resume / large-file publishing.

## Commands

```bash
# produce the parameter card first (exit code 3); add --confirm-publish after confirming
python3 scripts/cli.py model-publish \
  -n my-lora -t LoRA \
  -p /path/my-lora.safetensors \
  -b "FLUX.1 D" \
  -cover /path/cover.jpg \
  -i "Model description (≤5000 chars)"

# actually submit
python3 scripts/cli.py model-publish ... --confirm-publish

# validate + compute the signature + check for a duplicate name only; no upload, no model creation
python3 scripts/cli.py model-publish ... --dry-run
```

Multiple versions: repeat `-p`; `-b` / `-v` / `-cover` / `-i` correspond to them in order; if only one is given, all versions share it.

```bash
python3 scripts/cli.py model-publish -n mymodel -t Checkpoint \
  -p a.safetensors -b SDXL -v v1.0 -cover c1.jpg -i "First version" \
  -p b.safetensors -b "SD 1.5" -v v2.0 -cover c2.jpg -i "Second version" \
  --confirm-publish
```

## Parameters

| Parameter | Required | Description |
|---|---|---|
| `-n, --name` | yes | Model name, ≤60 characters; the same name and type already existing is rejected |
| `-t, --type` | yes | `Checkpoint` `VAE` `UNet` `LoRA` `Controlnet` `CLIP` `Upscaler` `Detection` `Other` |
| `-p, --file` | yes | Local model file; repeatable (multiple versions) |
| `-b, --base-model` | yes | Base model, value taken from `base_models` of `GET /v1/dict` (e.g. `FLUX.1 D`, `SDXL`) |
| `-cover` / `--cover` | no | Cover: a local jpg/jpeg/png/gif/webp/mp4/webm/mov, or an http(s) URL; video ≤100 MB. **If omitted, a placeholder cover is generated automatically** |
| `-i, --intro` / `--intro-path` | no | Version description (≤5000 characters); `--intro-path` accepts .txt/.md |
| `-v, --version` | no | Version name, default `v1.0` |
| `--public` / `--private` | no | Whether the version is public; private by default |
| `--dry-run` | no | Validate + compute the signature + check for a duplicate name; no write operations |
| `--upload-concurrency` | no | Part concurrency, 1–8, default 6 |
| `--restart-upload` | no | Discard the local checkpoint and upload again from part 1 |
| `--confirm-publish` | yes (when submitting) | Second-confirmation gate; exit code 3 when missing |
| `--reply-format markdown\|json` | no | Default markdown |

File formats: `.safetensors` `.ckpt` `.pt` `.pth` `.bin` `.sft`; multipart upload with a soft limit of 200 GB (the platform's actual limit is whatever the server returns).

## Automatic cover (when no cover is prepared)

When `-cover` is not passed, the command uses `scripts/cover.py` to generate a 1024×1024 placeholder cover:

- Pure Python: ships its own 5×7 bitmap font, no PIL / cwebp / system font dependency
- The background color is decided by a hash of "model name + type" (identical for the same name and type on every run); it draws the type (large), the model name (ASCII) and the base model (small)
- After generation it goes through the same upload flow as a local cover (token → OSS → commit), and the server converts it to WebP
- The parameter card shows `"cover": "(auto-generated placeholder cover)"`; to use a real cover, add `-cover <image path>`
- When the model name is Chinese, the placeholder draws only the type and base model (the bitmap font is ASCII only); publishing is unaffected

## Local files only

`-p` accepts local paths only. For external site links (`civitai.com` / `huggingface.co` / `modelscope.cn`)
use `model-repost` (wraps the official `bizyair-community` CLI, see `references/11-model-repost.md`);
passing a URL to `-p` exits with code 2 and suggests switching to `model-repost`.

## Endpoints and flow

| Step | Method | Path | Description |
|---|---|---|---|
| 1 | GET | `meta.bizyair.ai/v1/files/{sign}?type=<Type>` | Fetch OSS STS credentials; `data.file.id > 0` means the file is already registered and the upload can be skipped |
| 2 | POST/PUT | `https://{bucket}.{endpoint}/{object_key}?uploads` etc. | OSS multipart upload (STS + OSS v1 signature): initiate → concurrent UploadPart → Complete; the checkpoint resumes |
| 3 | POST | `meta.bizyair.ai/v1/files` | Register: `{sign, object_key, md5_hash, type}` |
| 4 | POST | `api.bizyair.ai/v1/upload/token?file_name=&file_type=inputs` → OSS → `meta.bizyair.ai/v1/input_resource/commit` | Cover upload, yielding a URL |
| 5 | POST | `meta.bizyair.ai/v1/bizy_models` | `{name, type, versions:[{version, base_model, description, public, sign, path, cover_urls}]}` |
| 6 | GET | `meta.bizyair.ai/v1/bizy_models/exists?name=&type=` | Duplicate-name check: already exists 200 + `data=null`; does not exist 404 + code `20224` |

For `path`, just pass the local absolute path (the official CLI also passes a local path; the server files it by `sign`).

## Multipart upload and resume (v1.11.1)

A single `PutObject` is capped at 5 GB on the OSS side, so large models switch to multipart:

- Adaptive part size: 16 MB by default; larger files scale up within the 9000-part ceiling (max 512 MB), keeping under OSS's 10000-part limit.
- 6 concurrent parts by default; a failed part is retried alone, never the whole file.
- Progress: the signature stage first, then upload percentage / bytes transferred / speed; refreshed in place on a terminal.
- Checkpoint file: `<state>/model_uploads/<sign>.json`, recording the uploadId, part ETags, size, mtime, object_key;
  rerunning the same command after an interruption skips the completed parts. The checkpoint also stores the non-expired STS temporary credentials (file mode 0600);
  after they expire it invalidates them automatically and uploads from zero. `--restart-upload` forces a restart.
- STS expiry: a 403 received mid-upload triggers re-acquiring the upload credentials for the same `sign` and continuing (the uploadId does not change).
- The previous session is gone on the server (`NoSuchUpload`): the checkpoint is discarded automatically, the upload is retried once and the user is told.

Note: signing must first read the whole file once (md5 + crc64); the signing stage of a large file takes time and cannot be skipped.

## sign algorithm (critical)

It is **not** the file's sha256:

```python
md5_b64 = base64(md5(file))
crc64   = Go hash/crc64 ECMA, both the initial value and the result are bitwise-inverted
sign    = sha256(md5_b64 + str(crc64))
```

`api.model_file_signature()` implements exactly this (streaming read, no memory blow-up).
A plain sha256 yields `40021 file signature error`, and creating the model then reports `20031 file not found`.

## Return

```json
{
  "ok": true,
  "model": {"id": 57050, "name": "skill-demo-lora-2", "type": "LoRA"},
  "version_ids": [58217],
  "page_url": "https://www.bizyair.ai/community/models/my/57050",
  "versions": [{"version": "v1.0", "base_model": "FLUX.1 D", "sign": "...", "file_size_text": "4.00 KB"}],
  "reply_markdown": "..."
}
```

- Review: after publishing, the model enters platform review; until it passes it is visible only on the "My models" page and does not enter the community list
- Cost: uploading model files does not deduct credits; only generation tasks that use the model are billed
- Verified in `my-models`: the new model appears in my model list (type LoRA etc.)

## Error reference

| Symptom | Cause | Handling |
|---|---|---|
| Exit code 3 | `--confirm-publish` not passed | Add it after confirming the parameter card |
| `file signature error` (40021) | wrong sign algorithm / md5 does not match the file | Use `api.model_file_signature()` |
| `file not found` (20031) | creating the model before the file was registered | `POST /v1/files` first, then create the model |
| `resource name already exists` | a model with the same name and type already exists | Change `--name`, or add a version on the web page |
| `Please provide at least one cover image` | the server received no cover | This command already generates a placeholder cover; if it still appears, check whether the API was called by hand |
| `invalid parameter: Versions[0].Path` | the version is missing `path` | Pass the local file path |

## Safety boundaries

- Only creates new models; never edits/deletes existing models; renaming, changing versions or changing public status is not supported after publishing
- Runs only when the user explicitly asks to publish; private by default, public only with `--public`
- Does not upload files unrelated to the model on the user's behalf, and does not touch account profile/payment
