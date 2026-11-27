"""Card parsing round-trip + parameter state decisions.

After merging path A / path B, card.py no longer renders cards (rendering is fully
handled by remote.build_remote_prefilled_card); it only does:
  - parse_card_text:            turn the card text the user filled in → field dict
  - compile_card_selection:     field dict → args.aspect_ratio / resolution etc.
  - apply_compiled_selection:   write the result above back into argparse.Namespace
  - determine_parameter_state:  decide whether we are ready_to_run / awaiting_confirmation / missing_required
  - inherit_previous_run:      for resume cases like "same as before", copy the previous run's parameters from runtime_state
  - has_explicit_run_authorization / allows_explicit_defaults: recognize user phrases ("run it" / "default")

Shared by the dispatch.py main flow and batch.py.
"""
from __future__ import annotations
import argparse, re

import dispatch_common
from dispatch_common import (
    COMPILER_RULES, INHERIT_MARKERS, RUN_AUTH_MARKERS, RUN_AUTH_NEGATION_MARKERS,
)


def build_prompt_bundle_for_model(model: str | None, args: argparse.Namespace) -> dict:
    cached = getattr(args, '_compiled_prompt_bundle', None)
    task_hint = 'text-to-video' if dispatch_common.is_video_model(model) else 'text-to-image'
    signature = (model, task_hint, str(getattr(args, 'prompt', None) or ''), len(getattr(args, 'image', None) or []), len(getattr(args, 'audio', None) or []))
    if isinstance(cached, dict) and cached.get('_signature') == signature:
        return cached
    raw = re.sub(r'\s+', ' ', str(getattr(args, 'prompt', None) or '').strip())
    bundle = {'raw_prompt': raw, 'card_display_prompt': raw, 'execution_prompt': raw, 'polished_prompt': raw, 'structured_prompt': raw, 'enriched_prompt': raw, 'rewritten_prompt': '', 'negative_prompt': '', 'changed': False, 'polish_notes': [], 'mode': None, 'mode_source': 'passthrough', 'style_profile': {'name': None, 'source': 'none', 'en_hint': None}, 'rewrite_applied': False}
    bundle['_signature'] = signature
    setattr(args, '_compiled_prompt_bundle', bundle)
    return bundle


def collect_modelzoo_params(slug: str, args: argparse.Namespace) -> dict[str, str]:
    """Collect dispatch / batch args into ModelZoo user_params.

    Only values the user supplied explicitly are collected; no hardcoded defaults are
    injected (defaults are filled in by the remote detail's field_value inside
    build_task_payload).

    A --param without `=` / with an empty key raises modelzoo.ParamError; callers turn
    that into exit code 2 uniformly.
    """
    import modelzoo

    params: dict[str, str] = {}
    prompt_bundle = build_prompt_bundle_for_model(slug, args)
    execution_prompt = prompt_bundle.get('execution_prompt', '') or (getattr(args, 'prompt', None) or '')
    if execution_prompt:
        params['prompt'] = execution_prompt
    if getattr(args, 'aspect_ratio', None):
        params['aspect_ratio'] = str(args.aspect_ratio)
    if getattr(args, 'resolution', None):
        params['resolution'] = str(args.resolution)
    if getattr(args, 'steps', None):
        params['steps'] = str(args.steps)
    if getattr(args, 'duration', None):
        params['duration'] = str(args.duration)
    if getattr(args, 'seed', None):
        params['seed'] = str(args.seed)
    elif getattr(args, 'random_seed', False):
        params['seed'] = '-1'
    if getattr(args, 'model_name', None):
        params['model_name'] = str(args.model_name)
    if getattr(args, 'width', None):
        params['width'] = str(args.width)
    if getattr(args, 'height', None):
        params['height'] = str(args.height)
    raw_params = getattr(args, 'param', None) or []
    parsed, issues = modelzoo.parse_param_pairs(raw_params)
    if issues:
        raise modelzoo.ParamError(issues)
    params.update(parsed)
    return params


