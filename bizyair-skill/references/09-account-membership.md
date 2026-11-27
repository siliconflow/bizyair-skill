# 09 · Personal center (my-profile) and membership benefits v1.12.0

The personal center is organized as one fixed hierarchy, instead of splitting "works / models / apps / profile" into unrelated commands:

```
my-profile
  1. published   What I published: works / AI apps / Workflow / models
  2. forked      What I forked: works / AI apps / Workflow / models
  3. generated   What I generated: outputs (published / unpublished)
  4. show / edit / share   Profile query, edit, share
```

The legacy commands `my-works` / `my-favorites` / `my-models` / `profile` are kept for compatibility, but new requests should use `my-profile`.

## Commands

| Command | Purpose | Endpoint |
|---|---|---|
| `my-profile published [--kind ...]` | Works I published + models / apps / workflows | `creations/my` + `userspace/{uid}/bizy_models` |
| `my-profile forked [--kind ...]` | Works I forked + models / apps / workflows | `creations/my_fork` + `bizy_models/my_fork` |
| `my-profile generated [--status ...]` | Outputs I generated (published / unpublished included) | `userspace/{uid}/task/outputs` |
| `my-profile show` | Profile query (nickname / email / level / description / homepage / bindings) | `user/info` |
| `my-profile edit ...` | Profile editing; produces only a confirmation card by default | `PUT userspace/update_user` |
| `my-profile share` | Personal homepage share link | site URL assembled from the user id |
| `profile [show\|edit\|share]` | Compatibility entry point, same behavior | same as above |
| `plan` / `plan-tiers` | Membership level, balance, tier comparison | `user/plan_overview` + `plan/compare_user_level` |
| `notifications` | Notification list + unread count | `notifications` + `notifications/unread_count` |
| `credits` | credits top-up/gift ledger and expiry | `finance/v1/credits` |

Common parameters: `--page` / `--limit` / `--kind` / `--keyword` / `--sort` / `--reply-format markdown|json` / `--api-key`.

## published / forked: grouping rules

`--kind` can be repeated or comma-separated: `creation` (work), `app` (AI app), `workflow`, `model`; default `all`.

| kind | Source | Determination |
|---|---|---|
| `creation` | `/v1/creations/my` or `/my_fork` | works (images / videos / 3D etc.) |
| `app` | `bizy_models` | `type == Application`, and the name contains neither `workflow` nor its Chinese equivalent |
| `workflow` | `bizy_models` | `type == Workflow`, or `Application` whose name contains `workflow` / its Chinese equivalent (workflows are often labeled Application in platform lists) |
| `model` | `bizy_models` | every other model type (Checkpoint / VAE / UNet / LoRA / ControlNet / CLIP / Upscaler / Detection / Other) |

- Each of the two sources has its own `total`; the output header reports "works N · models/apps/workflows M".
- Group counts show the **current page**, not the total for that kind; `--page` pages through, `--limit` controls items per group (default 10).
- Every item carries `page_url`: works `/creation/{id}`, apps `/community/app/{version_id}`, workflows `/community/models/publicity/{id}`, own models `/community/models/my/{id}`.
- **Three app ID levels**: asset `bizy_model.id` (also `resources_used.webapp.id`) ≠ version `bizy_model_version_id` (also `versions[0].id`; the app page uses this one); a Workflow's `ref_bizy_model_id` is another app it references, not its own ID. Use `gallery-app-works` for works by app; see `07-gallery-works.md` for details.
- If one source fails, the other is still returned, with `error=PARTIAL_REMOTE_FAILURE` and exit code 1.
- In JSON, `groups` is grouped into the four kinds and `results` is the flat list; `sources.*.total` is the server-side total.

Examples:

```bash
python3 scripts/cli.py my-profile published
python3 scripts/cli.py my-profile published --kind model --limit 50
python3 scripts/cli.py my-profile forked --kind workflow
python3 scripts/cli.py my-profile published --reply-format json
```

## generated: output list (read-only)

- `my-profile generated` is equivalent to `my-generated`; it lists task outputs and marks them `published` / `unpublished` / `not_publishable`.
- Publishing generated outputs is not available in this skill. `--publish` / `--output-id` / `--confirm-publish` return `GALLERY_PUBLISH_REMOVED` (exit 2) and send no request.
- Read-only examples:

