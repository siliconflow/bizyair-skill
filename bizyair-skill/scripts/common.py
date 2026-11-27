"""Shared constants + small helpers (no business logic).

This file holds:
  - BizyAir HTTP API endpoint constants (API_BASE / CREATE_URL / WEBAPP_DETAIL_URL ...)
  - Network/timeout constants (DEFAULT_TIMEOUT / POLL_INTERVAL / MAX_POLL_SECONDS)
  - Field-detection rule sets (PROMPT_EXCLUDE_* / KNOWN_PROMPT_* / TEXT_INPUT_FIELD_TYPES)
  - Shared text normalization and match-scoring helpers
    (normalized_text / classify_route_match / count_term_matches ...)

Nearly every module depends on this file (api / contract / remote / search ...).
Change it carefully: the blast radius is large.
"""
from __future__ import annotations
import argparse, json, os, re, sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from paths import config_display_path as preferred_config_display_path
from paths import load_config_json as _load_raw_config
from typing import Any


def configure_stdio() -> None:
    """Flush stdout per line so merged stdout/stderr capture keeps its order.

    P2-3: polling STATUS lines go to stderr while the submission text goes to stdout;
    under pipes stdout is block-buffered by default, which makes STATUS look like it
    comes before "submitted". Force line buffering on both streams here.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass


configure_stdio()

# ---------------------------------------------------------------------------
# Site config: this skill only serves the international site bizyair.ai.
#
# The API is split across the api / meta / pricing hosts; paths carry no /w /x /y prefix.
# Other modules only import the constants below and never assemble URLs themselves.
# ---------------------------------------------------------------------------
SITE = 'ai'

API_BASE = 'https://api.bizyair.ai'

CREATE_URL = 'https://api.bizyair.ai/v1/webapp/task/openapi/create'

# webapp task lookup uses a "path segment" style: request_id is appended right after
# /openapi/, with no /detail suffix and no ?requestId= query parameter.
DETAIL_URL = 'https://api.bizyair.ai/v1/webapp/task/openapi/{request_id}'

OUTPUTS_URL = 'https://api.bizyair.ai/v1/webapp/task/openapi/{request_id}/outputs'

UPLOAD_TOKEN_URL = 'https://api.bizyair.ai/v1/upload/token'

INPUT_COMMIT_URL = 'https://meta.bizyair.ai/v1/input_resource/commit'

WALLET_URL = 'https://api.bizyair.ai/v1/wallet'

# The international-site wallet balance is shown in credits; 1,000 credits = $1 (1 credit = $0.001).
# ModelZoo price tables store raw pricing_values in 1/1000 USD, displayed as USD per run, not credits.
# If the platform changes the ratio, change it only here; wallet and price display share it.
CREDITS_PER_USD = Decimal('1000')

WEBAPP_DETAIL_URL_TEMPLATE = 'https://meta.bizyair.ai/v1/webapp/{webapp_id}/detail'

BIZY_MODEL_DETAIL_URL = 'https://meta.bizyair.ai/v1/bizy_models'

COMMUNITY_URL = 'https://meta.bizyair.ai/v1/bizy_models/community'

OFFICIAL_URL = 'https://meta.bizyair.ai/v1/bizy_models/official'

RESOLVE_VERSION_URL = 'https://meta.bizyair.ai/v1/resolve/BizyModelVersion'

MODELZOO_LIST_URL = 'https://meta.bizyair.ai/v1/modelzoo/list'

MODELZOO_LIST_METHOD = 'POST'

MODELZOO_DETAIL_URL = 'https://meta.bizyair.ai/v1/modelzoo/detail/{endpoint}'

MODELZOO_PRICE_URL = 'https://pricing.bizyair.ai/v1/modelzoo/price_table/{endpoint}'

MODELZOO_TASK_URL = 'https://api.bizyair.ai/v1/modelzoo/tasks/openapi/{key}'


# The international-site community/official list endpoints support keyword search.
# Note: list endpoints require sort; app search must filter by model_types=Application/Workflow.
CATALOG_SEARCH_SUPPORTED = True

def client_config() -> dict[str, Any]:
    """config.json -> client (optional): lang / api_host / meta_host / pricing_host."""
    try:
        config = _load_raw_config()
    except Exception:
        config = {}
    client = config.get('client') if isinstance(config, dict) else None
    return client if isinstance(client, dict) else {}


def request_lang() -> str:
    """Request header lang: defaults to en (international site); override via config.client.lang or BIZYAIR_LANG."""
    env = os.environ.get('BIZYAIR_LANG', '').strip()
    if env:
        return env
    return str(client_config().get('lang') or 'en').strip() or 'en'


_HOST_ENV = {
    'api': ('BIZYAIR_API_HOST', 'api_host'),
    'meta': ('BIZYAIR_META_HOST', 'meta_host'),
    'pricing': ('BIZYAIR_PRICING_HOST', 'pricing_host'),
    'finance': ('BIZYAIR_FINANCE_HOST', 'finance_host'),
}


def host_overrides() -> dict[str, str]:
    """Host overrides: env wins over config.client. <origin> -> new origin."""
    overrides: dict[str, str] = {}
    for key, (env_name, cfg_name) in _HOST_ENV.items():
        value = os.environ.get(env_name, '').strip() or str(client_config().get(cfg_name) or '').strip()
        if value:
            overrides[key] = value.rstrip('/')
    return overrides


def apply_host_override(url: str) -> str:
    """Replace the api/meta/pricing/finance hosts with overrides (for staging / self-hosted proxies)."""
    overrides = host_overrides()
    if not overrides:
        return url
    mapping = {
        'https://api.bizyair.ai': overrides.get('api'),
        'https://meta.bizyair.ai': overrides.get('meta'),
        'https://pricing.bizyair.ai': overrides.get('pricing'),
        'https://finance.bizyair.ai': overrides.get('finance'),
    }
    for origin, replacement in mapping.items():
        if replacement and url.startswith(origin):
            return replacement + url[len(origin):]
    return url


SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_TIMEOUT = 60

POLL_INTERVAL = 5

MAX_POLL_SECONDS = 900

# Paid submissions are never retried automatically: a lost response may already have
# created a task, and a retry would charge twice. Re-running requires the user to confirm again.
WEBAPP_RETRY_LIMIT = 0

SEARCH_MIN_ROUNDS = 3

SEARCH_MAX_ROUNDS = 5

BIZYAIR_INFO_HOSTS = {'bizyair.ai', 'www.bizyair.ai'}

COMMUNITY_APP_URL = 'https://www.bizyair.ai/community/app/{id}'

# On-site a workflow is a model asset: /community/workflows/{id} returns 404 in testing,
# while /community/models/publicity/{id} is the one that actually opens.
COMMUNITY_WORKFLOW_URL = 'https://www.bizyair.ai/community/models/publicity/{id}'

WORKFLOW_URL_ALLOWED_HOSTS = {'bizyair-prod.oss-cn-shanghai.aliyuncs.com', 'bizyair.ai', 'www.bizyair.ai', 'meta.bizyair.ai', 'storage.bizyair.ai'}

# ---------------------------------------------------------------------------
# Model publishing (v1.10): the official bizyair-cli public flow, verified end to end.
#   GET  /v1/files/{sign}?type=<ModelType>  -> OSS STS credentials (returns the id when it already exists)
#   POST /v1/files                          -> register the file (sign + object_key + md5_hash + type)
#   POST /v1/bizy_models                    -> create the model (name + type + versions[])
# ---------------------------------------------------------------------------
MODEL_CREATE_URL = 'https://meta.bizyair.ai/v1/bizy_models'

MODEL_EXISTS_URL = 'https://meta.bizyair.ai/v1/bizy_models/exists'

MODEL_FILE_SIGN_URL = 'https://meta.bizyair.ai/v1/files/{sign}'

MODEL_FILE_COMMIT_URL = 'https://meta.bizyair.ai/v1/files'

BASE_MODEL_DICT_URL = 'https://meta.bizyair.ai/v1/dict'

# Your own model page after a successful publish (/community/models/my/{id} opens in testing).
MY_MODEL_PAGE_URL = 'https://www.bizyair.ai/community/models/my/{id}'

SITE_LABEL = 'bizyair.ai'

PROMPT_EXCLUDE_EN = {'title', 'caption', 'subtitle', 'watermark', 'label', 'logo'}  # used only for info/prefill display labeling

PROMPT_EXCLUDE_ZH = {'\u6807\u9898', '\u526f\u6807\u9898', '\u5b57\u5e55', '\u6c34\u5370', '\u6807\u7b7e', '\u6807\u8bed', '\u89d2\u6807', '\u5fbd\u6807'}  # internal input alias; never displayed

EXPLICIT_PROMPT_LABELS = {'prompt', 'text', '\u63d0\u793a\u8bcd', '\u6587\u672c', '\u6b63\u5411\u63d0\u793a\u8bcd', '\u6587\u672c\u63d0\u793a\u8bcd'}  # internal input alias; never displayed

TEXT_INPUT_FIELD_TYPES = {'customtext', 'textarea', 'text', 'string'}

KNOWN_PROMPT_NODE_MARKERS = {'cliptextencode', 'primitivestringmultiline', 'primitivestring'}  # used only for info/prefill display labeling

KNOWN_PROMPT_FIELD_NAMES = {'prompt', 'user_prompt', 'positive_prompt'}  # used only for info/prefill display labeling

REMOTE_EXPOSED_CONTRACT_SOURCES = {'execution_target.webapp_detail.input_nodes', 'webapp_detail.input_nodes'}

REMOTE_GENERIC_PROMPT_ALIASES = {'prompt', 'text', 'value', 'positiveprompt', 'positive_prompt', 'userprompt', 'user_prompt'}

REMOTE_CONTRACT_SOURCE_PRIORITY = ['execution_target.webapp_detail.input_nodes', 'webapp_detail.input_nodes', 'workflow_hints']

def normalized_text(text: Any) -> str:
    return (str(text) if text is not None else '').strip().lower()


# internal input alias; never displayed
DISCOVERY_MARKERS = (
    '\u6709\u54ea\u4e9b', '\u6709\u4ec0\u4e48', '\u54ea\u4e9b', '\u53ef\u4ee5\u7528', '\u80fd\u7528', '\u53ef\u7528', '\u63a8\u8350', '\u65b9\u6848', '\u670d\u52a1',
    '\u5de5\u5177', '\u652f\u6301\u54ea\u4e9b', '\u80fd\u505a\u5230', '\u600e\u4e48\u505a', '\u5982\u4f55\u505a', '\u80fd\u4e0d\u80fd', '\u662f\u5426\u53ef\u4ee5',
    '\u6709\u6ca1\u6709', '\u5bf9\u6bd4', '\u9009\u578b', '\u4e86\u89e3\u4e00\u4e0b', '\u627e\u627e', '\u641c\u4e00\u4e0b',
)
# internal input alias; never displayed
HARD_EXECUTION_MARKERS = (
    '\u5f00\u8dd1', '\u76f4\u63a5\u8dd1', '\u786e\u8ba4\u6267\u884c', '\u63d0\u4ea4\u4efb\u52a1', '\u5f00\u59cb\u751f\u6210', '\u751f\u6210\u4e00\u5f20', '\u51fa\u4e00\u5f20',
    '\u6765\u4e00\u5f20', '\u5e2e\u6211\u751f\u6210', '\u5e2e\u6211\u4fee', '\u5e2e\u6211\u505a', '\u5f00\u59cb\u505a', '\u8dd1\u4e00\u4e0b',
)


def is_discovery_query(prompt: Any) -> bool:
    """Whether the user is asking "what capabilities/services exist" rather than requesting immediate execution."""
    q = normalized_text(prompt)
    if not q:
        return False
    if any(marker in q for marker in HARD_EXECUTION_MARKERS):
        return False
    return any(marker in q for marker in DISCOVERY_MARKERS)

def config_display_path() -> str:
    return preferred_config_display_path()

def local_script_command(script_name: str, *args: str) -> str:
    script_path = (SCRIPT_DIR / script_name).resolve()
    parts = ['python3', str(script_path)]
    parts.extend((str(arg) for arg in args))
    return ' '.join(parts)

def safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _decimal_amount(value: Any) -> Decimal | None:
    """Convert a balance / price from the API into Decimal; return None when missing or non-numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value).replace(',', '').strip())
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def credits_to_usd(value: Any) -> Decimal | None:
    amount = _decimal_amount(value)
    return None if amount is None else amount / CREDITS_PER_USD


