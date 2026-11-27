"""Shared logic for dispatch.py + the 10 fixed models (5 image + 5 video) + metadata for the 4 search entries (image/video × ModelZoo / AI app).

Carries the pieces dispatch.py needs in order to run:
  - APP_SCRIPT / DISPATCH_SCRIPT: path constants used to compose subprocess commands
  - MODEL_MAP: user input aliases → internal slug
  - ROUTE_TABLE: slug → ModelZoo endpoint + modality (**to add a new menu model, change it here**)
  - SCENE_NUMBER_MAP: menu number → slug ("5" → "b2-t2i" / "v2" → "happyhorse-t2v" / "6" → "modelzoo-search-flow" / "7" → "remote-search-flow")
  - COMPILER_RULES: the model list for slug → {'defaults': {}}. **Defaults are not here** — the parameter set / defaults / labels / options all come dynamically from the remote contract
  - IMAGE_SCENE_MENU / VIDEO_SCENE_MENU: the menu text the user sees (loaded from config/menus.json)
  - the various runtime_state (resume cache) items, batch policy, and subprocess dispatch helpers

[How to add a new menu model]
  1) Add a row to ROUTE_TABLE {'slug': {'endpoint': '<modelzoo-endpoint>', 'modality': 'image/video'}} (note: the table is loaded from config/routes.json, so add it to the image / video section there)
  2) Add a few MODEL_MAP aliases → slug
  3) Take an empty slot in scene_number_map of config/routes.json
  4) Add a line of menu text to the matching entry of config/menus.json
  5) Display name: write "name" for that slug in config/routes.json; MODEL_DISPLAY_NAMES picks it up automatically.
      When it is missing, fall back to the slug itself; never hardcode model names in code
  6) COMPILER_RULES picks the slug up automatically (used to decide "emit a parameter card first / defaults allowed");
      do not hardcode the actual defaults, they all come from the remote detail's field_value.
  Fields, options, and labels — none of them need touching; they come from the remote contract automatically.
  Model names inside the recommendation text (recommend_image_option / recommend_video_option) must always be
  composed through slot_ref(), to avoid hardcoding model names — names always come from config/routes.json.
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, subprocess, sys, time
from collections.abc import Callable
from pathlib import Path
from paths import load_config_json, resolve_runtime_state_file
from common import normalized_text, is_discovery_query
import capabilities
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent

# Target script paths for subprocess dispatch. dispatch.py hands the work to app.py (the remote CLI),
# and batch.py hands the batch worker subprocess to dispatch.py itself.
# Note: never use Path(__file__) as the worker entry point — batch.py did that before,
# and because batch.py has no main(), the whole --batch-run failed silently.
APP_SCRIPT = SCRIPT_DIR / 'app.py'

DISPATCH_SCRIPT = SCRIPT_DIR / 'dispatch.py'

# User input aliases → internal slug. All keys are compared lowercase. Model names come from
# config/routes.json "name"; only the ways a user might refer to a model live here.
# The non-ASCII entries below are internal input aliases; never displayed.
MODEL_MAP = {
    'bpro-t2i': 'bpro-t2i', 'fkmax-t2i': 'fkmax-t2i', 'seedream5-t2i': 'seedream5-t2i', 'o2-t2i': 'o2-t2i', 'b2-t2i': 'b2-t2i',
    'v31pro-t2v': 'v31pro-t2v', 'happyhorse-t2v': 'happyhorse-t2v', 'kling30-t2v': 'kling30-t2v', 'wan27-t2v': 'wan27-t2v', 'seedance2-t2v': 'seedance2-t2v',
    '\u901a\u4e49\u5343\u95ee': 'bpro-t2i', '\u901a\u4e49\u5343\u95ee image 2.1': 'bpro-t2i', 'qwen image': 'bpro-t2i', 'qwen image 2.1': 'bpro-t2i', 'qwen-image-2.1': 'bpro-t2i', 'qwen': 'bpro-t2i',
    'flux 3': 'fkmax-t2i', 'flux3': 'fkmax-t2i', 'flux-3': 'fkmax-t2i', 'flux': 'fkmax-t2i',
    'seedream 5.0 flash': 'seedream5-t2i', 'seedream 5.0': 'seedream5-t2i', 'seedream flash': 'seedream5-t2i', 'seedream': 'seedream5-t2i', '\u5373\u68a6': 'seedream5-t2i',
    'nano banana': 'o2-t2i', 'nano banana 2.1': 'o2-t2i', 'nano-banana-2.1': 'o2-t2i',
    'gpt image': 'b2-t2i', 'gpt image 2.5': 'b2-t2i', 'gpt image 2.5 sunburst': 'b2-t2i', 'gpt-image-2.5': 'b2-t2i', 'sunburst': 'b2-t2i',
    'flux 3 video': 'v31pro-t2v', 'flux 3 \u89c6\u9891': 'v31pro-t2v',
    'happy horse': 'happyhorse-t2v', 'happy horse 1.1': 'happyhorse-t2v', 'happyhorse': 'happyhorse-t2v', 'happyhorse 1.1': 'happyhorse-t2v', '\u5feb\u4e50\u9a6c': 'happyhorse-t2v',
    '\u53ef\u7075': 'kling30-t2v', '\u53ef\u7075 3.0 pro': 'kling30-t2v', '\u53ef\u70753.0': 'kling30-t2v', 'kling 3.0 pro': 'kling30-t2v', 'kling 3.0': 'kling30-t2v', 'kling': 'kling30-t2v',
    '\u4e07\u76f8': 'wan27-t2v', '\u4e07\u76f8 2.7': 'wan27-t2v', 'wan 2.7': 'wan27-t2v', 'wan2.7': 'wan27-t2v', 'wan': 'wan27-t2v',
    'seedance': 'seedance2-t2v', 'seedance 2.0': 'seedance2-t2v',
}

# Default slug to fall back to by task type when the user did not name a model.
TASK_DEFAULTS = {'text-to-image': 'bpro-t2i', 'illustration-anime': 'bpro-t2i', 'commercial-visual': 'seedream5-t2i', 'text-layout-image': 'o2-t2i', 'text-to-video': 'wan27-t2v', 'cinematic-video': 'v31pro-t2v'}

# slug → ModelZoo endpoint + modality. Fixed models call the ModelZoo API directly; other models go through the app.py webapp path.
# The routing table and menus are loaded from config/ (**to add a new menu model, only edit config/routes.json + config/menus.json**)
def _load_routes_config():
    config_dir = SCRIPT_DIR.parent / 'config'
    routes_file = config_dir / 'routes.json'
    if routes_file.exists():
        data = json.loads(routes_file.read_text(encoding='utf-8'))
        table = {}
        # The model display name comes from the name field of routes.json; menu recommendations / execution hints
        # all use it, and no model name is hardcoded in code.
        names = {}
        for slug, info in (data.get('image') or {}).items():
            table[slug] = {'endpoint': info['endpoint'], 'modality': 'image'}
            if info.get('name'):
                names[slug] = info['name']
        for slug, info in (data.get('video') or {}).items():
            table[slug] = {'endpoint': info['endpoint'], 'modality': 'video'}
            if info.get('name'):
                names[slug] = info['name']
        return table, data.get('scene_number_map') or {}, names
    return {}, {}, {}

def _load_menus_config():
    config_dir = SCRIPT_DIR.parent / 'config'
    menus_file = config_dir / 'menus.json'
    if menus_file.exists():
        data = json.loads(menus_file.read_text(encoding='utf-8'))
        return data.get('image_menu', ''), data.get('video_menu', '')
    return '', ''

_rt, _snm, _cfg_names = _load_routes_config()
_im, _vm = _load_menus_config()
ROUTE_TABLE = _rt
IMAGE_SCENE_MENU = _im
VIDEO_SCENE_MENU = _vm
SCENE_NUMBER_MAP = _snm
# Display names (from the name field of config/routes.json) — the single source of names for menus, recommendations, and execution hints.
MODEL_DISPLAY_NAMES = _cfg_names

# slug → default parameters. **Only defaults, no fields** — the field set / labels / options
# all come dynamically from the remote contract (see remote.choose_remote_field_display_label
# / humanize_remote_prefill_label). The defaults here only act as a fallback in two places:
#   1) fields in card.compile_card_selection that "the user did not fill in and notes did not infer"
#   2) batch.should_batch_use_defaults, to decide whether this model runs "with no parameters at all"
# A new model may omit defaults; if given, they are a fallback.
# Which fixed models need the "show a parameter card first / may explicitly take defaults" behavior marker.
#
# Note: **only the slug list is kept here, defaults are always empty**. The real defaults come from the
# remote detail's input_params[].field_value (see modelzoo.build_task_payload).
# Historical lesson: resolution=4K / 720*1280 / 768P used to be hardcoded here, which did not match the
# remote schema and made default-parameter submissions return 400. Never write concrete parameter values here again.
COMPILER_RULES = {slug: {'defaults': {}} for slug in ROUTE_TABLE}

CONFIGURED_MAX_BATCH_CONCURRENCY_FALLBACK = 3

SYSTEM_MAX_BATCH_CONCURRENCY = 5

CONFIGURED_MAX_BATCH_TASKS_FALLBACK = 5

SYSTEM_MAX_BATCH_TASKS = 10

RUNTIME_STATE_VERSION = 3

RUNTIME_STATE_TTL_SECONDS = 30 * 60

RUNTIME_STATE_DISABLED_ENV = 'BIZYAIR_DISABLE_RUNTIME_STATE'

RUNTIME_SESSION_ID_ENV = 'BIZYAIR_SESSION_ID'

HOST_SESSION_TOKEN_KEYS = ['CODEX_THREAD_ID', 'TERM_SESSION_ID', 'TMUX', 'STY', 'KITTY_WINDOW_ID', 'KITTY_PID', 'WEZTERM_PANE']

# internal input alias; never displayed
INHERIT_MARKERS = ['\u8ddf\u521a\u624d\u4e00\u6837', '\u548c\u521a\u624d\u4e00\u6837', '\u6309\u521a\u624d', '\u540c\u4e0a', '\u4e0a\u4e00\u8f6e', '\u6cbf\u7528\u521a\u624d', '\u7ee7\u7eed\u521a\u624d', '\u4fdd\u6301\u521a\u624d', '\u548c\u4e0a\u6b21\u4e00\u6837', '\u8ddf\u4e0a\u6b21\u4e00\u6837', '\u6309\u4e0a\u6b21', '\u6cbf\u7528\u4e0a\u6b21', '\u7ee7\u7eed\u4e0a\u6b21', '\u4fdd\u6301\u4e0a\u6b21', '\u7167\u4e0a\u6b21', 'same as before', 'same as last time']

# internal input alias; never displayed
RUN_AUTH_MARKERS = ['\u5f00\u8dd1', '\u76f4\u63a5\u8dd1', '\u786e\u8ba4\u6267\u884c', '\u786e\u8ba4\u751f\u6210', '\u5f00\u59cb\u751f\u6210', '\u751f\u6210\u5427', '\u5f00\u59cb\u5427', '\u63d0\u4ea4\u5427', '\u8dd1\u5427', 'go ahead', 'run it', 'confirm run', 'confirm execution']

# internal input alias; never displayed
RUN_AUTH_NEGATION_MARKERS = ['\u4e0d\u8981\u76f4\u63a5\u8dd1', '\u5148\u522b\u76f4\u63a5\u8dd1', '\u5148\u4e0d\u8981\u76f4\u63a5\u8dd1', '\u522b\u76f4\u63a5\u8dd1', '\u4e0d\u8981\u8dd1', '\u5148\u522b\u8dd1', '\u5148\u4e0d\u8dd1', '\u5148\u4e0d\u8981\u8dd1', '\u4e0d\u8981\u6267\u884c', '\u5148\u522b\u6267\u884c', '\u5148\u4e0d\u6267\u884c', '\u4e0d\u8981\u751f\u6210', '\u5148\u522b\u751f\u6210', '\u5148\u4e0d\u751f\u6210', '\u4e0d\u76f4\u63a5\u6267\u884c', '\u6682\u4e0d\u6267\u884c']

def resolve_model(task: str | None, model: str | None) -> str:
    if model:
        normalized = model.strip()
        lowered = normalized.lower()
        # Exact match first, then lowercase match, then pass the input through as a slug (unknown names are reported by the caller).
        return MODEL_MAP.get(normalized) or MODEL_MAP.get(lowered) or normalized
    if task and task in TASK_DEFAULTS:
        return TASK_DEFAULTS[task]
    return 'bpro-t2i'

def has_any(text: str, terms: list[str]) -> bool:
    return any((term in text for term in terms))

def looks_like_image_edit_prompt(prompt: str | None) -> bool:
    q = normalized_text(prompt)
    if not q:
        return False
    # internal input alias; never displayed
    if has_any(q, ['\u6539\u56fe', '\u91cd\u7ed8', '\u6362\u80cc\u666f', '\u53bb\u6389\u80cc\u666f', '\u53bb\u9664\u80cc\u666f', '\u79fb\u9664\u80cc\u666f', '\u80cc\u666f\u79fb\u9664', '\u62a0\u56fe', '\u900f\u660e\u5e95', '\u767d\u5e95\u56fe', '\u5c40\u90e8\u91cd\u7ed8', '\u56fe\u751f\u56fe', '\u53c2\u8003\u56fe', '\u6269\u56fe', '\u4fee\u56fe']):
        return True
    # internal input alias; never displayed (Chinese phrases are matched, never printed)
    background_edit_patterns = ['\u6362.{0,3}\u80cc\u666f', '\u6539.{0,3}\u80cc\u666f', '\u80cc\u666f.{0,3}\u6362', '\u80cc\u666f.{0,3}\u6539']
    return any((re.search(pattern, q) for pattern in background_edit_patterns))

def slot_ref(option: str, model: str) -> str:
    """Build the "option N (<model name>)" reference fragment.

    Model names that start with an ASCII letter/digit already read fine after the space,
    so no extra separator is needed.
    """
    name = model_bare_name(model)
    return f'option {option} ({name})'

def wants_explicit_ai_app(q: str) -> bool:
    """Whether the user explicitly wants a community AI app / workflow rather than just describing a generation need."""
    # internal input alias; never displayed
    return has_any(q, ['ai \u5e94\u7528', 'ai\u5e94\u7528', '\u793e\u533a\u5e94\u7528', '\u5e94\u7528', 'app', 'workflow', '\u5de5\u4f5c\u6d41', '\u73a9\u6cd5'])

def recommend_image_option(prompt: str | None, images: list[str] | None) -> tuple[str, str]:
    q = normalized_text(prompt)
    image_count = len(images or [])
    if wants_explicit_ai_app(q):
        return ('7', 'You explicitly asked for a community AI app / Workflow, so go with option 7; if you already have a link or ID, send it to me directly.')
    if is_discovery_query(q):
        capability = capabilities.detect(q)
        capability_name = (capability or {}).get('label') or 'the relevant capability'
        return ('6', f'You are asking "which services/solutions are available", not placing an order. I will first have option 6 (ModelZoo) return runnable endpoints for "{capability_name}", then ask whether you want me to also check AI apps / Workflows.')
    # v1.12.4: fuzzy/short requests default to ModelZoo. Image-to-image/editing looks for the underlying endpoint first,
    # and no longer pushes the user into the slow, literal-matching AI app search by default.
    capability = capabilities.detect(q)
    if capability and capability.get('name') == 'background_removal':
        return ('6', 'This is a background removal / cutout request. ModelZoo has a dedicated background-remover endpoint: it only needs an image, no prompt, and it is faster than searching AI apps. When you want to keep shadows or swap in a new background, use general editing instead.')
    if capability and capability.get('name') == 'style_transfer':
        return ('6', 'This is image style transfer. ModelZoo general image edit / image-to-image endpoints can constrain the style through the prompt, so start with option 6 to find a runnable endpoint; only go to option 7 when you explicitly want a specific community app/workflow.')
    if capability or image_count >= 1 or looks_like_image_edit_prompt(q):
        return ('6', 'This looks like an image-to-image / image editing route. Start with option 6 (ModelZoo) and search for image edit / image-to-image endpoints: the parameters are explicit and billing is per run, which is faster than hunting keywords inside AI apps. Only go to option 7 when you explicitly want a specific community app/workflow.')
    # internal input alias; never displayed
    if has_any(q, ['endpoint', '\u8c03\u7528api', '\u8c03\u7528 api', '\u6309\u6b21\u6263\u8d39', '\u5e95\u5c42\u6a21\u578b', 'modelzoo', 'model zoo', '\u6a21\u578bapi', '\u6a21\u578b api']):
        return ('6', 'You want to call the underlying model API directly, so option 6 (ModelZoo) search is the better fit: explicit parameters and stable per-run billing.')
    # internal input alias; never displayed
    if has_any(q, ['\u6d77\u62a5', '\u5e7f\u544a', 'kv', '\u4e3b\u89c6\u89c9', '\u5c01\u9762']):
        return ('3', f'Your goal looks like a key-visual final render, and {slot_ref("3", "seedream5-t2i")} usually gives you that "ready to use" feel.')
    # internal input alias; never displayed
    if has_any(q, ['\u4e2d\u6587', '\u6c49\u5b57', '\u6392\u7248', '\u6807\u9898\u5b57', '\u5b57\u6548', 'logo\u5b57']):
        # Text/layout needs map to option 2 FLUX 3 (text rendering is where this tier shines).
        return ('2', f'You clearly care that the text and layout inside the frame do not fall apart, so I would lean toward {slot_ref("2", "fkmax-t2i")} first — text rendering is exactly where this tier shines.')
    # internal input alias; never displayed
    if has_any(q, ['\u5199\u5b9e', '\u8d28\u611f', '\u6444\u5f71', '\u4eba\u50cf', '\u5927\u7247', '\u5199\u771f']):
        return ('2', f'Your request sounds more like "not too AI, ideally with a real-shot texture", so I would rather have you look at {slot_ref("2", "fkmax-t2i")} first.')
    # internal input alias; never displayed
    if has_any(q, ['\u7ec6\u8282', '\u9ad8\u7ea7\u611f', '\u7535\u5546', '\u5546\u54c1\u56fe', '\u8be6\u60c5\u9875', '\u5ba1\u7f8e']):
        # Split by purpose: product/commercial final renders go to option 3 Seedream 5.0 Flash; pure detail/texture goes to option 2 FLUX 3.
        # internal input alias; never displayed
        if has_any(q, ['\u7535\u5546', '\u5546\u54c1\u56fe', '\u8be6\u60c5\u9875']):
            return ('3', f'This looks more like a product/commercial final render you can publish directly, so I would rather push {slot_ref("3", "seedream5-t2i")} forward.')
        return ('2', f'You care more about detail, texture and aesthetics here, so I would rather push {slot_ref("2", "fkmax-t2i")} forward.')
    # internal input alias; never displayed
    if has_any(q, ['\u5feb\u4e00\u70b9', '\u5feb\u901f', '\u5148\u6765\u4e00\u7248', '\u5148\u51fa\u4e00\u5f20', '\u8d76\u65f6\u95f4']):
        return ('5', f'You seem to want speed first and a version to feel out the direction, so {slot_ref("5", "b2-t2i")} is easier — it is the "faster pace" tier.')
    return ('1', f'You have not locked the style down yet, so try {slot_ref("1", "bpro-t2i")} first — it is more forgiving.')

def recommend_video_option(prompt: str | None, images: list[str] | None, audios: list[str] | None) -> tuple[str, str]:
    q = normalized_text(prompt)
    image_count = len(images or [])
    audio_count = len(audios or [])
    if wants_explicit_ai_app(q):
        return ('7', 'You explicitly asked for a community AI app / Workflow, so go with option 7; if you already have a link or ID, send it to me directly.')
    if is_discovery_query(q):
        capability = capabilities.detect(q)
        capability_name = (capability or {}).get('label') or 'the relevant capability'
        return ('6', f'You are asking "which services/solutions are available", not placing an order. I will first have option 6 (ModelZoo) return runnable endpoints for "{capability_name}", then ask whether you want me to also check AI apps / Workflows.')
    # v1.12.4: image-to-video / lip-sync etc. look for ModelZoo image-to-video / lip-sync endpoints first.
    # internal input alias; never displayed
    if image_count >= 1 or audio_count >= 1 or has_any(q, ['\u56fe\u751f\u89c6\u9891', '\u9996\u5c3e\u5e27', '\u9996\u5e27', '\u5c3e\u5e27', '\u8fc7\u6e21', '\u8f6c\u573a', '\u5bf9\u53e3\u578b', '\u53e3\u64ad', '\u8bf4\u8bdd', '\u5634\u578b', '\u97f3\u9891', '\u914d\u97f3', '\u5531\u6b4c']):
        return ('6', 'This is not plain text-to-video. Start with option 6 (ModelZoo) and search for image-to-video / lip-sync endpoints (for example PixVerse image-to-video); that is more direct than searching inside AI apps. Only go to option 7 when you explicitly want a specific community app/workflow.')
    # internal input alias; never displayed
    if has_any(q, ['endpoint', '\u8c03\u7528api', '\u8c03\u7528 api', '\u6309\u6b21\u6263\u8d39', '\u5e95\u5c42\u6a21\u578b', 'modelzoo', 'model zoo', '\u6a21\u578bapi', '\u6a21\u578b api']):
        return ('6', 'You want to call the underlying video model API directly, so option 6 (ModelZoo) search is the better fit: explicit parameters and stable per-run billing.')
    # internal input alias; never displayed
    if has_any(q, ['\u7535\u5f71\u611f', '\u955c\u5934', '\u5149\u5f71', '\u8d28\u611f', '\u5927\u7247']):
        return ('1', f'You are clearly after a cinematic feel and shot language, so I would like to push {slot_ref("1", "v31pro-t2v")} first.')
    # internal input alias; never displayed
    if has_any(q, ['\u5267\u60c5', '\u53d9\u4e8b', '\u5206\u955c', '\u8bb2\u6545\u4e8b', '\u8fde\u7eed\u573a\u666f']):
        return ('2', f'You want a quick first version to feel out the direction, so {slot_ref("2", "happyhorse-t2v")} is easier — fast output at a comfortable cost/performance ratio.')
    # internal input alias; never displayed
    if has_any(q, ['\u52a8\u4f5c', '\u6253\u6597', '\u8fd0\u52a8', '\u5feb\u8282\u594f', '\u51b2\u51fb\u529b']):
        return ('3', f'This one needs to move and hit hard, so I would rather have you look at {slot_ref("3", "kling30-t2v")} first.')
    # internal input alias; never displayed
    if has_any(q, ['\u4e2d\u6587', '\u7a33\u5b9a', '\u91cf\u4ea7', '\u5e7f\u544a\u811a\u672c', '\u7701\u94b1']):
        return ('4', f'This looks like stable mass production where Chinese must not drift, so {slot_ref("4", "wan27-t2v")} is less trouble.')
    # internal input alias; never displayed
    if has_any(q, ['\u8868\u73b0\u529b', '\u6f14\u7ece', '\u4eba\u7269\u52a8\u4f5c', '\u8868\u60c5', '\u98ce\u683c\u5316']):
        return ('5', f'You care more about character performance and a dynamic feel, so {slot_ref("5", "seedance2-t2v")} will land better.')
    return ('4', f'You have not locked the style down yet, so look at {slot_ref("4", "wan27-t2v")} first — it is usually more stable.')

def discovery_menu_note(prompt: str | None) -> str:
    if not is_discovery_query(prompt) or wants_explicit_ai_app(normalized_text(prompt)):
        return ''
    return (
        '\n\n🔎 **You are asking "which services are available"**: this menu only gives a first direction across ModelZoo / featured models. '
        'AI apps and Workflows may cover this capability too; want me to keep checking? Reply "check apps" / "check workflows" / "check both".'
    )


def _menu_options(entries: list[tuple[str, str]], *, modality: str) -> list[dict[str, Any]]:
    """Build structured menu options.

    entries are (display number, scene key) pairs. The display number is what the human-facing
    menu shows ("reply 1-7"); the scene key is the canonical --scene value ("4" for image,
    "v4" for video). Keeping both avoids the old mismatch where a recommendation of "4"
    could not be matched to video options numbered "v1"..."v5".
    """
    options: list[dict[str, Any]] = []
    for display_number, scene_key in entries:
        slug = SCENE_NUMBER_MAP.get(scene_key)
        if not slug or slug not in ROUTE_TABLE:
            continue
        options.append({
            'number': display_number,
            'option_index': int(display_number),
            'scene_number': scene_key,
            'type': 'model',
            'id': slug,
            'name': MODEL_DISPLAY_NAMES.get(slug) or slug,
            'endpoint': ROUTE_TABLE[slug].get('endpoint'),
            'modality': ROUTE_TABLE[slug].get('modality') or modality,
        })
    options.append({'number': '6', 'option_index': 6, 'scene_number': 'v6' if modality == 'video' else '6', 'type': 'modelzoo_search', 'name': 'ModelZoo search'})
    options.append({'number': '7', 'option_index': 7, 'scene_number': 'v7' if modality == 'video' else '7', 'type': 'ai_app_search', 'name': 'AI app search'})
    return options


def build_image_menu_payload(prompt: str | None, images: list[str] | None) -> dict[str, Any]:
    option, reason = recommend_image_option(prompt, images)
    options = _menu_options([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5')], modality='image')
    scene_key = next((item['scene_number'] for item in options if item['number'] == option), option)
    return {
        'menu_type': 'image',
        'recommended_option': option,
        'recommended_scene_number': scene_key,
        'reason': reason,
        'options': options,
        'discovery_note': discovery_menu_note(prompt) or None,
    }


def build_video_menu_payload(prompt: str | None, images: list[str] | None, audios: list[str] | None) -> dict[str, Any]:
    option, reason = recommend_video_option(prompt, images, audios)
    options = _menu_options([('1', 'v1'), ('2', 'v2'), ('3', 'v3'), ('4', 'v4'), ('5', 'v5')], modality='video')
    scene_key = next((item['scene_number'] for item in options if item['number'] == option), option)
    return {
        'menu_type': 'video',
        'recommended_option': option,
        'recommended_scene_number': scene_key,
        'reason': reason,
        'options': options,
        'discovery_note': discovery_menu_note(prompt) or None,
    }


def build_image_menu_message(prompt: str | None, images: list[str] | None) -> str:
    (option, reason) = recommend_image_option(prompt, images)
    return f'{IMAGE_SCENE_MENU}\nI suggest looking at option {option} first — {reason}\nJust reply 1-7; if you name a model or number explicitly, we follow that instead.{discovery_menu_note(prompt)}'

def build_video_menu_message(prompt: str | None, images: list[str] | None, audios: list[str] | None) -> str:
    (option, reason) = recommend_video_option(prompt, images, audios)
    return f'{VIDEO_SCENE_MENU}\nI suggest looking at option {option} first — {reason}\nJust reply 1-7; if you name a model or number explicitly, we follow that instead.{discovery_menu_note(prompt)}'

def is_modelzoo_endpoint(value: str | None) -> bool:
    """Whether a batch/dispatch target is an arbitrary ModelZoo endpoint ID.

    Fixed routes use slugs such as ``bpro-t2i``; app inputs are numeric IDs or links.
    An endpoint is a slash-separated identifier like ``bizyair/image-tools/background-remover``.
    URLs (``://``) and paths starting with ``/`` are not endpoint IDs.
    """
    text = str(value or '').strip()
    if not text or '://' in text or text.startswith('/') or text.endswith('/'):
        return False
    if 'bizyair.ai/' in text or text.startswith(('www.', 'community/', 'models/', 'app/')):
        return False
    return '/' in text

def is_video_model(model: str | None) -> bool:
    if model in ROUTE_TABLE:
        return ROUTE_TABLE[model]['modality'] == 'video'
    return False

# Emoji is bound to the slug; display_model_name always carries an emoji.
MODEL_EMOJI = {'bpro-t2i': '📷', 'fkmax-t2i': '🖼️', 'seedream5-t2i': '🌈', 'o2-t2i': '🎨', 'b2-t2i': '⚡', 'v31pro-t2v': '🎬', 'happyhorse-t2v': '🐎', 'kling30-t2v': '🐉', 'wan27-t2v': '🌊', 'seedance2-t2v': '💃'}


def model_bare_name(model: str | None) -> str:
    """Model display name without the emoji, for inline use in body text (menu recommendations)."""
    slug = model or ''
    return MODEL_DISPLAY_NAMES.get(slug) or slug or 'this model'


def display_model_name(model: str | None) -> str:
    slug = model or ''
    bare = model_bare_name(slug)
    emoji = MODEL_EMOJI.get(slug)
    return f'{emoji} {bare}' if emoji else bare

def build_run_dispatch_message(model: str | None) -> str:
    name = display_model_name(model)
    if is_video_model(model):
        return f'All set — {name} is running now. Video tasks take a little longer, so I will keep an eye on the progress for you!'
    return f'All set — submitted to {name}. I am watching the progress here and will send the image to you the moment it is ready!'

def runtime_state_enabled() -> bool:
    return os.environ.get(RUNTIME_STATE_DISABLED_ENV, '').strip().lower() not in {'1', 'true', 'yes', 'on'}

def resolve_runtime_terminal_token() -> str | None:
    for fd in [0, 1, 2]:
        try:
            if os.isatty(fd):
                value = os.ttyname(fd)
                if value:
                    return str(value)
        except Exception:
            continue
    return None

def resolve_runtime_session_signal() -> tuple[str | None, str | None]:
    explicit = os.environ.get(RUNTIME_SESSION_ID_ENV, '').strip()
    if explicit:
        return ('explicit_env', explicit)
    for key in HOST_SESSION_TOKEN_KEYS:
        value = os.environ.get(key)
        if value:
            return ('host_env', f'{key}:{value}')
    terminal_token = resolve_runtime_terminal_token()
    if terminal_token:
        return ('terminal', terminal_token)
    try:
        parent_pid = int(os.getppid())
    except Exception:
        parent_pid = 0
    if parent_pid > 1:
        return ('parent_pid', str(parent_pid))
    return (None, None)

def build_runtime_scope_hash(scope: dict[str, Any]) -> str:
    payload = {'workspace_root': str(scope.get('workspace_root') or ''), 'invocation_cwd': str(scope.get('invocation_cwd') or ''), 'session_source': str(scope.get('session_source') or ''), 'session_token': str(scope.get('session_token') or '')}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()[:16]

def resolve_runtime_scope() -> dict[str, Any]:
    (session_source, session_token) = resolve_runtime_session_signal()
    scope = {'workspace_root': str(SCRIPT_DIR.parent.resolve()), 'invocation_cwd': str(Path.cwd().resolve()), 'session_source': session_source, 'session_token': session_token}
    scope['scope_hash'] = build_runtime_scope_hash(scope)
    return scope

def normalize_runtime_state_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {'version': RUNTIME_STATE_VERSION, 'last_run': {}}
    last_run = payload.get('last_run')
    if not isinstance(last_run, dict):
        last_run = {}
    return {'version': payload.get('version') if isinstance(payload.get('version'), int) else RUNTIME_STATE_VERSION, 'last_run': last_run}

def build_runtime_state_entry(app_id: str, args: argparse.Namespace, execution_prompt: str | None) -> dict[str, Any]:
    now = int(time.time())
    scope = resolve_runtime_scope()
    return {'version': RUNTIME_STATE_VERSION, 'app_id': str(app_id), 'saved_at': now, 'expires_at': now + RUNTIME_STATE_TTL_SECONDS, 'scope': scope, 'prompt': execution_prompt or args.prompt, 'raw_prompt': args.prompt, 'image': list(args.image or []), 'audio': list(args.audio or []), 'video': list(args.video or []), 'aspect_ratio': args.aspect_ratio, 'resolution': args.resolution, 'duration': args.duration, 'seed': args.seed, 'random_seed': bool(args.random_seed), 'model_name': args.model_name, 'width': args.width, 'height': args.height, 'param': list(args.param or [])}

def runtime_state_entry_is_valid(entry: Any, *, now: int | None=None) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get('version') != RUNTIME_STATE_VERSION:
        return False
    expires_at = entry.get('expires_at')
    saved_at = entry.get('saved_at')
    try:
        expires_at = int(expires_at)
        saved_at = int(saved_at)
    except Exception:
        return False
    current_ts = int(now if now is not None else time.time())
    if expires_at < current_ts or saved_at > current_ts:
        return False
    scope = entry.get('scope') if isinstance(entry.get('scope'), dict) else {}
    expected_scope = resolve_runtime_scope()
    workspace_root = scope.get('workspace_root')
    if workspace_root and workspace_root != expected_scope['workspace_root']:
        return False
    invocation_cwd = scope.get('invocation_cwd')
    if invocation_cwd and invocation_cwd != expected_scope['invocation_cwd']:
        return False
    stored_hash = str(scope.get('scope_hash') or '').strip()
    expected_hash = str(expected_scope.get('scope_hash') or '').strip()
    if stored_hash and expected_hash:
        return stored_hash == expected_hash
    session_token = scope.get('session_token')
    expected_token = expected_scope.get('session_token')
    if session_token and expected_token and (session_token != expected_token):
        return False
    session_source = scope.get('session_source')
    expected_source = expected_scope.get('session_source')
    if session_source and expected_source and (session_source != expected_source):
        return False
    return True

def load_runtime_state() -> dict:
    state_file = resolve_runtime_state_file()
    if not runtime_state_enabled():
        return {'version': RUNTIME_STATE_VERSION, 'last_run': {}}
    try:
        if state_file.exists():
            return normalize_runtime_state_payload(json.loads(state_file.read_text(encoding='utf-8')))
    except Exception:
        pass
    return {'version': RUNTIME_STATE_VERSION, 'last_run': {}}

def save_runtime_state(state: dict) -> None:
    state_file = resolve_runtime_state_file()
    if not runtime_state_enabled():
        return
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        normalized = normalize_runtime_state_payload(state)
        state_file.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass

def clear_runtime_state() -> bool:
    state_file = resolve_runtime_state_file()
    try:
        if state_file.exists():
            state_file.unlink()
            return True
    except Exception:
        pass
    return False

def load_skill_config() -> dict:
    return load_config_json()

def classify_error_bucket(payload: dict) -> str:
    error = str(payload.get('error') or '')
    message = json.dumps(payload, ensure_ascii=False).lower()
    if error == 'NO_API_KEY':
        return 'missing_api_key'
    if error == 'LOCAL_APP_CATALOG_REMOVED':
        return 'local_app_catalog_removed'
    if error == 'REMOTE_APP_RUN_NOT_SUPPORTED':
        return 'remote_app_run_not_supported'
    if error == 'REMOTE_APP_RUN_ATTEMPT_FAILED' and any((m in message for m in ['webappid', 'web_app_id', 'invalid parameter: webappid', 'invalid parameter: webappid'])):
        return 'remote_app_run_not_supported'
    if error == 'FILE_NOT_FOUND':
        return 'missing_file'
    if error == 'REMOTE_APP_RUN_REQUIRES_USER_MEDIA':
        return 'missing_user_media'
    if error == 'UPLOAD_FAILED':
        if any((m in message for m in ['too large', 'size', 'format', 'invalid', 'image', 'audio'])):
            return 'bad_upload_input'
        return 'upload_or_station'
    if error == 'TIMEOUT':
        return 'station_timeout'
    if error == 'APP_NOT_FOUND':
        return 'app_not_found'
    if error == 'TASK_FAILED':
        if any((m in message for m in ['quota', 'rate', 'busy', 'overload', 'internal', 'upstream', 'service unavailable', 'temporarily', 'network', 'timeout'])):
            return 'station_runtime'
        if any((m in message for m in ['prompt', 'param', 'invalid', 'missing', 'unsupported', 'format', 'size', 'empty', 'not found', 'image', 'audio', 'upload'])):
            return 'bad_input_runtime'
        return 'station_runtime'
    return 'raw'

def friendly_error_message(payload: dict) -> str | None:
    bucket = classify_error_bucket(payload)
    if bucket == 'missing_api_key':
        return 'The BizyAir API key is not connected yet, so the task cannot run. Set up the key first, then we continue.'
    if bucket == 'local_app_catalog_removed':
        return 'The legacy local catalog channel has been shut down. The fixed image and video entries still work directly; for everything else use online search or open the detail page.'
    if bucket == 'remote_app_run_not_supported':
        reason = str(payload.get('reason') or '')
        feedback_channel = str(payload.get('feedback_channel') or '')
        if reason == 'workflow_without_ref_app':
            return 'This is a pure ComfyUI workflow with no matching AI app version, so running it directly is not supported yet. You can open it on the BizyAir website and load it into the ComfyUI editor to run it manually.'
        return 'The information and parameters are ready. However, the backend has not fully opened this path yet, so we will not run it directly this round.'
    if bucket == 'missing_user_media':
        missing_inputs = payload.get('missing_inputs') or {}
        parts = []
        if missing_inputs.get('image'):
            parts.append('image')
        if missing_inputs.get('audio'):
            parts.append('audio')
        if missing_inputs.get('video'):
            parts.append('video')
        joined = ', '.join(parts) if parts else 'media'
        return f'This round is missing your own media. This task must run with your own media; the built-in examples cannot be reused directly. Missing this round: {joined}.'
    if bucket == 'missing_file':
        return 'The file you want to use has not arrived yet. The path may be wrong, or the upload may not have finished. Send it again and we continue.'
    if bucket == 'bad_upload_input':
        return 'It got stuck on the media upload step; the file format or size may not meet the requirements. Try a common format, or make the file smaller and try again.'
    if bucket == 'upload_or_station':
        return 'The upload step is stuck; the BizyAir channel is fluctuating right now. Your media should be fine, so try again in a moment.'
    if bucket == 'station_timeout':
        return 'BizyAir took too long to process this time, so the request timed out. This is not your mistake; retry once in a little while.'
    if bucket == 'station_runtime':
        return 'BizyAir did not complete this time; the server may be busy or fluctuating. Your parameters were not the problem.'
    if bucket == 'bad_input_runtime':
        return 'The media or parameters do not quite match this time. The prompt, format or size may be off; let me help you find where.'
    if bucket == 'app_not_found':
        return 'No matching model or app was found. The number, name or ID may not line up. Send the object you picked again and I will check it.'
    return None

def format_success_payload(payload: dict) -> str | None:
    if payload.get('status') == 'ok' and payload.get('summary') and payload.get('verdict'):
        return f"{payload['summary']}\n{payload['verdict']}"
    return None

def ensure_api_key_ready(api_key_arg: str | None) -> None:
    cmd = [sys.executable, str(APP_SCRIPT), '--check']
    if api_key_arg:
        cmd += ['--api-key', api_key_arg]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    payload = None
    raw_stdout = (result.stdout or '').strip()
    if raw_stdout:
        try:
            payload = json.loads(raw_stdout)
        except Exception:
            payload = None
    if isinstance(payload, dict):
        status = payload.get('status')
        if status == 'ok':
            return
        if status == 'no_key':
            print(friendly_error_message({'error': 'NO_API_KEY'}))
            sys.exit(1)
        message = payload.get('message') or f'API key status is abnormal: {status or "unknown"}'
        print(json.dumps({'error': 'API_KEY_NOT_READY', 'status': status, 'message': message}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    # The check itself exited abnormally or its output cannot be parsed: treat it as not ready, never continue with a business command.
    print((result.stderr or result.stdout or 'API key check failed').strip(), file=sys.stderr)
    sys.exit(1)

def run_subprocess(args: list[str], *, success_callback: Callable[[], None] | None=None):
    result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode == 0:
        if success_callback:
            try:
                success_callback()
            except Exception:
                pass
        payload = None
        if result.stdout:
            raw_stdout = result.stdout.strip()
            if raw_stdout:
                try:
                    payload = json.loads(raw_stdout)
                except Exception:
                    for line in reversed(result.stdout.splitlines()):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            payload = json.loads(line)
                            break
                        except Exception:
                            continue
        friendly_success = format_success_payload(payload) if isinstance(payload, dict) else None
        if friendly_success:
            print(friendly_success)
            if result.stderr:
                print(result.stderr, end='', file=sys.stderr)
            sys.exit(result.returncode)
        if result.stdout:
            print(result.stdout, end='')
        if result.stderr:
            print(result.stderr, end='', file=sys.stderr)
        sys.exit(result.returncode)
    payload = None
    candidate_streams = [(result.stderr or '').strip(), (result.stdout or '').strip()]
    for text in candidate_streams:
        if not text:
            continue
        try:
            payload = json.loads(text)
            break
        except Exception:
            pass
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                break
            except Exception:
                continue
        if payload is not None:
            break
    friendly = friendly_error_message(payload) if isinstance(payload, dict) else None
    if friendly:
        print(friendly)
        sys.exit(result.returncode)
    if result.stdout:
        print(result.stdout, end='')
    if result.stderr:
        print(result.stderr, end='', file=sys.stderr)
    sys.exit(result.returncode)