def _prefill_current(param: dict, user_params: dict) -> tuple[object, bool]:
    """Return (display value, whether the user supplied it explicitly). A missing required customtext does not show the vendor example."""
    field_name = str(param.get('field_name') or '')
    field_type = str(param.get('field_type') or '').lower()
    if field_name in user_params:
        return user_params[field_name], True
    if field_type == 'customtext' and param.get('required'):
        return '', False
    return param.get('field_value'), False


def render_modelzoo_param_lines(input_params: list[dict], user_params: dict) -> list[str]:
    """Render the remote input_params + user parameters as Markdown parameter lines (shared by the single-task card and the batch card)."""
    lines: list[str] = []
    for param in input_params:
        field_name = param.get('field_name')
        if not field_name:
            continue
        field_label = param.get('field_label') or field_name
        field_type = str(param.get('field_type') or '').lower()
        options = param.get('field_options') if isinstance(param.get('field_options'), dict) else {}
        current, provided = _prefill_current(param, user_params)
        default_value = param.get('field_value')
        suffix = '' if provided else (' (default)' if default_value not in (None, '', []) else '')

        if field_type == 'customtext':
            display = str(current or '') if current else ('(enter a value)' if param.get('required') else '(optional, leave blank)')
            if len(display) > 80:
                display = display[:80] + '...'
            lines.append(f'- **{field_label}**: {display}')
        elif field_type == 'combo':
            values = [str(item) for item in (options.get('values') or [])]
            values_str = ' / '.join(values[:8])
            if len(values) > 8:
                values_str += ' / ...'
            display = str(current) if current not in (None, '') else 'default'
            lines.append(f'- **{field_label}**: {display}{suffix} (options: {values_str})')
        elif field_type in ('number', 'slider', 'slides'):
            display = current if current not in (None, '') else 'default'
            lines.append(f'- **{field_label}**: {display}{suffix}')
        elif field_type == 'seed':
            display = str(current) if current not in (None, '') else '-1 (random)'
            lines.append(f'- **{field_label}**: {display}{suffix}')
        elif field_type == 'boolean':
            display = current if current is not None else default_value
            lines.append(f'- **{field_label}**: {display}{suffix}')
        else:
            display = current if current not in (None, '') else (default_value if default_value not in (None, '') else '')
            lines.append(f'- **{field_label}**: {display}{suffix}')
    return lines


def build_modelzoo_prefill_card(model_name: str, endpoint: str, input_params: list[dict], user_params: dict) -> str:
    lines = [f'## {model_name} parameter confirmation', '', f'- **endpoint**: `{endpoint}`']
    lines.extend(render_modelzoo_param_lines(input_params, user_params))
    lines.extend(['', '- **Billing confirmation**: credits are charged only on submit; one confirmation submits once. If you are not happy with the result, look at the output first and do not rerun automatically; a rerun requires a new confirmation.'])
    lines.extend(['', 'Once the parameters are confirmed, add `--confirm-run` to start execution.'])
    return '\n'.join(lines)


def has_explicit_tuning(args: argparse.Namespace) -> bool:
    return any([bool(args.card_input), bool(args.aspect_ratio), bool(args.resolution), bool(args.steps), bool(args.duration), bool(args.seed), bool(args.random_seed), bool(args.model_name), bool(args.width), bool(args.height), bool(args.param)])

def collect_intent_text(args: argparse.Namespace) -> str:
    texts = [getattr(args, 'prompt', None), getattr(args, 'card_input', None)]
    return dispatch_common.normalized_text('\n'.join([str(x or '') for x in texts]))

def asks_to_inherit_previous(args: argparse.Namespace) -> bool:
    low = collect_intent_text(args)
    return any((x in low for x in INHERIT_MARKERS))

def allows_explicit_defaults(args: argparse.Namespace) -> bool:
    if args.run_with_defaults:
        return True
    low = collect_intent_text(args)
    # Chinese entries below are internal input aliases; never displayed.
    markers = ['\u9ed8\u8ba4\u5c31\u884c', '\u9ed8\u8ba4', '\u4f60\u5b9a', '\u63a8\u8350\u5c31\u884c', '\u90fd\u884c', '\u968f\u4fbf', 'auto', 'default', 'whatever', 'up to you']
    return any((x in low for x in markers))