def format_credits(value: Any) -> str | None:
    """Return credits with thousands separators, e.g. 19265 -> '19,265 credits'."""
    amount = _decimal_amount(value)
    return None if amount is None else f'{amount:,} credits'


def format_usd(value: Any) -> str | None:
    """Convert credits to USD with a fixed 3 decimals, e.g. 19265 -> '$19.265'."""
    amount = credits_to_usd(value)
    if amount is None:
        return None
    rounded = amount.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
    return f'${rounded:,}'


def format_credits_and_usd(value: Any) -> str | None:
    """Show USD and credits together, e.g. '$19.265 (19,265 credits)'."""
    credits_text = format_credits(value)
    usd_text = format_usd(value)
    if credits_text is None:
        return None
    return f'{usd_text} ({credits_text})' if usd_text else credits_text

def add_unique_key(items: list[str], value: str | None) -> None:
    text = str(value or '').strip()
    if text and text not in items:
        items.append(text)


def object_page_url(object_type: str | None, object_id: Any, *, version_id: Any=None) -> str | None:
    """Return a page the international site can open, keyed by object type (verified in v1.9.1).

    Application -> /community/app/{version_id} (the AI app page keys on the webapp version id, not the bizy_model id)
    Workflow    -> /community/models/publicity/{bizy_model_id}
    Other models -> /community/models/publicity/{bizy_model_id}

    Verified: /community/app/{bizy_model_id} returns 200 but is an empty shell (generic title);
    only /community/app/{version_id} carries the AI app name, so applications must pass version_id.
    """
    if object_id is None or str(object_id).strip() == '':
        return None
    kind = str(object_type or '').strip()
    if kind == 'Application':
        page_id = version_id if version_id not in (None, '') else object_id
        return COMMUNITY_APP_URL.format(id=page_id)
    return COMMUNITY_WORKFLOW_URL.format(id=object_id)


