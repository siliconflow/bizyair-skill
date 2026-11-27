"""modelzoo.py — BizyAir ModelZoo task module.

Covers: list / detail / price / create task / poll status / call records.
Differences from webapp (AI app):
  - create is always asynchronous (returns request_id in < 5s), no X-Bizyair-Task-Async header needed
  - field_name is the payload key directly (no contract mapping needed)
  - field_options is already a dict (no JSON.parse needed)
  - field_type=images must be passed a list[str]
  - outputs keeps four field kinds side by side: {texts, images, videos, audios}
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from common import (
    MODELZOO_DETAIL_URL, MODELZOO_LIST_URL, MODELZOO_PRICE_URL, MODELZOO_TASK_URL,
    POLL_INTERVAL, MAX_POLL_SECONDS, is_discovery_query,
)

import api
import capabilities


class ParamError(ValueError):
    """Parameter parsing / validation failure. Callers turn it into a structured error + exit code 2."""

    def __init__(self, issues: list[dict[str, Any]], *, available_params: list[str] | None = None):
        self.issues = issues or []
        self.available_params = available_params or []
        first = self.issues[0].get("message") if self.issues else "invalid parameters"
        super().__init__(str(first))


def _coerce_param(field_type: str, value: Any) -> Any:
    """Coerce the parameter type according to field_type, to avoid a server-side 500."""
    if value is None:
        return value
    ft = (field_type or "").lower()
    if ft in ("number", "slider", "slides"):
        try:
            return float(value) if "." in str(value) else int(value)
        except (ValueError, TypeError):
            return value
    if ft == "seed":
        try:
            return int(value)
        except (ValueError, TypeError):
            return -1
    if ft == "boolean":
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("true", "1", "yes")
    if ft == "images":
        if isinstance(value, list):
            return value
        return [str(value)]  # wrap a single value into a list too
    return value


# Parameter name alias map. field_name naming is not consistent across ModelZoo endpoints,
# so this defines field_name → possible alias list. When user_params has no exact match,
# build_task_payload tries to read the value through the aliases.
_FIELD_ALIASES: dict[str, list[str]] = {
    'ratio': ['aspect_ratio'],           # happyhorse/wan use ratio
    'aspect_ratio': ['ratio'],           # and the reverse
    'size': ['resolution', 'image_size'], # seedream uses size
    'resolution': ['size', 'image_size'], # and the reverse
    'image_size': ['resolution', 'size'], # some endpoints
}


def _resolve_user_param(field_name: str, user_params: dict[str, Any]) -> tuple[Any, bool]:
    """Read the value from user_params, allowing alias fallback. Returns (value, found)."""
    if field_name in user_params:
        return user_params[field_name], True
    for alias in _FIELD_ALIASES.get(field_name, []):
        if alias in user_params:
            return user_params[alias], True
    return None, False


def parse_param_pairs(raw_params: list[str] | None) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Parse the --param key=value list.

    Returns (params, issues). A missing `=` / an empty key counts as an input error; callers turn it into exit code 2.
    Values may contain `=` (split only on the first `=`).
    """
    params: dict[str, str] = {}
    issues: list[dict[str, Any]] = []
    for raw in raw_params or []:
        text = str(raw)
        if "=" not in text:
            issues.append({
                "param": text,
                "error": "MISSING_EQUALS",
                "message": f"--param expects key=value, got {text!r}",
            })
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        if not key:
            issues.append({
                "param": text,
                "error": "EMPTY_PARAM_NAME",
                "message": f"the key of --param must not be empty, got {text!r}",
            })
            continue
        params[key] = value
    return params, issues


def _schema_available(detail_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(param.get("field_name")): param
        for param in (detail_data.get("input_params") or [])
        if param.get("field_name")
    }


def _resolve_schema_field(key: str, schema: dict[str, dict[str, Any]]) -> tuple[str | None, list[str]]:
    """Map the key the user passed to a schema field_name, with _FIELD_ALIASES support."""
    if key in schema:
        return key, [key]
    candidates = [
        field_name
        for field_name in schema
        if key in (_FIELD_ALIASES.get(field_name) or [])
    ]
    if len(candidates) == 1:
        return candidates[0], candidates
    return None, candidates