def has_explicit_run_authorization(args: argparse.Namespace) -> bool:
    if getattr(args, 'confirm_run', False) or args.run_with_defaults:
        return True
    low = collect_intent_text(args)
    if any((x in low for x in RUN_AUTH_NEGATION_MARKERS)):
        return False
    return any((x in low for x in RUN_AUTH_MARKERS))


def inherit_previous_run(args: argparse.Namespace, model: str | None) -> bool:
    if not model or not asks_to_inherit_previous(args):
        return False
    state = dispatch_common.load_runtime_state().get('last_run', {})
    previous = state.get(str(model))
    if not dispatch_common.runtime_state_entry_is_valid(previous):
        return False
    inherited_any = False
    prompt_low = dispatch_common.normalized_text(args.prompt or '')
    if previous.get('prompt') and (not args.prompt or any((marker in prompt_low for marker in INHERIT_MARKERS))):
        args.prompt = previous.get('prompt')
        inherited_any = True
    if not (args.image or []) and previous.get('image'):
        args.image = list(previous.get('image') or [])
        inherited_any = True
    if not (args.audio or []) and previous.get('audio'):
        args.audio = list(previous.get('audio') or [])
        inherited_any = True
    if not (args.video or []) and previous.get('video'):
        args.video = list(previous.get('video') or [])
        inherited_any = True
    if not args.aspect_ratio and previous.get('aspect_ratio'):
        args.aspect_ratio = previous.get('aspect_ratio')
        inherited_any = True
    if not args.resolution and previous.get('resolution'):
        args.resolution = previous.get('resolution')
        inherited_any = True
    if not args.duration and previous.get('duration') is not None:
        args.duration = previous.get('duration')
        inherited_any = True
    if not args.seed and previous.get('seed') is not None:
        args.seed = previous.get('seed')
        inherited_any = True
    if previous.get('random_seed') and (not args.seed) and (not args.random_seed):
        args.random_seed = True
        inherited_any = True
    if not args.model_name and previous.get('model_name'):
        args.model_name = previous.get('model_name')
        inherited_any = True
    if not args.width and previous.get('width') is not None:
        args.width = previous.get('width')
        inherited_any = True
    if not args.height and previous.get('height') is not None:
        args.height = previous.get('height')
        inherited_any = True
    if not (args.param or []) and previous.get('param'):
        args.param = list(previous.get('param') or [])
        inherited_any = True
    return inherited_any

def determine_parameter_state(args: argparse.Namespace, model: str | None) -> str:
    if not model:
        return 'user_supplied_enough'
    if model in {'remote-search-flow', 'remote-video-search-flow', 'modelzoo-search-flow', 'modelzoo-video-search-flow'}:
        return 'user_supplied_enough'
    run_authorized = has_explicit_run_authorization(args)
    if asks_to_inherit_previous(args):
        inherited = inherit_previous_run(args, model)
        if not inherited:
            return 'missing_required'
        return 'ready_to_run' if run_authorized else 'awaiting_confirmation'
    if allows_explicit_defaults(args):
        return 'ready_to_run' if run_authorized else 'awaiting_confirmation'
    if has_explicit_tuning(args):
        return 'ready_to_run' if run_authorized else 'awaiting_confirmation'
    if model in COMPILER_RULES:
        return 'missing_required'
    return 'ready_to_run' if run_authorized else 'awaiting_confirmation'

def normalize_field_name(name: str) -> str:
    cleaned = re.sub('[^\\w\\u4e00-\\u9fff]+', '', str(name or ''))
    return cleaned.strip()

def parse_card_text(card_text: str) -> dict[str, str]:
    parsed = {}
    current_key = None
    for raw_line in (card_text or '').splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if '：' in line:
            (key, value) = line.split('：', 1)
        elif ':' in line:
            (key, value) = line.split(':', 1)
        else:
            if current_key and line and (not line.startswith(('1.', '2.', '3.', '4.', '5.', '6.', '7.'))):
                parsed[current_key] = (parsed.get(current_key, '') + '\n' + line).strip()
            continue
        key = normalize_field_name(key)
        value = value.strip()
        parsed[key] = value
        current_key = key
    return parsed

