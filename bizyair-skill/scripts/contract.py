"""contract.py — input-field contract / node classification for BizyAir remote objects.

Why this exists: field names vary wildly across BizyAir apps/workflows (prompt / text /
CLIPTextEncode.text / 27:text / BizyAir_Seedream4.prompt, etc.), so a rule layer is
needed to sort field types for the info output and for the display annotations on the
confirmation card.

Note: the prompt-detection helpers (is_remote_prompt_input_node /
get_supported_remote_prompt_binding_keys, etc.) are now used only for the info output and
for confirmation-card display annotations; they are not part of the field mapping on the
actual execution path.
At run time the prompt is written by the --prompt / --param logic in
api.build_remote_run_payload.

Main API:
  - resolve_remote_input_contract(execution_target, webapp_detail, workflow): main entry,
    merges input_nodes from several data sources into one resolved_contract dict
  - is_negative_prompt_key: whether a field is a negative prompt
  - is_text_input_field: whether field_type is a text input type
  - classify_remote_media_slot: whether a field is a media upload slot
  - remote_contract_supports(contract, logical_name): checks during card rendering
    whether "the field is present in the contract"
"""
from __future__ import annotations
import json, re
from typing import Any

import api
import common
from common import (
    EXPLICIT_PROMPT_LABELS, KNOWN_PROMPT_FIELD_NAMES,
    KNOWN_PROMPT_NODE_MARKERS, PROMPT_EXCLUDE_EN, PROMPT_EXCLUDE_ZH,
    REMOTE_CONTRACT_SOURCE_PRIORITY, REMOTE_EXPOSED_CONTRACT_SOURCES,
    REMOTE_GENERIC_PROMPT_ALIASES, TEXT_INPUT_FIELD_TYPES,
    add_unique_key,
)

def is_negative_prompt_key(field_name: str | None, field_label: str | None=None) -> bool:
    raw = ' '.join([str(field_name or '').strip(), str(field_label or '').strip()])
    lowered = raw.lower()
    if 'negative' in lowered and 'prompt' in lowered:
        return True
    # Chinese negative-prompt detection
    # internal input alias; never displayed
    chinese_negatives = ('\u53cd\u5411\u63d0\u793a\u8bcd', '\u8d1f\u5411\u63d0\u793a\u8bcd', '\u8d1f\u9762\u63d0\u793a\u8bcd', '\u53cd\u5411', '\u8d1f\u5411')
    return any(marker in raw for marker in chinese_negatives)

def normalize_prompt_label(value: Any) -> str:
    text = str(value or '').strip()
    text = re.sub('[\\s:：_\\-()\\[\\]{}]+', '', text)
    return text.lower()

def has_prompt_excluded_marker(*parts: Any) -> bool:
    raw_parts = [str(part or '').strip() for part in parts if str(part or '').strip()]
    if not raw_parts:
        return False
    joined = ' '.join(raw_parts)
    lowered = joined.lower()
    if any((marker in lowered for marker in PROMPT_EXCLUDE_EN)):
        return True
    return any((marker in joined for marker in PROMPT_EXCLUDE_ZH))

def is_explicit_prompt_label(label: Any) -> bool:
    normalized = normalize_prompt_label(label)
    return normalized in EXPLICIT_PROMPT_LABELS

def is_known_prompt_node_type(node_type: Any) -> bool:
    lowered = str(node_type or '').strip().lower()
    if not lowered or has_prompt_excluded_marker(lowered) or is_negative_prompt_key(lowered):
        return False
    return any((marker in lowered for marker in KNOWN_PROMPT_NODE_MARKERS)) or lowered == 'prompt' or lowered.endswith('.prompt')

def is_known_prompt_binding_key(value: Any) -> bool:
    text = str(value or '').strip()
    lowered = text.lower()
    if not lowered or has_prompt_excluded_marker(text) or is_negative_prompt_key(lowered):
        return False
    if lowered in KNOWN_PROMPT_FIELD_NAMES:
        return True
    if any((token in lowered for token in ['.prompt', ':prompt', '.user_prompt', ':user_prompt', '.positive_prompt', ':positive_prompt'])):
        return True
    if 'cliptextencode.text' in lowered or 'cliptextencode:text' in lowered:
        return True
    if 'primitivestringmultiline.value' in lowered or 'primitivestringmultiline:value' in lowered:
        return True
    if 'primitivestring.value' in lowered or 'primitivestring:value' in lowered:
        return True
    if lowered in {'text', 'value'}:
        return False
    if (lowered.endswith('.text') or lowered.endswith(':text')) and is_known_prompt_node_type(lowered):
        return True
    if (lowered.endswith('.value') or lowered.endswith(':value')) and any((marker in lowered for marker in {'primitivestringmultiline', 'primitivestring'})):
        return True
    return False

def split_remote_binding_key(value: Any) -> tuple[str, str]:
    text = str(value or '').strip()
    if not text:
        return ('', '')
    if ':' not in text:
        return ('', text)
    (node_id, suffix) = text.split(':', 1)
    return (str(node_id).strip(), str(suffix).strip())