```bash
python3 scripts/cli.py my-profile generated --status unpublished
python3 scripts/cli.py my-profile generated --status published
```

- Determination still uses the output row `outputs[].id`, not `comfy_task_output_id` (see 07); `published` only means a work page already exists.

## Profile query / edit / share

`my-profile show` returns nickname, email, level, language, description, homepage list, third-party bindings, avatar, plus:

```
Personal homepage: https://www.bizyair.ai/userspace/{user_id}
```

`my-profile share` reads only the profile and id, and outputs the same shareable URL (measured 200).

`my-profile edit` supports these fields:

| Parameter | Description |
|---|---|
| `--nickname` / `--name` | nickname, 1–40 characters |
| `--description` | description, ≤2000 characters; pass an empty string to clear it |
| `--avatar` | an http(s) image URL, or a local jpg/png/webp (≤10 MB, uploaded to OSS after confirmation) |
| `--homepage platform=url` | repeatable; platform: `discord` / `github` / `x` / `youtube` / `tiktok` / `other`; must be https and the domain must match the platform |
| `--clear-homepages` | clear the homepage list |
| `--confirm` | actually submit; without it only a preview card is produced (exit code 3) |

Examples:

```bash
python3 scripts/cli.py my-profile edit --nickname "New Nickname" --description "One-line introduction"
python3 scripts/cli.py my-profile edit --homepage github=https://github.com/me --homepage x=https://x.com/me
python3 scripts/cli.py my-profile edit --nickname "New Nickname" --confirm
```

- Request body fields: `nick_name`, `description`, `avatar`, `homepages[{social_icon,social_url}]`; unspecified fields keep their current values and are not accidentally cleared.
- PUT `/v1/userspace/update_user` returns `code=20000` as measured; after submitting, the profile is fetched again as the result.
- **Only these four profile fields are changed**. Email, third-party bindings, API Key, membership, top-up and orders remain outside the skill's scope.

## Membership tiers (measured)

| level | Name | Threshold |
|---|---|---|
| 1 | Basic | none (current) |
| 10 | Black Iron | $50 |
| 20 | Bronze | $200 |
| 30 | Silver | $500 |
| 40 | Gold | $1,000 |
| 50 | Platinum | $2,000 |

- `compare_user_level` amounts are **USD cents** (`min_amount_usd_cents`); divide by 100 when displaying.
- Each tier carries `relation`: `current` / `upgrade_available` (a downgrade is also possible; the code maps it to Chinese).
- `plan_overview` additionally gives `auto_level` (automatic level) and `next_tier.need_recharge_usd_cents` (how much is still missing).

## Wallet and credits

- The wallet balance uses **credits**: 1,000 credits = $1 (`scripts/common.py: CREDITS_PER_USD`).
- `plan_overview.wallet` fields: `total_balance`, `gift_balance`, `next_gift_expire_at`, `credits_expire_days`, `gift_credits_expire_days`.
- `finance/v1/credits` returns `list[]` (each entry with `total_amount` / `gift_amount` / `created_at` / `expired_at`) + `total` / `total_recharge_amount` / `gift_amount`.
- credits expire; when replying to the user, always state the number of days until expiry as well.

## Notifications

- The list returns `notifications[]`: `id`, `type`, `title`, `content`, `sender_user_nick_name`, `related_type`, `related_ids`, `created_at`.
- `unread_count` returns two parallel arrays `types[]` and `counts[]`; the code zips them into `{type,label,count}` and sums them.
- Type mapping (observed so far): `1` system, `2` follow, `3` comment, `4` post, `101` like, `102` favorite; unknown types show the raw number rather than being guessed.
- Liking your own work also creates a notification (measured: 3 likes produced 3 entries with type=101).

## Not possible / not done

- Marking notifications read (`/notifications/read`, `/read_all` endpoints exist, not exposed in this version, to avoid clearing unread by accident)
- Changing email / third-party bindings / managing API Keys (profile editing covers only nickname, description, avatar, homepage)
- Buying membership, topping up, upgrading, invoicing, orders (transactional; never done)