def is_defaultish(value: str | None) -> bool:
    low = dispatch_common.normalized_text(value)
    # Chinese entries below are internal input aliases; never displayed.
    return low in {'', '\u9ed8\u8ba4', '\u4f60\u5b9a', '\u63a8\u8350\u5c31\u884c', '\u90fd\u884c', '\u968f\u4fbf', 'auto', 'default', 'whatever', 'up to you'}

def coerce_size_to_width_height(value: str) -> tuple[int, int] | None:
    if 'x' in value.lower():
        (left, right) = value.lower().split('x', 1)
        if left.strip().isdigit() and right.strip().isdigit():
            return (int(left.strip()), int(right.strip()))
    return None

def compile_card_selection(model: str, card_text: str) -> dict:
    """Parse the card text the user filled in into structured parameters.

    Three-stage logic (the order matters, do not shuffle it):
      1) values the user typed explicitly into card fields   -- highest priority
      2) natural-language inference from "other notes"       -- applies only to fields the user left empty
      3) COMPILER_RULES.defaults fallback                    -- currently always empty; real defaults come from the remote
                                                                detail's field_value when the payload is built

    Defaults used to be applied first, so when the user wrote "landscape HD" in
    "other notes", aspect_ratio had already been filled with the default 1:1 and the
    notes inference condition `not compiled.get('aspect_ratio')` was always False, which
    made natural-language inference completely dead. That is why this order must stay
    after the reorder.

    Special seed guard: if the user/notes already chose "random" (random_seed=True),
    the last step must not push the fixed default seed back over that intent.
    """
    rule = COMPILER_RULES.get(model, {})
    parsed = parse_card_text(card_text)
    # The Chinese card labels below (prompt / notes) are internal input aliases; never displayed.
    compiled = {'prompt': parsed.get('prompt') or parsed.get('\u63d0\u793a\u8bcd') or '', 'images': [], 'audios': [], 'aspect_ratio': None, 'resolution': None, 'duration': None, 'seed': None, 'random_seed': False, 'model_name': None, 'width': None, 'height': None, 'params': [], 'notes': parsed.get('notes') or parsed.get('\u5176\u4ed6\u8865\u5145') or ''}

    # Chinese/English alias table for card field names, so a Chinese ratio line and an English
    # "aspect_ratio: 3:2" line are both recognized. The Chinese entries are internal input
    # aliases; never displayed.
    keys_to_map = {
        'aspect_ratio': ['aspect_ratio', 'aspect ratio', 'ratio', '\u6bd4\u4f8b', '\u89c6\u9891\u6bd4\u4f8b', '\u753b\u9762\u6bd4\u4f8b', '\u56fe\u7247\u6bd4\u4f8b'],
        'resolution': ['resolution', 'quality', '\u89c4\u683c', '\u6e05\u6670\u5ea6', '\u56fe\u7247\u8d28\u91cf', '\u56fe\u7247\u89c4\u683c', '\u753b\u9762\u89c4\u683c', '\u89c6\u9891\u5c3a\u5bf8', '\u56fe\u7247\u5c3a\u5bf8'],
        'duration': ['duration', 'length', '\u65f6\u957f', '\u89c6\u9891\u65f6\u957f'],
        'model_name': ['model_name', 'model', '\u6a21\u578b\u6863\u4f4d'],
        'seed': ['seed', 'randomness', '\u968f\u673a\u6027', '\u56fa\u5b9aseed'],
        'width': ['width', '\u5bbd\u5ea6'],
        'height': ['height', '\u9ad8\u5ea6'],
    }

    # 1) First parse the values the user typed into card fields (highest priority)
    for target, aliases in keys_to_map.items():
        for alias in aliases:
            val = parsed.get(normalize_field_name(alias))
            if val and not is_defaultish(val):
                if target == 'seed':
                    if '\u968f\u673a' in str(val) or str(val).lower() == 'random':  # internal input alias; never displayed
                        compiled['random_seed'] = True
                        compiled['seed'] = None
                    elif str(val).isdigit():
                        compiled['seed'] = int(val)
                elif target == 'resolution' and 'x' in str(val).lower():
                    wh = coerce_size_to_width_height(str(val))
                    if wh:
                        (compiled['width'], compiled['height']) = wh
                    else:
                        compiled[target] = val
                else:
                    compiled[target] = val
                break

    # 2) Then infer from natural language in "other notes" (only for fields the user left empty).
    #    The Chinese markers below are internal input aliases; never displayed.
    notes = dispatch_common.normalized_text(compiled.get('notes') or '')
    if notes:
        if ('\u6a2a' in notes or 'landscape' in notes) and (not compiled.get('aspect_ratio')) and (not compiled.get('width')):
            compiled['aspect_ratio'] = '16:9'
        if ('\u7ad6' in notes or 'portrait' in notes) and (not compiled.get('aspect_ratio')) and (not compiled.get('width')):
            compiled['aspect_ratio'] = '9:16'
        if ('\u65b9' in notes or 'square' in notes or '1:1' in notes) and (not compiled.get('aspect_ratio')) and (not compiled.get('width')):
            compiled['aspect_ratio'] = '1:1'
        if compiled.get('duration') is None:
            # internal input alias; never displayed
            if '10\u79d2' in notes or '10 \u79d2' in notes or '10s' in notes or '10 s' in notes:
                compiled['duration'] = 10
            elif '8\u79d2' in notes or '8 \u79d2' in notes or '8s' in notes or '8 s' in notes:
                compiled['duration'] = 8
            elif '5\u79d2' in notes or '5 \u79d2' in notes or '5s' in notes or '5 s' in notes:
                compiled['duration'] = 5
            elif '3\u79d2' in notes or '3 \u79d2' in notes or '3s' in notes or '3 s' in notes:
                compiled['duration'] = 3
        if ('\u968f\u673a' in notes or 'random' in notes) and compiled.get('seed') is None and (not compiled.get('random_seed')):
            compiled['random_seed'] = True
        if '\u9ad8\u6e05' in notes or 'high' in notes or 'large' in notes or 'hd' in notes:  # internal input alias; never displayed
            for (idx, item) in enumerate(list(compiled['params'])):
                if item.startswith('60:BizyAir_Sora_V2_T2V_API.size='):
                    compiled['params'][idx] = '60:BizyAir_Sora_V2_T2V_API.size=large'
            if model == 'nano-banana-pro-text' and (not compiled.get('resolution')):
                compiled['resolution'] = '2K'

    # 3) COMPILER_RULES.defaults is currently always empty (defaults now come from the remote
    #    schema); this logic is kept for future fields that really do need a local fallback.
    for (key, value) in (rule.get('defaults') or {}).items():
        if key == 'params':
            existing_param_keys = {p.split('=', 1)[0] for p in compiled['params'] if '=' in p}
            for (p_key, p_val) in value.items():
                if p_key not in existing_param_keys:
                    compiled['params'].append(f'{p_key}={p_val}')
        elif key == 'seed' and compiled.get('random_seed'):
            # The user or the inference already chose random → do not push a fixed seed back over it
            continue
        elif compiled.get(key) is None:
            compiled[key] = value

    return compiled

def apply_compiled_selection(args: argparse.Namespace, target: str) -> None:
    if not getattr(args, 'card_input', None):
        return
    compiled = compile_card_selection(target, args.card_input)
    if compiled.get('prompt') and (not args.prompt):
        args.prompt = compiled['prompt']
    if compiled.get('aspect_ratio') and (not args.aspect_ratio):
        args.aspect_ratio = str(compiled['aspect_ratio'])
    if compiled.get('resolution') and (not args.resolution):
        args.resolution = str(compiled['resolution'])
    if compiled.get('duration') and (not args.duration):
        args.duration = int(compiled['duration'])
    if compiled.get('seed') and (not args.seed):
        args.seed = int(compiled['seed'])
    if compiled.get('random_seed'):
        args.random_seed = True
    if compiled.get('model_name') and (not args.model_name):
        args.model_name = str(compiled['model_name'])
    if compiled.get('width') and (not args.width):
        args.width = int(compiled['width'])
    if compiled.get('height') and (not args.height):
        args.height = int(compiled['height'])
    if compiled.get('params'):
        args.param = (args.param or []) + list(compiled['params'])
