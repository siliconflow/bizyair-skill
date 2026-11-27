# 02 Account and assets

For API Key management and wallet balance queries.

## API Key

### Check

```bash
python3 scripts/cli.py check
python3 scripts/cli.py check --api-key <KEY>
```

Returns `status`: `ok` / `no_key` / `invalid_key` / `no_balance`

### Configuration priority

1. CLI `--api-key`
2. Environment variable `BIZYAIR_API_KEY`
3. `config.json` → `credentials.api_key` (single key) or `credentials.api_keys[]` (multiple keys)

Multiple keys can be stored, but the current CLI uses only the first non-placeholder key; it does **not** rotate keys automatically. To use another key, pass `--api-key` or change the configured order.

### Balance query

```bash
python3 scripts/cli.py wallet
```

Corresponding endpoint: `GET https://api.bizyair.ai/v1/wallet`

International-site wallet fields are counted in **credits**. The current conversion is **1,000 credits = $1**
(1 credit = $0.001), so `19265` credits is displayed as `$19.265`.

```text
Current available balance $19.265 (19,265 credits; top-up —, gifted 19,265 credits)
```

Returns: `charge_balance_amount` + `gift_balance_amount` + `total_balance_amount`
(all credits values).

BizyAir may currently return only `gift_balance`, with `charge_balance` missing. Missing fields are shown
as "—" in the summary and no longer as `None`. JSON fields fall into two groups:

| Field | Meaning |
|---|---|
| `charge_balance_amount` / `gift_balance_amount` / `total_balance_amount` | credits values, compatibility fields |
| `charge_display` / `gift_display` / `total_display` | credits value or "—", for legacy callers |
| `charge_text` / `gift_text` / `total_text` | directly displayable, e.g. `$19.265 (19,265 credits)` |
| `charge_usd` / `gift_usd` / `total_usd` | USD amount, e.g. `$19.265` |
| `unit` / `credits_per_usd` | `credits` / `1000` |

## Call records (currently unavailable on BizyAir)

The `POST /x/v1/modelzoo/mycalls` in the upstream docs is a path that does not apply to BizyAir. BizyAir's corresponding endpoint
`https://meta.bizyair.ai/v1/modelzoo/mycalls` (GET / POST) **was measured returning 404**, and single-record detail is equally
unavailable. Therefore this edition has **no** call-records subcommand, and will not pretend it can query them.

To check historical consumption, use the task status command per `request_id`:
`python3 scripts/cli.py modelzoo-status <request_id> --full` (it shows the exact charge when `usage` is included).