def _validate_param_value(
    param: dict[str, Any],
    value: Any,
) -> tuple[Any, dict[str, Any] | None]:
    field_name = str(param.get("field_name") or "")
    field_label = str(param.get("field_label") or field_name)
    ft = str(param.get("field_type") or "").lower()
    options = param.get("field_options") if isinstance(param.get("field_options"), dict) else {}

    def issue(code: str, message: str, **extra: Any) -> dict[str, Any]:
        return {
            "param": field_name,
            "label": field_label,
            "error": code,
            "message": message,
            **extra,
        }

    if ft == "customtext":
        text = "" if value is None else str(value)
        if not text.strip() and param.get("required"):
            return value, issue("EMPTY_VALUE", f"{field_label} must not be empty")
        return text, None

    if ft == "combo":
        allowed = [str(item) for item in (options.get("values") or [])]
        if not allowed:
            return value, None
        normalized = str(value).strip()
        for item in allowed:
            if normalized.lower() == item.lower():
                return item, None
        return value, issue(
            "INVALID_OPTION",
            f"{field_label} does not support {value!r}; allowed values: {' / '.join(allowed)}",
            allowed=allowed,
        )

    if ft in ("number", "slider", "slides"):
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            return value, issue("INVALID_NUMBER", f"{field_label} needs a number, got {value!r}")
        if number.is_integer():
            number = int(number)
        min_value = options.get("min")
        max_value = options.get("max")
        step = options.get("step")
        if isinstance(min_value, (int, float)) and number < min_value:
            return value, issue("OUT_OF_RANGE", f"{field_label} must not be less than {min_value}, got {number}")
        if isinstance(max_value, (int, float)) and number > max_value:
            return value, issue("OUT_OF_RANGE", f"{field_label} must not be greater than {max_value}, got {number}")
        if isinstance(step, (int, float)) and step:
            base = min_value if isinstance(min_value, (int, float)) else 0
            quotient = (number - base) / step
            if abs(quotient - round(quotient)) > 1e-9:
                return value, issue("INVALID_STEP", f"{field_label} must increase in steps of {step}, got {number}")
        return number, None

    if ft == "seed":
        try:
            seed = int(str(value).strip())
        except (TypeError, ValueError):
            return value, issue("INVALID_SEED", f"{field_label} needs an integer, got {value!r}")
        # -1 is the ModelZoo convention for a random seed; it must pass even when field_options min is 1.
        if seed != -1:
            min_value = options.get("min")
            max_value = options.get("max")
            if isinstance(min_value, (int, float)) and seed < min_value:
                return value, issue("OUT_OF_RANGE", f"{field_label} must not be less than {min_value}, got {seed}")
            if isinstance(max_value, (int, float)) and seed > max_value:
                return value, issue("OUT_OF_RANGE", f"{field_label} must not be greater than {max_value}, got {seed}")
        return seed, None

    if ft == "boolean":
        if isinstance(value, bool):
            return value, None
        low = str(value).strip().lower()
        if low in {"true", "1", "yes", "on"}:
            return True, None
        if low in {"false", "0", "no", "off"}:
            return False, None
        return value, issue("INVALID_BOOLEAN", f"{field_label} needs true/false, got {value!r}")

    if ft == "image_size":
        text = str(value).strip()
        normalized = text.lower().replace("*", "x")
        parts = normalized.split("x")
        if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
            return value, issue("INVALID_IMAGE_SIZE", f"{field_label} needs width x height (for example 1280x720), got {value!r}")
        width, height = (int(part.strip()) for part in parts)
        if width <= 0 or height <= 0:
            return value, issue("INVALID_IMAGE_SIZE", f"{field_label} width and height must be greater than 0, got {value!r}")
        return f"{width}x{height}", None

    if ft in ("images", "audios", "videos"):
        if isinstance(value, list):
            return value, None
        return [str(value)], None

    return value, None