def collect_remote_binding_aliases(*values: Any) -> set[str]:
    aliases: set[str] = set()
    for raw in values:
        text = str(raw or '').strip()
        if not text:
            continue
        variants = [text]
        if ':' in text:
            (_, suffix) = split_remote_binding_key(text)
            if suffix:
                variants.append(suffix)
        for variant in variants:
            normalized = normalize_prompt_label(variant)
            if normalized:
                aliases.add(normalized)
            dot_parts = [part for part in re.split('[.:]', variant) if str(part).strip()]
            if dot_parts:
                aliases.add(normalize_prompt_label(dot_parts[-1]))
                aliases.add(normalize_prompt_label('.'.join(dot_parts[-2:])))
                for part in dot_parts:
                    aliases.add(normalize_prompt_label(part))
    return {alias for alias in aliases if alias}

REMOTE_LOGICAL_FIELD_ALIASES = {
    'seed': {'seed', 'randomseed', 'fixedseed'},
    'steps': {'steps', 'step', 'samplingsteps', 'samplingstep', 'numsteps'},
    'duration': {'duration', 'seconds', 'second', 'videoduration'},
    'aspect_ratio': {'aspectratio', 'ratio'},
    'resolution': {'resolution', 'maxresolution', 'size'},
    'width': {'width', 'imagewidth', 'outputwidth', 'targetwidth'},
    'height': {'height', 'imageheight', 'outputheight', 'targetheight'},
    'model_name': {'model', 'modelname', 'checkpoint'},
}

REMOTE_LOGICAL_FIELD_TEXT_MARKERS = {
    # internal input alias; never displayed
    'seed': {'\u968f\u673a'},
    'steps': {'\u6b65\u6570', '\u91c7\u6837\u6b65\u6570'},
    'duration': {'\u65f6\u957f'},
    'aspect_ratio': {'\u6bd4\u4f8b', '\u753b\u5e45'},
    'resolution': {'\u5206\u8fa8\u7387', '\u6e05\u6670\u5ea6', '\u89c4\u683c'},
    'width': {'\u5bbd\u5ea6'},
    'height': {'\u9ad8\u5ea6'},
    'model_name': {'\u6a21\u578b\u6863\u4f4d', '\u6863\u4f4d'},
}

def is_remote_logical_field(logical_name: str, *values: Any) -> bool:
    logical = str(logical_name or '').strip()
    if not logical:
        return False
    raw_values = [str(value or '').strip() for value in values if str(value or '').strip()]
    aliases = collect_remote_binding_aliases(*raw_values)
    if aliases & (REMOTE_LOGICAL_FIELD_ALIASES.get(logical) or set()):
        return True
    markers = REMOTE_LOGICAL_FIELD_TEXT_MARKERS.get(logical) or set()
    return any((marker in value for marker in markers for value in raw_values))

def is_remote_seed_field(*values: Any) -> bool:
    return is_remote_logical_field('seed', *values)

def is_text_input_field(field_type: str | None) -> bool:
    return str(field_type or '').strip().lower() in TEXT_INPUT_FIELD_TYPES

def is_remote_prompt_input_node(node: dict[str, Any]) -> bool:
    field_type = str(node.get('field_type') or '').strip().lower()
    field_name = str(node.get('field_name') or '').strip()
    field_label = str(node.get('field_label') or '').strip()
    variable_name = str(node.get('variable_name') or '').strip()
    node_type = str(node.get('node_type') or '').strip()
    if not is_text_input_field(field_type):
        return False
    if has_prompt_excluded_marker(field_name, field_label, variable_name, node_type):
        return False
    if is_negative_prompt_key(field_name, field_label):
        return False
    if is_explicit_prompt_label(field_label):
        return True
    # Composite labels also count: a label such as "image prompt(single character)" glues
    # the purpose word and the prompt word together, so it is not equal to any entry in
    # EXPLICIT_PROMPT_LABELS as a whole, but its root is a prompt.
    # Applies only to text fields (already filtered above), and negative prompts and
    # annotation slots such as title/caption are already excluded.
    normalized_label = normalize_prompt_label(field_label)
    # internal input alias; never displayed (the Chinese token below matches prompt labels)
    if normalized_label and any((token in normalized_label for token in ('prompt', '\u63d0\u793a\u8bcd'))) and (not is_negative_prompt_key(f'{field_name} {variable_name}', field_label)):
        return True
    if is_known_prompt_binding_key(field_name) or is_known_prompt_binding_key(variable_name):
        return True
    if normalize_prompt_label(field_name) == 'text' and is_known_prompt_node_type(node_type):
        return True
    return False

def infer_remote_contract_logical_name(*, field_name: str='', field_label: str='', variable_name: str='', node_type: str='', media_type: str | None=None, prompt_like: bool=False, negative_prompt_like: bool=False) -> str:
    if negative_prompt_like:
        return 'negative_prompt'
    if prompt_like:
        return 'prompt'
    if media_type:
        return media_type
    (_, variable_suffix) = split_remote_binding_key(variable_name)
    strict_values = (field_name, field_label, variable_suffix)
    joined = ' '.join([field_name, field_label, variable_suffix, node_type]).lower()
    if 'batch_size' in joined or 'batch size' in joined:
        return 'batch_size'
    if is_remote_logical_field('aspect_ratio', *strict_values):
        return 'aspect_ratio'
    if is_remote_logical_field('resolution', *strict_values):
        return 'resolution'
    if is_remote_logical_field('width', *strict_values):
        return 'width'
    if is_remote_logical_field('height', *strict_values):
        return 'height'
    if is_remote_logical_field('duration', *strict_values):
        return 'duration'
    if is_remote_logical_field('seed', *strict_values):
        return 'seed'
    if is_remote_logical_field('steps', *strict_values):
        return 'steps'
    if is_remote_logical_field('model_name', *strict_values):
        return 'model_name'
    if 'cfg_scale' in joined or ('cfg' in joined and 'scale' in joined):
        return 'cfg_scale'
    if 'strength' in joined or 'denoise' in joined:
        return 'strength'
    if 'sound' in joined or 'audio' in joined:
        return 'sound'
    raw = str(field_name or variable_name or field_label or node_type or '').strip()
    if not raw:
        return 'unknown'
    normalized = re.sub('[^A-Za-z0-9_]+', '_', raw).strip('_').lower()
    return normalized or 'unknown'