WORKFLOW_LINK_NOT_SUPPORTED_MESSAGE = 'That is a ComfyUI workflow link (comfy-ui?id=xxx), and such links cannot be run directly yet.\n\nIf you want to run this workflow, find its matching AI app version in the BizyAir community and send me the AI app link instead.\n\nAn AI app link looks like this: ' + COMMUNITY_APP_URL.format(id='xxxxx')


def build_prompt_bundle_for_args(args: argparse.Namespace, *, route_name: str | None=None, modality: str | None=None, task: str | None=None, input_profile: dict[str, Any] | None=None) -> dict[str, Any]:
    cached = getattr(args, '_compiled_prompt_bundle', None)
    signature = (str(getattr(args, 'prompt', None) or ''), route_name, modality, task, json.dumps(input_profile or {}, ensure_ascii=False, sort_keys=True), len(getattr(args, 'image', None) or []), len(getattr(args, 'audio', None) or []))
    if isinstance(cached, dict) and cached.get('_signature') == signature:
        return cached
    raw = re.sub(r'\s+', ' ', str(getattr(args, 'prompt', None) or '').strip())
    bundle = {'raw_prompt': raw, 'card_display_prompt': raw, 'execution_prompt': raw, 'polished_prompt': raw, 'structured_prompt': raw, 'enriched_prompt': raw, 'rewritten_prompt': '', 'negative_prompt': '', 'changed': False, 'polish_notes': [], 'mode': None, 'mode_source': 'passthrough', 'style_profile': {'name': None, 'source': 'none', 'en_hint': None}, 'rewrite_applied': False}
    bundle['_signature'] = signature
    setattr(args, '_compiled_prompt_bundle', bundle)
    return bundle