def validate_user_params(
    detail_data: dict[str, Any],
    user_params: dict[str, Any],
    *,
    require_all: bool = True,
    supplied_media_types: set[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate --param against the remote schema and normalize combo/number/boolean values.

    - unknown parameters, invalid combo values, out of range, wrong type → issues (callers use exit code 2)
    - combo is case-insensitive and returns the canonical spelling from the schema
    - a required customtext (prompt etc.) must be supplied explicitly by the user; never fall back to the vendor example prompt
    - require_all=False is used for the prefill card: the prompt may still be empty, but parameters already filled in wrongly still error
    """
    schema = _schema_available(detail_data)
    if not schema:
        return dict(user_params or {}), [{
            "param": "",
            "error": "MODELZOO_DETAIL_EMPTY",
            "message": "The remote side returned no input_params, so parameters cannot be validated",
        }]

    provided = dict(user_params or {})
    supplied_media = set(supplied_media_types or set())

    # Old cards / the generic CLI may pass width+height; if the schema only knows image_size, merge them into WxH.
    if (
        "width" in provided
        and "height" in provided
        and "width" not in schema
        and "height" not in schema
        and "image_size" in schema
    ):
        provided["image_size"] = f"{provided.pop('width')}x{provided.pop('height')}"

    normalized: dict[str, Any] = {}
    issues: list[dict[str, Any]] = []
    available = sorted(schema)

    schema_media_types = {
        str(param.get("field_type") or "").lower()
        for param in schema.values()
        if str(param.get("field_type") or "").lower() in {"images", "audios", "videos"}
    }
    for media_type in sorted(supplied_media):
        if media_type not in schema_media_types:
            issues.append({
                "param": media_type,
                "error": "UNSUPPORTED_MEDIA_TYPE",
                "message": f"This endpoint has no {media_type} input field; available media fields: {', '.join(sorted(schema_media_types)) or 'none'}",
            })

    for key, value in provided.items():
        field_name, candidates = _resolve_schema_field(str(key), schema)
        if not field_name:
            if len(candidates) > 1:
                message = f"Parameter {key!r} is ambiguous; it may map to: {' / '.join(candidates)}; please pass the exact field_name"
            else:
                message = f"Unknown parameter {key!r}; this endpoint supports: {', '.join(available)}"
            issues.append({
                "param": str(key),
                "error": "UNKNOWN_PARAM",
                "message": message,
                "available_params": available,
            })
            continue
        validated, param_issue = _validate_param_value(schema[field_name], value)
        if param_issue:
            issues.append(param_issue)
            continue
        normalized[field_name] = validated

    if require_all:
        for field_name, param in schema.items():
            if not param.get("required"):
                continue
            field_type = str(param.get("field_type") or "").lower()
            if field_name in normalized:
                continue
            # Media fields can be supplied via --image / --audio / --video, not through --param.
            if field_type in supplied_media:
                continue
            default_value = param.get("field_value")
            # Required text fields must be given explicitly by the user; the remote field_value is the vendor example prompt.
            if field_type == "customtext" or default_value is None:
                field_label = str(param.get("field_label") or field_name)
                issues.append({
                    "param": field_name,
                    "label": field_label,
                    "error": "MISSING_REQUIRED",
                    "message": f"Missing required parameter {field_label} (field_name={field_name})",
                })

    return normalized, issues


def _strip_default_media(value: Any, default_urls: set[str]) -> Any:
    """Strip default example URLs that got mixed into the user's media value.

    Scenario: the Agent sees field_value ["demo.jpg"] in the detail, the user also provides their own image,
    and the Agent may wrongly assemble ["user.jpg", "demo.jpg"].
    This function removes the URLs that belong to the defaults and keeps only the user's own.
    If the result is empty (meaning everything the user passed was a default), return the original value unchanged.
    """
    if isinstance(value, list):
        filtered = [u for u in value if str(u) not in default_urls]
        return filtered if filtered else value
    if isinstance(value, str) and value in default_urls:
        return value  # single value and it is the default → do not strip (otherwise the field would be empty)
    return value


def list_endpoints(
    api_key: str,
    *,
    keyword: str = "",
    tags: list[str] | None = None,
    categories: list[str] | None = None,
    sort: str = "Auto",
    page: int = 1,
    page_size: int = 20,
    show_deprecated: bool = False,
) -> dict[str, Any]:
    """Search the modelzoo endpoint list."""

    url = f"{MODELZOO_LIST_URL}?current={page}&page_size={page_size}"
    if keyword:
        # Must be URL encoded, otherwise a non-ASCII keyword is rejected by the server (urllib.error or 0 results).
        url += f"&keyword={urllib.parse.quote(keyword)}"
    if sort:
        url += f"&sort={sort}"
    body = {
        "tags": tags or [],
        "categories": categories or [],
        "show_deprecated": show_deprecated,
    }
    return api.safe_request_json("POST", url, api_key, payload=body)


def get_detail(api_key: str, endpoint: str) -> dict[str, Any]:
    """Fetch endpoint detail (input_params + outputs_example)."""

    return api.safe_request_json("GET", MODELZOO_DETAIL_URL.format(endpoint=endpoint), api_key)


def get_price(api_key: str, endpoint: str) -> dict[str, Any]:
    """Fetch the endpoint price table. Prefer showing simple_price_text."""

    return api.safe_request_json("GET", MODELZOO_PRICE_URL.format(endpoint=endpoint), api_key)


def build_task_payload(
    detail_data: dict[str, Any],
    user_params: dict[str, Any],
    *,
    media_overrides: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Build the create payload from the detail's input_params + user parameters.

    Rules (highest priority first):
    1. media_overrides: matched by field_type (images/audios/videos),
       replaces the default value as a whole, never merged with it. Used for files uploaded via --image/--audio/--video.
    2. user_params: parameters the user passed explicitly via --param key=value
    3. the input_params field_value (default): fallback default value
    - type coercion is done per field_type

    media_overrides format: {"images": ["url1", ...], "audios": ["url1"], ...}
    """
    input_params = detail_data.get("input_params") or []
    payload: dict[str, Any] = {}
    overrides = media_overrides or {}
    consumed_media_types: set[str] = set()

    # Collect the default URLs of all media fields, for fallback filtering
    _default_media_urls: set[str] = set()
    for param in input_params:
        ft = (param.get("field_type") or "").lower()
        if ft in ("images", "audios", "videos"):
            dv = param.get("field_value")
            if isinstance(dv, list):
                _default_media_urls.update(str(u) for u in dv)
            elif isinstance(dv, str) and dv:
                _default_media_urls.add(dv)

    for param in input_params:
        field_name = param.get("field_name")
        field_type = param.get("field_type") or ""
        default_value = param.get("field_value")
        ft = field_type.lower()

        if not field_name:
            continue

        # media_overrides has the highest priority: match field_type and replace the default value as a whole
        if ft in overrides and ft not in consumed_media_types:
            payload[field_name] = _coerce_param(field_type, overrides[ft])
            consumed_media_types.add(ft)
            continue

        # Parameters the user passed explicitly via --param (with alias fallback)
        user_value, user_found = _resolve_user_param(field_name, user_params)
        raw_value = user_value if user_found else default_value

        # Fallback: if the user explicitly passed a media value, strip any default example URLs mixed in
        if ft in ("images", "audios", "videos") and user_found and _default_media_urls:
            raw_value = _strip_default_media(raw_value, _default_media_urls)

        # Skip None values that are not required
        if raw_value is None and not param.get("required"):
            continue

        payload[field_name] = _coerce_param(field_type, raw_value)

    return payload


def create_task(api_key: str, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Create a modelzoo task. Always asynchronous, request_id returned in < 5s."""

    return api.safe_request_json(
        "POST",
        MODELZOO_TASK_URL.format(key=endpoint),
        api_key,
        payload=payload,
    )


def query_task(api_key: str, request_id: str) -> dict[str, Any]:
    """Query modelzoo task status."""

    return api.safe_request_json(
        "GET",
        MODELZOO_TASK_URL.format(key=request_id),
        api_key,
    )


def poll_until_done(api_key: str, request_id: str) -> dict[str, Any]:
    """Poll until the task finishes or fails. Returns the final data."""

    start = time.time()
    last_status = None

    while time.time() - start < MAX_POLL_SECONDS:
        result = query_task(api_key, request_id)
        data = (result.get("data") or {}).get("data") or result.get("data") or {}
        status = data.get("status")

        if status != last_status:
            print(f"STATUS:{status}", file=sys.stderr)
            last_status = status

        if status == "Success":
            return data
        if status == "Failed":
            message = data.get("message") or "Unknown error"
            print(
                json.dumps(
                    {"error": "MODELZOO_TASK_FAILED", "status": status, "message": message},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return data

        time.sleep(POLL_INTERVAL)

    print(
        json.dumps({"error": "TIMEOUT", "message": "Polling timed out", "request_id": request_id}, ensure_ascii=False),
        file=sys.stderr,
    )
    return {"status": "Timeout", "request_id": request_id}


def _output_extension(url: str) -> str | None:
    suffix = Path(urllib.parse.urlparse(str(url)).path).suffix.lower()
    if suffix and len(suffix) <= 8:
        return suffix
    return None


def flatten_outputs(outputs: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Flatten ModelZoo's {texts,images,videos,audios} into download items."""
    flattened: list[dict[str, Any]] = []
    for media_type in ("images", "videos", "audios"):
        for url in ((outputs or {}).get(media_type) or []):
            flattened.append({
                "media_type": media_type,
                "object_url": str(url),
                "output_ext": _output_extension(str(url)),
            })
    return flattened


def save_task_outputs(
    outputs: dict[str, Any] | None,
    output_path: str | None = None,
    *,
    slug: str | None = None,
    endpoint: str | None = None,
    request_id: str | None = None,
    prompt: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write task outputs to disk and write run.json.

    output_path semantics:
      - with a suffix (e.g. out.png): treated as the base name of a single file; multiple outputs get -01/-02 suffixes
      - without a suffix: treated as a directory, files are named result.png / result-01.png ...
      - omitted: goes to paths.resolve_output_root()/<time>-<slug>/
    """
    from paths import resolve_output_root

    raw_output = str(output_path).strip() if output_path else ""
    if raw_output:
        candidate = Path(raw_output).expanduser().resolve()
        if candidate.suffix:
            run_dir = candidate.parent
            download_base = str(candidate)
        else:
            run_dir = candidate
            download_base = str(candidate)
    else:
        run_dir = (resolve_output_root() / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug or 'modelzoo'}").resolve()
        download_base = str(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    saved_files: list[str] = []
    download_errors: list[dict[str, Any]] = []

    media_items = flatten_outputs(outputs)
    for index, item in enumerate(media_items, start=1):
        try:
            target_path = api.resolve_indexed_output_path(
                download_base,
                index=index,
                total=len(media_items),
                preferred_ext=item.get("output_ext"),
            )
            saved_files.append(api.download_file(item["object_url"], target_path, preferred_ext=item.get("output_ext")))
        except Exception as exc:
            download_errors.append({
                "index": index,
                "media_type": item.get("media_type"),
                "url": item.get("object_url"),
                "error": str(exc),
            })

    for index, text in enumerate(((outputs or {}).get("texts") or []), start=1):
        text_path = run_dir / f"text-{index:02d}.txt"
        try:
            text_path.write_text(str(text), encoding="utf-8")
            saved_files.append(str(text_path.resolve()))
        except Exception as exc:
            download_errors.append({
                "index": index,
                "media_type": "texts",
                "error": str(exc),
            })

    manifest: dict[str, Any] = {
        "slug": slug,
        "endpoint": endpoint,
        "request_id": request_id,
        "status": "Success",
        "saved_at": int(time.time()),
        "output_path": str(run_dir),
        "outputs": outputs or {},
        "saved_files": saved_files,
        "download_errors": download_errors,
        "prompt": prompt,
        "params": params or {},
    }
    manifest_path = run_dir / "run.json"
    manifest["manifest_path"] = str(manifest_path.resolve())
    try:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        download_errors.append({"media_type": "manifest", "error": str(exc)})

    return manifest


# ---- ModelZoo endpoint candidate search (menu option 6 / v6 entry) ----

# Modality inference: read the category field the server returns directly; authoritative + simple.
# The old version scanned keywords across display_name + endpoint + description in one blob, treated brand names (wan/hunyuan)
# as video signals, and wrongly killed the image edition of Wan. The category field values are "Text to Image" / "Text to Video" /
# "FLF to Video" / "Image to Image" / "Vision" etc.; containing the video/image substring is enough to split them.


def infer_modality_hint(item: dict[str, Any]) -> tuple[str, str]:
    """Decide the modality from the ModelZoo server category field. Returns (hint, reason).

    hint: 'image' / 'video' / 'unknown' (Vision / LLM / TTS and other non image/video types fall to unknown)
    reason: a short explanation for the LLM / debug output
    """
    category = item.get("category") or ""
    cat_low = category.lower()
    if "video" in cat_low:
        return "video", f"category={category}"
    if "image" in cat_low:
        return "image", f"category={category}"
    return "unknown", f"category={category or '(empty)'}"


def summarize_modelzoo_candidate(item: dict[str, Any]) -> dict[str, Any]:
    """Pick the few fields shown to the user, so the whole raw item is not passed through.

    cover_image priority:
      1. outputs_example.images[0] / videos[0] (a real example image from the detail API; the picker must inject it into the raw item's _cover_image_url field beforehand)
      2. icon_url (already available at the list stage; it is the model logo, not a real example)
      3. None
    The cover_image_source field tells the caller whether this is a real example or a logo.
    """
    endpoint = item.get("endpoint")
    hint, reason = infer_modality_hint(item)

    cover_url = item.get("_cover_image_url")
    cover_source = item.get("_cover_image_source")
    if not cover_url:
        icon = item.get("icon_url")
        if icon:
            cover_url = icon
            cover_source = "icon_url"
        else:
            cover_source = "none"

    return {
        "endpoint": endpoint,
        "display_name": item.get("display_name"),
        "category": item.get("category"),
        "description": item.get("description") or item.get("introduction"),
        "cover_image": cover_url,
        "cover_image_source": cover_source,
        "tags": item.get("tags") or [],
        "modality_hint": hint,
        "modality_reason": reason,
    }


def _modelzoo_number_badge(index: int) -> str:
    return f"{index}."


def _modality_label(hint: str) -> str:
    """Label appended to the title when rendering reply_markdown. unknown gets no label."""
    if hint == "image":
        return " [image]"
    if hint == "video":
        return " [video]"
    return ""


def discovery_followup_query(query: str, capability: dict[str, Any] | None = None) -> str:
    """Short query for the follow-up pick-image/search-workflows, so a whole question is not used as a keyword."""
    q = str(query or '').strip()
    if not capability:
        return q
    aliases = [str(item).strip() for item in (capability.get('aliases') or []) if str(item).strip()]
    normalized_q = q.lower()
    matched = [alias for alias in aliases if alias.lower() in normalized_q]
    if matched:
        return max(matched, key=len)
    return str(capability.get('label') or q).strip()


def _discovery_followup_markdown(query: str) -> str:
    """When a question-style request only searched ModelZoo, give the user a cross-source next step."""
    return (
        "\n\n---\n\n"
        "🔎 **You are asking \"which services are available\"**: the results above only cover **ModelZoo**.\n\n"
        "**AI apps** and **Workflows** may have the matching capability too. Want me to keep looking?\n\n"
        "- Reply \"check apps\" → I search community AI apps\n"
        "- Reply \"check workflows\" → I search Workflows\n"
        "- Reply \"check both\" → I search each of them\n\n"
        "⚠️ **The following instructions are for the agent / LLM; do not forward them verbatim to the user:**\n\n"
        f"- Only ModelZoo may be searched this round; do not widen the search automatically. After the user explicitly agrees, run `pick-image \"{query}\" --remote` / `search-workflows \"{query}\"` separately.\n"
        "- Do not page through or run multiple rounds just to \"find everything\"; at most one round per source."
    )


def build_modelzoo_reply_markdown(candidates: list[dict[str, Any]], *, modality: str, query: str = "", capability: dict[str, Any] | None = None, discovery_mode: bool = False, discovery_query: str | None = None) -> str:
    """Build the reply_markdown shown to the user.

    v1.12.5: when a capability is recognized, mark dedicated/general matches explicitly and tell the agent not to switch to AI apps automatically.
    v1.12.7: a question-style request only searches ModelZoo, and appends "want me to check AI apps / Workflows".
    """
    cap_label = str((capability or {}).get('label') or '').strip()
    if not candidates:
        hint = ''
        if capability:
            hint = (
                f'\n\n⚠️ **The following instructions are for the agent / LLM; do not forward them verbatim to the user:**\n\n'
                f'- This round was recognized as "{cap_label}", but ModelZoo returned no matching endpoint.\n'
                '- **Do not switch to AI app search automatically**; retry once with a different capability word, or use a general image edit endpoint + the suggested prompt.\n'
                '- Only switch to `pick-image` / `pick-video` when the user explicitly asks for apps/workflows.'
            )
        text = (
            "📭 **ModelZoo found no suitable endpoint this round**\n\n"
            f"No model matched the current keyword \"{query}\".{hint}"
        )
        if discovery_mode:
            text += _discovery_followup_markdown(discovery_query or query)
        return text
    target = (modality or "image").lower()
    target_label = "video" if target == "video" else "image"
    heading = f"🎯 **Endpoints found in ModelZoo (sorted for {target_label} intent)**"
    if cap_label:
        heading += f" | recognized as: {cap_label}"
    intro = "ModelZoo calls the underlying model API directly: explicit parameters and stable per-run billing. Candidates below (the tag is a modality hint; reorder or ignore cross-modality ones according to user intent):"
    lines = [heading, "", intro, ""]
    for index, item in enumerate(candidates, start=1):
        title = item.get("display_name") or item.get("endpoint") or f"endpoint {index}"
        label = _modality_label(item.get("modality_hint", "unknown"))
        lines.append(f"{_modelzoo_number_badge(index)} **{title}**{label}")
        lines.append("")
        description = item.get("description")
        if description:
            text = str(description).strip().replace("\n", " ")
            if len(text) > 120:
                text = text[:120] + "..."
            lines.append(f"- **Summary**: {text}")
        if item.get("category"):
            lines.append(f"- **Category**: {item['category']}")
        quality = item.get("match_quality")
        if quality == "dedicated":
            lines.append("- **Match**: ✅ dedicated capability endpoint, no prompt needed")
        elif quality == "model":
            lines.append("- **Match**: explicit model match, run by the parameter card")
        elif quality == "preferred":
            lines.append("- **Match**: preferred capability endpoint, run by the parameter card")
        elif quality == "general" and capability:
            lines.append("- **Match**: general ModelZoo capability, needs a prompt to constrain it (not a dedicated cutout model)")
        cover = item.get("cover_image")
        if cover:
            label_text = "example" if item.get("cover_image_source") == "outputs_example" else "icon"
            cover_lower = str(cover).lower()
            is_video_cover = any(cover_lower.endswith(ext) for ext in (".mp4", ".mov", ".webm"))
            if is_video_cover:
                lines.append(f"- **{label_text}** (video):")
                lines.append(cover)
            else:
                lines.append(f"- **{label_text}**: ![{title}]({cover})")
        lines.append("- **Can it run directly**: ✅ yes, it can start once the parameters are confirmed")
        if item.get("endpoint"):
            lines.append(f"- **endpoint**: `{item['endpoint']}`")
            lines.append(
                f"- **Next step**: `cli.py modelzoo-detail {item['endpoint']}` to see parameters / "
                f"`cli.py modelzoo-price {item['endpoint']}` to see price"
            )
        lines.append("")
    lines.append("Tell me the number or the endpoint name, and I will build the parameter card next.")
    if capability:
        lines.append("")
        lines.append("---")
        lines.append("⚠️ **The following instructions are for the agent / LLM; do not forward them verbatim to the user:**")
        lines.append("")
        lines.append(f"- This round was recognized as \"{cap_label}\"; prefer the ModelZoo endpoints above.")
        lines.append("- **Do not switch to AI app search automatically**; only switch when the user explicitly asks for apps/workflows.")
        if not any(item.get("match_quality") == "dedicated" for item in candidates):
            prompt = str(capability.get("suggested_prompt") or "").strip()
            lines.append("- When there is no dedicated endpoint, fall back to a general edit endpoint; do not treat \"find an app\" as the default next step.")
            if prompt:
                lines.append(f"- Suggested prompt for general editing: {prompt}")
    text = "\n".join(lines).strip()
    if discovery_mode:
        text += _discovery_followup_markdown(discovery_query or query)
    return text


def _fetch_outputs_example_url(api_key: str, endpoint: str) -> tuple[str | None, str]:
    """Hit detail once for a single endpoint and take the first example image/video URL from outputs_example.

    Returns (url, source). source is one of:
      - 'outputs_example': a real example image was obtained
      - 'none': nothing obtained (detail failed / outputs_example empty / the endpoint is an LLM/TTS type with no image)
    A single failure falls back silently, raises nothing, and does not affect other candidates.
    """
    if not endpoint:
        return None, "none"
    try:
        result = get_detail(api_key, endpoint)
    except Exception:
        return None, "none"
    data = (result.get("data") or {}).get("data") or result.get("data") or {}
    outputs = data.get("outputs_example") or {}
    if not isinstance(outputs, dict):
        return None, "none"
    images = outputs.get("images") or []
    if images:
        first = images[0] if isinstance(images, list) else None
        if isinstance(first, str) and first.strip():
            return first.strip(), "outputs_example"
    videos = outputs.get("videos") or []
    if videos:
        first = videos[0] if isinstance(videos, list) else None
        if isinstance(first, str) and first.strip():
            return first.strip(), "outputs_example"
    return None, "none"


def pick_endpoint_candidates(
    api_key: str,
    query: str,
    modality: str,
    *,
    limit: int = 10,
    page_size: int = 50,
) -> dict[str, Any]:
    """Search ModelZoo endpoints: capability mapping -> term expansion -> dedicated first.

    v1.12.5 design:
    - colloquial input is first mapped to a capability in capabilities.json (e.g. background_removal)
    - expand with capability words (instead of throwing "remove the background" at the literal keyword)
    - dedicated endpoints come first; when there is no dedicated endpoint, keep the general-editing fallback
    - return fallback_policy=do_not_search_apps, explicitly forbidding an automatic switch to AI apps
    """
    keyword = (query or "").strip()
    target = (modality or "image").lower()
    discovery_mode = is_discovery_query(keyword)
    capability = capabilities.detect(keyword)
    followup_query = discovery_followup_query(keyword, capability) if discovery_mode else None
    cap_name = capability.get('name') if capability else None
    cap_label = str((capability or {}).get('label') or '').strip() if capability else None
    expanded = capabilities.search_terms(keyword, capability)

    def _has_han(text: str) -> bool:
        return any('\u4e00' <= ch <= '\u9fff' for ch in str(text or ''))

    # Keep the full expansion for the API calls, but never print Chinese search terms.
    display_expanded = [term for term in expanded if not _has_han(term)]
    if not display_expanded:
        display_expanded = [cap_label or keyword]

    preferred = capabilities.preferred_endpoints(capability)
    dedicated = capabilities.dedicated_endpoints(capability)
    # When the user names a model (qwen/flux/...), keep the original query so capability words do not push the explicit model out.
    model_hints = ('qwen', 'flux', 'seedream', 'wan', 'kling', 'pixverse', 'vidu', 'minimax', 'hunyuan', 'nano banana', 'ideogram')
    model_hint = next((token for token in model_hints if token in keyword.lower()), None)
    if model_hint and keyword and keyword not in expanded:
        expanded = ([keyword] + expanded)[:3]

    def fetch(term: str) -> dict[str, Any]:
        return list_endpoints(api_key, keyword=term, page=1, page_size=page_size, sort="Auto")

    # Fetch at most 3 capability words in parallel; only successful results are merged, and the request only counts as failed if all fail.
    if not expanded:
        expanded = [keyword] if keyword else []
    results: list[dict[str, Any]] = []
    if expanded:
        with ThreadPoolExecutor(max_workers=min(3, len(expanded))) as pool:
            results = list(pool.map(fetch, expanded))
    ok_results = [r for r in results if api.result_is_ok(r)]
    if not ok_results:
        first = results[0] if results else {}
        return {
            "source": "modelzoo-pick",
            "modality": "video" if target == "video" else "image",
            "query": keyword,
            "capability": cap_name,
            "capability_label": cap_label,
            "expanded_queries": display_expanded,
            "fallback_policy": capabilities.fallback_policy(capability),
            "discovery_mode": discovery_mode,
            "followup_sources": ["ai_apps", "workflows"] if discovery_mode else [],
            "followup_question": "Want me to also check AI apps and Workflows?" if discovery_mode else None,
            "error": "REMOTE_REQUEST_FAILED",
            "http_status": first.get("status"),
            "message": api.request_error_message(first),
            "candidates": [],
            "reply_markdown": "",
        }

    # Merge and dedupe; keep the original raw item (cover_image is injected later).
    seen: set[str] = set()
    raw_unique: list[dict[str, Any]] = []
    def merge_results(items: list[dict[str, Any]]) -> None:
        for item in items:
            endpoint = str(item.get("endpoint") or "").strip()
            if not endpoint or endpoint in seen:
                continue
            seen.add(endpoint)
            raw_unique.append(item)
    for result in ok_results:
        data = (result.get("data") or {}).get("data") or result.get("data") or {}
        merge_results(data.get("list") or [])

    # Only when no dedicated endpoint hit do we search the fallback capability (bg -> image_edit),
    # so "there is no dedicated model" still has a general-editing fallback instead of switching to apps empty-handed.
    fallback_used = False
    if cap_name and dedicated and not any(str(it.get("endpoint") or "").strip() in dedicated for it in raw_unique):
        fallback_cap = capabilities.get_capability((capability or {}).get("fallback_capability"))
        if fallback_cap:
            fallback_terms = capabilities.search_terms(keyword, fallback_cap)
            fallback_terms = [term for term in fallback_terms if term not in expanded]
            if fallback_terms:
                with ThreadPoolExecutor(max_workers=min(3, len(fallback_terms))) as pool:
                    fallback_results = list(pool.map(fetch, fallback_terms))
                for result in fallback_results:
                    if not api.result_is_ok(result):
                        continue
                    data = (result.get("data") or {}).get("data") or result.get("data") or {}
                    merge_results(data.get("list") or [])
                fallback_used = True

    # Dedicated/preferred capability -> modality match -> everything else. This way "remove" does not push watermark first.
    def sort_key(it: dict[str, Any]) -> tuple[int, int]:
        endpoint = str(it.get("endpoint") or "").strip()
        if endpoint in dedicated:
            pref_rank = 0
        elif model_hint and model_hint in endpoint.lower():
            pref_rank = 1
        elif endpoint in preferred:
            pref_rank = 2
        elif cap_name:
            pref_rank = 3
        else:
            pref_rank = 0
        hint, _ = infer_modality_hint(it)
        hint_rank = 0 if hint == target else 1 if hint == "unknown" else 2
        return (pref_rank, hint_rank)

    raw_unique.sort(key=sort_key)
    top_n = raw_unique[:limit]

    # Hit detail in parallel for the outputs_example example image (only for the final top N)
    if top_n:
        with ThreadPoolExecutor(max_workers=min(10, len(top_n))) as pool:
            cover_results = list(pool.map(
                lambda it: _fetch_outputs_example_url(api_key, str(it.get("endpoint") or "")),
                top_n,
            ))
        for it, (url, source) in zip(top_n, cover_results):
            if url:
                it["_cover_image_url"] = url
                it["_cover_image_source"] = source

    candidates: list[dict[str, Any]] = []
    for it in top_n:
        candidate = summarize_modelzoo_candidate(it)
        endpoint = str(candidate.get("endpoint") or "").strip()
        if cap_name:
            candidate["capability"] = cap_name
            candidate["capability_label"] = cap_label
            candidate["model_match"] = bool(model_hint and model_hint in endpoint.lower())
            if endpoint in dedicated:
                candidate["match_quality"] = "dedicated"
                candidate["prompt_required"] = False
            elif candidate["model_match"]:
                candidate["match_quality"] = "model"
                candidate["prompt_required"] = True
            elif endpoint in preferred:
                candidate["match_quality"] = "preferred"
                candidate["prompt_required"] = True
            else:
                candidate["match_quality"] = "general"
                candidate["prompt_required"] = True
        candidates.append(candidate)

    return {
        "source": "modelzoo-pick",
        "modality": "video" if target == "video" else "image",
        "query": keyword,
        "capability": cap_name,
        "capability_label": cap_label,
        "expanded_queries": display_expanded,
        "fallback_policy": capabilities.fallback_policy(capability),
        "fallback_used": fallback_used,
        "suggested_prompt": capabilities.suggested_prompt(capability),
        "discovery_mode": discovery_mode,
        "followup_sources": ["ai_apps", "workflows"] if discovery_mode else [],
        "followup_query": followup_query,
        "followup_question": "Want me to also check AI apps and Workflows?" if discovery_mode else None,
        "candidates": candidates,
        "reply_markdown": build_modelzoo_reply_markdown(
            candidates, modality=target, query=keyword, capability=capability, discovery_mode=discovery_mode,
            discovery_query=followup_query,
        ),
    }