def normalize_remote_field_options(field_options: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    metadata_keys = {'max', 'min', 'precision', 'step', 'step2', 'hideonzoom', 'minnodesize'}
    if isinstance(field_options, str):
        text = field_options.strip()
        if text.startswith('{') or text.startswith('['):
            try:
                field_options = json.loads(text)
            except Exception:
                return normalized
        else:
            return normalized
    if isinstance(field_options, dict):
        if set((str(key).strip().lower() for key in field_options.keys())).issubset(metadata_keys):
            return normalized
        iterable = field_options.items()
    elif isinstance(field_options, list):
        iterable = enumerate(field_options)
    else:
        return normalized
    for (raw_key, raw_value) in iterable:
        if isinstance(raw_value, list):
            for item in raw_value:
                normalized.append({'value': item, 'label': str(item)})
            continue
        if isinstance(raw_value, dict):
            value = raw_value.get('value', raw_value.get('id', raw_key))
            label = raw_value.get('label', raw_value.get('name', value))
        else:
            value = raw_value
            label = raw_value
        normalized.append({'value': value, 'label': str(label)})
    return normalized

def build_remote_contract_field_from_node(node: dict[str, Any], *, source: str, priority: int) -> dict[str, Any]:
    field_name = str(node.get('field_name') or '').strip()
    field_label = str(node.get('field_label') or '').strip()
    variable_name = str(node.get('variable_name') or '').strip()
    node_type = str(node.get('node_type') or '').strip()
    field_type = str(node.get('field_type') or '').strip().lower()
    media_type = classify_remote_media_slot(node)
    prompt_like = is_remote_prompt_input_node(node)
    negative_prompt_like = is_negative_prompt_key(field_name, field_label)
    if not prompt_like and (not negative_prompt_like) and is_text_input_field(field_type) and (not has_prompt_excluded_marker(field_name, field_label, variable_name, node_type)):
        normalized_hint = normalize_prompt_label(field_name or field_label)
        if normalized_hint in {'text', 'prompt'} or is_known_prompt_binding_key(variable_name):
            prompt_like = True
    logical_name = infer_remote_contract_logical_name(field_name=field_name, field_label=field_label, variable_name=variable_name, node_type=node_type, media_type=media_type, prompt_like=prompt_like, negative_prompt_like=negative_prompt_like)
    display_name = humanize_remote_prefill_label(node)
    return {'source': source, 'priority': priority, 'support_level': 'supported', 'logical_name': logical_name, 'display_name': display_name, 'field_name': field_name, 'field_label': field_label, 'variable_name': variable_name, 'node_type': node_type, 'field_type': field_type, 'field_value': node.get('field_value'), 'field_options': normalize_remote_field_options(node.get('field_options')), 'user_input': field_type != 'hidden' or media_type is not None or prompt_like or negative_prompt_like, 'system_inject': negative_prompt_like, 'media_type': media_type, 'prompt_like': prompt_like, 'negative_prompt_like': negative_prompt_like, 'execution_binding': {'write_key': variable_name or None}, 'raw_node': node}

def build_remote_contract_field_from_hint(logical_name: str, binding_key: str, *, source: str, priority: int, support_level: str='hint_only', user_input: bool=True) -> dict[str, Any]:
    return {'source': source, 'priority': priority, 'support_level': support_level, 'logical_name': logical_name, 'display_name': logical_name, 'field_name': logical_name, 'field_label': logical_name, 'variable_name': str(binding_key), 'node_type': 'workflow_hint', 'field_type': 'hint', 'field_value': None, 'field_options': [], 'user_input': user_input, 'system_inject': logical_name == 'negative_prompt', 'media_type': logical_name if logical_name in {'image', 'audio', 'video'} else None, 'prompt_like': logical_name == 'prompt', 'negative_prompt_like': logical_name == 'negative_prompt', 'execution_binding': {'write_key': str(binding_key)}, 'raw_node': None}

def empty_remote_contract_bindings() -> dict[str, Any]:
    return {'prompt_keys': [], 'supported_prompt_keys': [], 'hint_prompt_keys': [], 'negative_prompt_keys': [], 'logical': {}, 'media_slots': {'image': [], 'audio': [], 'video': []}}

def collect_remote_contract_field_aliases(field: dict[str, Any]) -> set[str]:
    field_name = str(field.get('field_name') or '').strip()
    field_label = str(field.get('field_label') or '').strip()
    variable_name = str(field.get('variable_name') or '').strip()
    node_type = str(field.get('node_type') or '').strip()
    aliases = collect_remote_binding_aliases(field_name, field_label, variable_name, node_type)
    (_, variable_suffix) = split_remote_binding_key(variable_name)
    if variable_suffix:
        aliases.update(collect_remote_binding_aliases(variable_suffix))
    if node_type and field_name:
        aliases.update(collect_remote_binding_aliases(f'{node_type}.{field_name}'))
    if node_type and field_label:
        aliases.update(collect_remote_binding_aliases(f'{node_type}.{field_label}'))
    return aliases

def choose_safe_prompt_upgrade_field(contract: dict[str, Any], hint_key: str) -> dict[str, Any] | None:
    (hint_node_id, hint_suffix) = split_remote_binding_key(hint_key)
    if not hint_node_id or not hint_suffix:
        return None
    hint_aliases = collect_remote_binding_aliases(hint_key, hint_suffix)
    exact_candidates: list[dict[str, Any]] = []
    structural_candidates: list[dict[str, Any]] = []
    for field in contract.get('fields') or []:
        if not is_real_remote_text_input_contract_field(field):
            continue
        variable_name = str(field.get('variable_name') or '').strip()
        (field_node_id, _) = split_remote_binding_key(variable_name)
        if field_node_id != hint_node_id:
            continue
        field_aliases = collect_remote_contract_field_aliases(field)
        if hint_aliases & field_aliases:
            exact_candidates.append(field)
            continue
        if is_structurally_prompt_like_remote_text_field(field):
            structural_candidates.append(field)
    deduped_exact = {str(item.get('variable_name') or '').strip(): item for item in exact_candidates}
    if len(deduped_exact) == 1:
        return next(iter(deduped_exact.values()))
    if len(deduped_exact) > 1:
        return None
    deduped_structural = {str(item.get('variable_name') or '').strip(): item for item in structural_candidates}
    if len(deduped_structural) == 1:
        return next(iter(deduped_structural.values()))
    return None

def promote_supported_prompt_fields_from_hints(contract: dict[str, Any], hint_keys: list[str] | None) -> list[dict[str, Any]]:
    promotions: list[dict[str, Any]] = []
    seen_promotions: set[tuple[str, str]] = set()
    for hint_key in hint_keys or []:
        hint_text = str(hint_key or '').strip()
        if not hint_text:
            continue
        candidate = choose_safe_prompt_upgrade_field(contract, hint_text)
        if not candidate:
            continue
        write_key = str((candidate.get('execution_binding') or {}).get('write_key') or candidate.get('variable_name') or '').strip()
        if not write_key:
            continue
        already_supported_prompt = bool(candidate.get('prompt_like')) and str(candidate.get('logical_name') or '').strip() == 'prompt' and (str(candidate.get('support_level') or 'supported').strip().lower() == 'supported')
        if already_supported_prompt:
            continue
        dedupe = (hint_text, write_key)
        if dedupe in seen_promotions:
            continue
        seen_promotions.add(dedupe)
        candidate['prompt_like'] = True
        candidate['logical_name'] = 'prompt'
        candidate['support_level'] = 'supported'
        candidate['support_reason'] = 'workflow_hint_promoted'
        hint_binding_keys = candidate.setdefault('hint_binding_keys', [])
        add_unique_key(hint_binding_keys, hint_text)
        promotions.append({'hint_key': hint_text, 'promoted_to': write_key, 'source': str(candidate.get('source') or ''), 'reason': 'same_node_exposed_text_input'})
    return promotions

def rebuild_remote_contract_bindings(contract: dict[str, Any]) -> None:
    rebuilt = empty_remote_contract_bindings()
    contract['bindings'] = rebuilt
    for field in contract.get('fields') or []:
        binding_key = str((field.get('execution_binding') or {}).get('write_key') or '').strip()
        logical_name = str(field.get('logical_name') or '').strip()
        media_type = field.get('media_type')
        support_level = str(field.get('support_level') or 'supported').strip().lower()
        if binding_key and field.get('prompt_like'):
            if support_level == 'supported':
                add_unique_key(rebuilt['prompt_keys'], binding_key)
                add_unique_key(rebuilt['supported_prompt_keys'], binding_key)
            else:
                add_unique_key(rebuilt['hint_prompt_keys'], binding_key)
        if binding_key and field.get('negative_prompt_like'):
            add_unique_key(rebuilt['negative_prompt_keys'], binding_key)
        if binding_key:
            add_to_logical = bool(logical_name)
            if field.get('prompt_like') and logical_name == 'prompt' and (support_level != 'supported'):
                add_to_logical = False
            if field.get('negative_prompt_like') and logical_name == 'negative_prompt' and (support_level != 'supported'):
                add_to_logical = False
            if add_to_logical:
                rebuilt['logical'].setdefault(logical_name, [])
                add_unique_key(rebuilt['logical'][logical_name], binding_key)
            if media_type in {'image', 'audio', 'video'}:
                current_slots = rebuilt['media_slots'][str(media_type)]
                node_prefix = binding_key.split(':', 1)[0]
                if not any((str(existing).split(':', 1)[0] == node_prefix for existing in current_slots)):
                    add_unique_key(current_slots, binding_key)

def add_remote_contract_field(contract: dict[str, Any], field: dict[str, Any]) -> None:
    fields = contract.setdefault('fields', [])
    bindings = contract.setdefault('bindings', empty_remote_contract_bindings())
    seen_keys = contract.setdefault('_seen_binding_keys', set())
    binding_key = str((field.get('execution_binding') or {}).get('write_key') or '').strip()
    source = str(field.get('source') or '')
    dedupe_key = binding_key or f"{source}:{field.get('logical_name')}:{field.get('field_name')}:{field.get('field_label')}"
    if dedupe_key in seen_keys:
        return
    seen_keys.add(dedupe_key)
    fields.append(field)
    logical_name = str(field.get('logical_name') or '').strip()
    media_type = field.get('media_type')
    support_level = str(field.get('support_level') or 'supported').strip().lower()
    if binding_key and media_type in {'image', 'audio', 'video'}:
        current_slots = bindings['media_slots'][str(media_type)]
        node_prefix = binding_key.split(':', 1)[0]
        if any((str(existing).split(':', 1)[0] == node_prefix for existing in current_slots)):
            return
    if binding_key:
        if field.get('prompt_like'):
            if support_level == 'supported':
                add_unique_key(bindings['prompt_keys'], binding_key)
                add_unique_key(bindings['supported_prompt_keys'], binding_key)
            else:
                add_unique_key(bindings['hint_prompt_keys'], binding_key)
        if field.get('negative_prompt_like'):
            add_unique_key(bindings['negative_prompt_keys'], binding_key)
        add_to_logical = bool(logical_name)
        if field.get('prompt_like') and logical_name == 'prompt' and (support_level != 'supported'):
            add_to_logical = False
        if field.get('negative_prompt_like') and logical_name == 'negative_prompt' and (support_level != 'supported'):
            add_to_logical = False
        if add_to_logical:
            bindings['logical'].setdefault(logical_name, [])
            add_unique_key(bindings['logical'][logical_name], binding_key)
        if media_type in {'image', 'audio', 'video'}:
            add_unique_key(bindings['media_slots'][str(media_type)], binding_key)

def get_supported_remote_prompt_binding_keys(contract: dict[str, Any] | None) -> list[str]:
    bindings = (contract or {}).get('bindings') or {}
    supported = bindings.get('supported_prompt_keys')
    if supported is not None:
        return list(supported or [])
    return list(bindings.get('prompt_keys') or [])

def build_remote_required_inputs_from_contract(contract: dict[str, Any]) -> list[dict[str, Any]]:
    required_inputs: list[dict[str, Any]] = []
    bindings = contract.get('bindings') or {}
    prompt_keys = get_supported_remote_prompt_binding_keys(contract)
    if prompt_keys:
        required_inputs.append({'type': 'prompt', 'keys': list(prompt_keys), 'required': True})
    media_slots = bindings.get('media_slots') or {}
    for media_type in ['image', 'audio', 'video']:
        slots = media_slots.get(media_type) or []
        if slots:
            required_inputs.append({'type': media_type, 'count': len(slots), 'required': True})
    return required_inputs

def get_remote_contract_binding_keys(contract_data: dict[str, Any], logical_name: str) -> list[str]:
    if str(logical_name) == 'prompt':
        return get_supported_remote_prompt_binding_keys(contract_data)
    logical = ((contract_data or {}).get('bindings') or {}).get('logical') or {}
    return list(logical.get(str(logical_name), []) or [])

def remote_contract_supports(contract: dict[str, Any], logical_name: str) -> bool:
    return bool(get_remote_contract_binding_keys(contract, logical_name))

def contract_field_by_binding_key(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for field in contract.get('fields') or []:
        binding = str((field.get('execution_binding') or {}).get('write_key') or '').strip()
        if not binding:
            continue
        mapping.setdefault(binding, field)
    return mapping

def resolve_remote_input_contract(*, execution_target: dict[str, Any] | None=None, webapp_detail: dict[str, Any] | None=None, workflow: dict[str, Any] | None=None) -> dict[str, Any]:
    execution = execution_target or {}
    runtime_webapp_detail = execution.get('webapp_detail') or {}
    runtime_webapp_detail_data = api.extract_result_data(runtime_webapp_detail)
    webapp_detail_data = api.extract_result_data(webapp_detail)
    workflow_data = api.extract_result_data(workflow)
    input_hints = collect_required_input_hints(workflow_data)
    contract: dict[str, Any] = {'source_priority': list(REMOTE_CONTRACT_SOURCE_PRIORITY), 'fields': [], 'bindings': empty_remote_contract_bindings(), 'required_inputs': [], 'candidate_fields': [], 'hint_promotions': [], 'source_status': {'execution_target.webapp_detail.input_nodes': {'available': bool(runtime_webapp_detail_data.get('input_nodes')), 'count': len(runtime_webapp_detail_data.get('input_nodes', []) or [])}, 'webapp_detail.input_nodes': {'available': bool(webapp_detail_data.get('input_nodes')), 'count': len(webapp_detail_data.get('input_nodes', []) or [])}, 'workflow_hints': {'available': bool(input_hints.get('prompt_keys') or input_hints.get('candidate_fields')), 'count': len(input_hints.get('prompt_keys', []) or []) + len(input_hints.get('candidate_fields', []) or [])}}}
    for (priority, (source_name, nodes)) in enumerate([('execution_target.webapp_detail.input_nodes', runtime_webapp_detail_data.get('input_nodes', []) or []), ('webapp_detail.input_nodes', webapp_detail_data.get('input_nodes', []) or [])], start=1):
        for node in nodes:
            add_remote_contract_field(contract, build_remote_contract_field_from_node(node, source=source_name, priority=priority))
    workflow_priority = len(REMOTE_CONTRACT_SOURCE_PRIORITY)
    contract['hint_promotions'] = promote_supported_prompt_fields_from_hints(contract, list(input_hints.get('prompt_keys') or []))
    for key in input_hints.get('prompt_keys', []) or []:
        add_remote_contract_field(contract, build_remote_contract_field_from_hint('prompt', str(key), source='workflow_hints', priority=workflow_priority))
    for (media_type, keys) in [('image', list(input_hints.get('image_keys') or [])), ('audio', list(input_hints.get('audio_keys') or [])), ('video', list(input_hints.get('video_keys') or []))]:
        for (index, binding_key) in enumerate(keys):
            add_remote_contract_field(contract, build_remote_contract_field_from_hint(media_type, str(binding_key), source='workflow_hints', priority=workflow_priority))
    rebuild_remote_contract_bindings(contract)
    contract['source_status']['workflow_hint_safe_promotions'] = {'available': bool(contract.get('hint_promotions')), 'count': len(contract.get('hint_promotions') or [])}
    contract['required_inputs'] = build_remote_required_inputs_from_contract(contract)
    contract['candidate_fields'] = list(input_hints.get('candidate_fields') or [])
    contract['binding_by_logical_name'] = {key: list(value) for (key, value) in (contract.get('bindings', {}).get('logical') or {}).items()}
    contract.pop('_seen_binding_keys', None)
    return contract

def find_prompt_keys_in_workflow(workflow_data: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    if not isinstance(workflow_data, dict):
        return keys
    graph = workflow_data.get('graph') if isinstance(workflow_data.get('graph'), dict) else workflow_data
    nodes = graph.get('nodes', []) if isinstance(graph, dict) else []
    for node in nodes:
        node_id = node.get('id')
        node_type_name = str(node.get('type') or '')
        node_type = str(node.get('type') or '').lower()
        inputs = node.get('inputs') or []
        widget_values = node.get('widgets_values') or []
        if not isinstance(node_id, (int, str)):
            continue
        for (idx, inp) in enumerate(inputs):
            input_name = str((inp or {}).get('name') or '').strip()
            input_label = str((inp or {}).get('label') or '').strip()
            name = str(input_name or input_label).lower()
            if has_prompt_excluded_marker(node_type_name, input_name, input_label) or is_negative_prompt_key(name):
                continue
            explicit_label = is_explicit_prompt_label(input_label) or is_explicit_prompt_label(input_name)
            known_binding_name = is_known_prompt_binding_key(input_name) or is_known_prompt_binding_key(input_label)
            if not explicit_label and (not known_binding_name) and (not (normalize_prompt_label(input_name) == 'text' and is_known_prompt_node_type(node_type_name))):
                continue
            if idx < len(widget_values):
                value = widget_values[idx]
                if should_strip_prompt_default(value):
                    keys.append(f'{node_id}:{input_name or input_label}')
                    continue
            keys.append(f'{node_id}:{input_name or input_label}')
        if is_known_prompt_node_type(node_type_name):
            has_string_widget = any((isinstance(value, str) and value.strip() for value in widget_values))
            if not has_string_widget:
                continue
            if 'textencode' in node_type or node_type.endswith('text') or '.text' in node_type:
                keys.append(f'{node_id}:{node_type_name}.text')
                keys.append(f'{node_id}:text')
            elif 'primitivestringmultiline' in node_type or 'primitivestring' in node_type:
                keys.append(f'{node_id}:{node_type_name}.value')
                keys.append(f'{node_id}:value')
            else:
                keys.append(f'{node_id}:{node_type_name}.prompt')
                keys.append(f'{node_id}:prompt')
    deduped: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            deduped.append(key)
    return deduped

def find_media_binding_keys_in_workflow(workflow_data: dict[str, Any]) -> dict[str, list[str]]:
    result = {'image': [], 'audio': [], 'video': []}
    if not isinstance(workflow_data, dict):
        return result
    graph = workflow_data.get('graph') if isinstance(workflow_data.get('graph'), dict) else workflow_data
    nodes = graph.get('nodes', []) if isinstance(graph, dict) else []
    for node in nodes:
        node_id = node.get('id')
        node_type = str(node.get('type') or '').lower()
        if not isinstance(node_id, (int, str)):
            continue
        if 'loadimage' in node_type:
            add_unique_key(result['image'], f'{node_id}:image')
        if 'audio' in node_type and ('load' in node_type or 'input' in node_type):
            add_unique_key(result['audio'], f'{node_id}:audio')
        if 'video' in node_type and ('load' in node_type or 'input' in node_type):
            add_unique_key(result['video'], f'{node_id}:video')
    return result

def collect_required_input_hints(workflow_data: dict[str, Any]) -> dict[str, Any]:
    media_binding_keys = find_media_binding_keys_in_workflow(workflow_data)
    result = {'prompt_keys': find_prompt_keys_in_workflow(workflow_data), 'image_slots': len(media_binding_keys['image']), 'audio_slots': len(media_binding_keys['audio']), 'video_slots': len(media_binding_keys['video']), 'image_keys': list(media_binding_keys['image']), 'audio_keys': list(media_binding_keys['audio']), 'video_keys': list(media_binding_keys['video']), 'candidate_fields': []}
    if not isinstance(workflow_data, dict):
        return result
    graph = workflow_data.get('graph') if isinstance(workflow_data.get('graph'), dict) else workflow_data
    nodes = graph.get('nodes', []) if isinstance(graph, dict) else []
    candidate_fields: list[str] = []
    for node in nodes:
        node_type = str(node.get('type') or '')
        lowered = node_type.lower()
        node_id = node.get('id')
        for inp in node.get('inputs') or []:
            name = str((inp or {}).get('name') or (inp or {}).get('label') or '').strip()
            low = name.lower()
            if not name or low in {'prompt', 'text'} or 'negative' in low:
                continue
            if any((k in low for k in ['seed', 'step', 'cfg', 'duration', 'ratio', 'size', 'width', 'height', 'image', 'audio', 'video', 'model'])):
                candidate_fields.append(f'{node_id}:{name}')
    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidate_fields:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    result['candidate_fields'] = deduped[:20]
    return result

def should_strip_prompt_default(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = common.normalized_text(value)
    if not text:
        return False
    # internal input alias; never displayed
    suspicious_markers = ['example', 'default', 'preset', 'sample', 'template', '\u793a\u4f8b', '\u9ed8\u8ba4', '\u9884\u8bbe', '\u6837\u4f8b', '\u6a21\u677f']
    obvious_prompt_markers = ['masterpiece', 'best quality', 'ultra detailed', 'cinematic', 'poster', 'girl', 'boy', '1girl', '1boy']
    return any((x in text for x in suspicious_markers)) or any((x in text for x in obvious_prompt_markers))

def is_real_remote_text_input_contract_field(field: dict[str, Any]) -> bool:
    source = str(field.get('source') or '').strip()
    field_type = str(field.get('field_type') or '').strip().lower()
    field_name = str(field.get('field_name') or '').strip()
    field_label = str(field.get('field_label') or '').strip()
    variable_name = str(field.get('variable_name') or '').strip()
    node_type = str(field.get('node_type') or '').strip()
    if source not in REMOTE_EXPOSED_CONTRACT_SOURCES:
        return False
    if not is_text_input_field(field_type):
        return False
    if field_type == 'hidden':
        return False
    if not bool(field.get('user_input', True)):
        return False
    if is_negative_prompt_key(field_name, field_label):
        return False
    if has_prompt_excluded_marker(field_name, field_label, variable_name, node_type):
        return False
    return True

def is_structurally_prompt_like_remote_text_field(field: dict[str, Any]) -> bool:
    aliases = collect_remote_contract_field_aliases(field)
    if aliases & REMOTE_GENERIC_PROMPT_ALIASES:
        return True
    node_type = str(field.get('node_type') or '').strip()
    return is_known_prompt_node_type(node_type)


# ---- Field-detection helpers moved in from remote.py ----

def _split_machine_words(*parts: str) -> list[str]:
    raw = ' '.join((str(part or '') for part in parts))
    raw = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', raw)
    tokens = re.split(r'[^A-Za-z0-9]+', raw.lower())
    return [token for token in tokens if token]

def _contains_cjk(text: str) -> bool:
    # internal input alias; never displayed (Han code-point range used for matching only)
    return bool(re.search(r'[\u4e00-\u9fff]', text or ''))



def classify_remote_media_slot(node: dict[str, Any]) -> str | None:
    variable_name = str(node.get('variable_name') or '').lower()
    field_name = str(node.get('field_name') or '').lower()
    field_label = str(node.get('field_label') or '').lower()
    node_type = str(node.get('node_type') or '').lower()
    field_type = str(node.get('field_type') or '').lower()
    field_value = node.get('field_value')
    load_like_node = any((token in node_type for token in {'load', 'input'}))
    # Toggle / numeric / enum fields can never be upload slots, so exclude them outright.
    # Otherwise boolean / int / enum switches whose labels carry audio/image/video
    # keywords ("generate_audio", "image_count", "video_speed") are wrongly classified as
    # slots that need an uploaded file.
    non_media_field_types = {'boolean', 'bool', 'toggle', 'switch', 'checkbox', 'combo', 'select', 'enum', 'choice', 'option', 'int', 'integer', 'float', 'number', 'slider', 'dropdown'}
    if field_type in non_media_field_types:
        return None
    if isinstance(field_value, bool):
        return None
    # Text fields can never be upload slots: for a prompt field such as
    # "image prompt(single character)", having "image" in the label only describes the
    # purpose, it does not ask the user to upload an image. Matching label substrings alone
    # read the only prompt input of such text-to-image apps as an image slot, which made
    # prefill show "1 to provide" and made run blocked by REMOTE_APP_RUN_REQUIRES_USER_MEDIA.
    # Exception: the field name is explicitly image/images, or the node itself is a load/input
    # upload node.
    if field_type in TEXT_INPUT_FIELD_TYPES and field_name not in {'image', 'images'} and (not load_like_node):
        return None
    if field_name in {'image', 'images'} or 'image' in field_label or 'loadimage' in node_type or ('image' in variable_name and field_type == 'hidden') or ('image' in node_type and load_like_node and (field_type == 'hidden')):
        return 'image'
    if field_name in {'audio', 'audios'} or 'audio' in field_label or ('audio' in node_type and load_like_node and (field_type == 'hidden')) or ('audio' in variable_name and field_type == 'hidden'):
        return 'audio'
    if field_name in {'video', 'videos'} or 'video' in field_label or ('video' in node_type and load_like_node and (field_type == 'hidden')) or ('video' in variable_name and field_type == 'hidden'):
        return 'video'
    return None


def humanize_remote_prefill_label(node: dict[str, Any]) -> str:
    field_label = str(node.get('field_label') or '').strip()
    field_name = str(node.get('field_name') or '').strip()
    variable_name = str(node.get('variable_name') or '').strip()
    field_type = str(node.get('field_type') or '').strip().lower()
    joined = ' '.join([field_label.lower(), field_name.lower(), variable_name.lower()])
    tokens = _split_machine_words(field_label, field_name, variable_name)
    if 'negative' in tokens and 'prompt' in tokens:
        return 'Negative prompt (what should not appear)'
    # Chinese negative-prompt detection
    # internal input alias; never displayed
    raw_text = ' '.join([field_label, field_name])
    if any(marker in raw_text for marker in ('\u53cd\u5411\u63d0\u793a\u8bcd', '\u8d1f\u5411\u63d0\u793a\u8bcd', '\u8d1f\u9762\u63d0\u793a\u8bcd', '\u53cd\u5411', '\u8d1f\u5411')):
        return 'Negative prompt (what should not appear)'
    # Chinese tokens in the matchers below are internal input aliases; never displayed
    if any((token in joined for token in ['prompt', 'text', '\u63cf\u8ff0', '\u6587\u672c', '\u5185\u5bb9'])):
        return 'Text instruction (tells the model what to do this round)'
    if 'batch_size' in joined:
        return 'Generation count (how many per run)'
    if any((token in joined for token in ['aspect_ratio', 'aspect-ratio', ' ratio', '\u6bd4\u4f8b', '\u753b\u5e45'])):
        return 'Aspect ratio / frame (orientation and composition ratio)'
    if any((token in joined for token in ['resolution', 'size', '\u89c4\u683c', '\u5206\u8fa8\u7387', '\u6e05\u6670\u5ea6'])):
        return 'Size / spec (quality or output tier)'
    if 'width' in joined:
        return 'Width (pixels)'
    if 'height' in joined:
        return 'Height (pixels)'
    if any((token in joined for token in ['duration', '\u65f6\u957f'])):
        return 'Duration (seconds)'
    if 'seed' in joined or '\u968f\u673a' in joined:
        return 'Randomness / Seed (fix it when you want to reproduce)'
    if any((token in joined for token in ['steps', 'step', '\u91c7\u6837\u6b65\u6570'])):
        return 'Sampling steps (refinement strength)'
    if 'cfg' in joined:
        return 'Prompt strength (how strongly to follow the text)'
    if 'guidance' in tokens and 'scale' in tokens:
        return 'Prompt strength (how strongly to follow the text)'
    if any((token in joined for token in ['strength', 'denoise', '\u91cd\u7ed8\u5f3a\u5ea6'])):
        return 'Redraw strength (how much the source image changes)'
    if 'sampler' in tokens:
        return 'Sampling method (generation strategy)'
    if 'quality' in tokens:
        return 'Quality tier'
    if 'weight' in tokens:
        return 'Weight setting (influence strength)'
    if 'temperature' in tokens:
        return 'Random divergence'
    if 'top' in tokens and 'p' in tokens:
        return 'Sampling range (Top P)'
    if any((token in joined for token in ['model', 'checkpoint', '\u6863\u4f4d'])):
        return 'Model tier'
    if field_label:
        if _contains_cjk(field_label):
            return field_label
    if field_name and _contains_cjk(field_name):
        return field_name
    token_labels = {'image': 'image', 'audio': 'audio', 'video': 'video', 'prompt': 'prompt', 'text': 'text', 'seed': 'Seed', 'random': 'random', 'ratio': 'ratio', 'aspect': 'aspect', 'size': 'size', 'resolution': 'resolution', 'width': 'width', 'height': 'height', 'duration': 'duration', 'step': 'steps', 'steps': 'steps', 'cfg': 'prompt strength', 'guidance': 'guidance', 'scale': 'strength', 'strength': 'redraw strength', 'denoise': 'denoise strength', 'model': 'model', 'checkpoint': 'model', 'sampler': 'sampler', 'quality': 'quality', 'weight': 'weight', 'temperature': 'randomness', 'count': 'count', 'batch': 'batch'}
    translated_tokens: list[str] = []
    for token in tokens:
        label = token_labels.get(token)
        if label and label not in translated_tokens:
            translated_tokens.append(label)
    if translated_tokens:
        return f"Other controllable parameters ({' / '.join(translated_tokens[:3])})"
    type_labels = {'number': 'number', 'slider': 'number', 'select': 'choice', 'dropdown': 'choice', 'radio': 'choice', 'checkbox': 'toggle', 'switch': 'toggle', 'boolean': 'toggle', 'text': 'text', 'textarea': 'text', 'customtext': 'text'}
    type_label = type_labels.get(field_type)
    if type_label:
        return f'Other controllable parameters ({type_label})'
    return 'Other controllable parameters (needs confirmation)'
