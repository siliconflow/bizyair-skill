"""Remote-object info assembly + card rendering.

Main responsibilities:
  - resolve_info_target / resolve_remote_prefill_target: normalize a link / draft id / app id
    into a usable bizy_model_id
  - build_remote_info_output: fold remote detail / webapp_detail / version_detail / workflow into
    a two-layer JSON of summary (for the LLM) + raw (for development)
  - build_remote_info_summary: inner structure of summary (identity / capability / workflow_summary
    / human_summary / resolved_contract / execution_support / executable_draft)
  - build_remote_prefilled_card: **the only confirmation-card renderer**. The 10 fixed menu models
    (5 image + 5 video) and any remote app all go through it. Card fields, English labels and
    option values all come from the contract
  - choose_remote_field_display_label / humanize_remote_prefill_label: generic English label
    translation (logical_name first, fallback to field_name heuristics). Covers prompt /
    aspect_ratio / resolution / quality / cfg / strength / temperature / top_p / sampler and 20+
    other fields

[After the unified execution path]
The 10 fixed menu models (5 image + 5 video) no longer have dedicated renderers. Single-task
dispatch.py and batch batch.py both call build_remote_prefilled_card directly; the parameter
table / English labels / option values are all taken dynamically from the BizyAir remote
contract. Adding a new menu model only needs one ID row in dispatch_common.ROUTE_TABLE, with
zero field configuration.
"""
from __future__ import annotations
import argparse, json, re, sys, urllib.parse
from collections import Counter
from typing import Any

import api
import contract
import common
from common import (
    BIZYAIR_INFO_HOSTS, BIZY_MODEL_DETAIL_URL, COMMUNITY_URL, OFFICIAL_URL,
)

# The public list endpoint accepts these enums. Unknown values return HTTP 200 with an
# empty page, which looks like "no data" instead of "bad parameter", so validate locally.
SUPPORTED_REMOTE_TYPES = {
    'Application', 'Workflow', 'MCP', 'Checkpoint', 'LoRA', 'VAE', 'UNet',
    'CLIP', 'CLIP Vision', 'ControlNet', 'Upscaler', 'Embedding', 'Other',
}

REMOTE_TYPE_ALIASES = {
    'image': 'Application', 'application': 'Application', 'app': 'Application',
    'video': 'Workflow', 'workflow': 'Workflow', 'mcp': 'MCP',
    'model': 'Checkpoint', 'checkpoint': 'Checkpoint', 'lora': 'LoRA',
    'vae': 'VAE', 'unet': 'UNet', 'clip': 'CLIP', 'clip vision': 'CLIP Vision',
    'controlnet': 'ControlNet', 'upscaler': 'Upscaler', 'embedding': 'Embedding',
    'other': 'Other',
}

# Verified against the public community/official list endpoints on 2026-11-06:
# all four values return 200; Recommend/Trending return 400 invalid parameter: Sort.
SUPPORTED_SORTS = {'Most Used', 'Recently', 'Most Liked', 'Auto'}
SORT_ALIASES = {
    'recent': 'Recently', 'recently': 'Recently', 'new': 'Recently', 'latest': 'Recently',
    'most used': 'Most Used', 'popular': 'Most Used', 'most liked': 'Most Liked',
    'liked': 'Most Liked', 'auto': 'Auto',
}

def resolve_list_sort(value: str | None, *, remote_source: str='community', default: str | None=None) -> str:
    """Normalize a user sort word to the list endpoint enum; unknown values pass through for local validation.

    `default` lets each caller keep its historical default (browse/search use Recently;
    app pickers use Most Used).
    """
    raw = str(value or '').strip()
    if not raw:
        if default:
            return default
        return 'Auto' if common.normalized_text(remote_source) == 'official' else 'Most Used'
    return SORT_ALIASES.get(raw.lower(), raw)

def resolve_remote_type(value: str | None) -> str | None:
    """Map a user modality/type word to the platform enum; unknown values are returned unchanged."""
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip()
    return REMOTE_TYPE_ALIASES.get(common.normalized_text(raw), raw)

def normalize_remote_type(modality: str | None) -> str | None:
    return resolve_remote_type(modality)

def remote_list_endpoint(remote_source: str) -> str:
    return OFFICIAL_URL if common.normalized_text(remote_source) == 'official' else COMMUNITY_URL

def is_workflow_identity(detail_result: dict[str, Any] | None, fallback_model: dict[str, Any] | None=None) -> bool:
    detail_data = api.extract_result_data(detail_result)
    fallback = fallback_model or {}
    kind = str(detail_data.get('type') or fallback.get('type') or '').strip().lower()
    return kind == 'workflow'

def collect_workflow_ref_ids(detail_result: dict[str, Any] | None, webapp_detail: dict[str, Any] | None, version_detail: dict[str, Any] | None) -> dict[str, Any]:
    detail_data = api.extract_result_data(detail_result)
    webapp_data = api.extract_result_data(webapp_detail)
    version_data = api.extract_result_data(version_detail)
    first_version = {}
    versions = detail_data.get('versions') or []
    if versions and isinstance(versions[0], dict):
        first_version = versions[0]
    return {'ref_bizy_model_id': version_data.get('ref_bizy_model_id') or webapp_data.get('ref_bizy_model_id') or first_version.get('ref_bizy_model_id') or detail_data.get('ref_bizy_model_id'), 'ref_web_app_id': version_data.get('ref_web_app_id') or webapp_data.get('ref_web_app_id') or first_version.get('ref_web_app_id') or detail_data.get('ref_web_app_id')}

def build_workflow_info_only_message() -> str:
    return 'This is a pure ComfyUI workflow with no corresponding AI app version, so it cannot be run directly for now~ You can open it on the BizyAir web page and load it into the ComfyUI editor to run.'

def build_missing_execution_mapping_message() -> str:
    return 'I have already organized the info and parameters for you~ but the backend does not have a stable execution route for it yet, so to be safe we will not run it directly this round. Would you like to take the parameters and try on the web page?'

def build_workflow_ref_app_ready_message() -> str:
    return 'All set~ the channel for this workflow is fully open, so I can run it for you at any time~'

def resolve_execution_webapp_id(detail_result: dict[str, Any] | None, version_detail: dict[str, Any] | None, fallback_model: dict[str, Any] | None=None) -> Any:
    """When /v1/webapp/{id}/detail cannot find the object by bizy_model_id, find a version ID that works.

    Community Applications have two ID namespaces: the model ID (e.g. 56818) and the version ID used
    in web page URLs (e.g. 57978).
    `/v1/webapp/{id}/detail` only accepts the latter; querying with the model ID returns empty data
    (no id field).
    """
    version_data = api.extract_result_data(version_detail)
    for candidate in (version_data.get('id'), version_data.get('bizy_model_version_id'), version_data.get('version_id')):
        if candidate not in (None, '', [], {}):
            return candidate
    detail_data = api.extract_result_data(detail_result)
    versions = detail_data.get('versions') or []
    if versions and isinstance(versions[0], dict) and versions[0].get('id') is not None:
        return versions[0]['id']
    for candidate in (detail_data.get('bizy_model_version_id'), detail_data.get('version_id')):
        if candidate not in (None, '', [], {}):
            return candidate
    fallback = fallback_model or {}
    fallback_versions = fallback.get('versions') or []
    if fallback_versions and isinstance(fallback_versions[0], dict) and fallback_versions[0].get('id') is not None:
        return fallback_versions[0]['id']
    return None


def resolve_remote_execution_target(api_key: str, detail_result: dict[str, Any] | None, webapp_detail: dict[str, Any] | None, version_detail: dict[str, Any] | None, *, fallback_model: dict[str, Any] | None=None) -> dict[str, Any]:
    webapp_data = api.extract_result_data(webapp_detail)
    if not is_workflow_identity(detail_result, fallback_model=fallback_model):
        supported = bool(webapp_data.get('id'))
        resolved_from = None
        if not supported:
            # Querying webapp detail with the model ID returns empty data; fall back to the resolved version ID and retry.
            fallback_id = resolve_execution_webapp_id(detail_result, version_detail, fallback_model=fallback_model)
            if fallback_id is not None:
                retry = api.fetch_remote_webapp_detail(api_key, fallback_id)
                retry_data = api.extract_result_data(retry)
                if retry_data.get('id'):
                    webapp_detail = retry
                    webapp_data = retry_data
                    supported = True
                    resolved_from = str(fallback_id)
        target = {'supported': supported, 'mode': 'direct_webapp' if supported else 'missing_webapp_mapping', 'reason': None if supported else 'missing_webapp_mapping', 'webapp_detail': webapp_detail or {}, 'web_app_id': webapp_data.get('id'), 'resolved_from': resolved_from, 'support_scope': {'info': True, 'parameter_card': True, 'execute': supported}, 'message': None if supported else build_missing_execution_mapping_message(), 'feedback_channel': None}
        return target
    ref_ids = collect_workflow_ref_ids(detail_result, webapp_detail, version_detail)
    candidate_ids: list[str] = []
    for candidate in [ref_ids.get('ref_bizy_model_id'), ref_ids.get('ref_web_app_id')]:
        if candidate in (None, '', [], {}):
            continue
        candidate_text = str(candidate)
        if candidate_text not in candidate_ids:
            candidate_ids.append(candidate_text)
    for candidate in candidate_ids:
        resolved = api.fetch_remote_webapp_detail(api_key, candidate)
        resolved_data = api.extract_result_data(resolved)
        if resolved_data.get('id'):
            return {'supported': True, 'mode': 'workflow_ref_app', 'reason': None, 'webapp_detail': resolved, 'web_app_id': resolved_data.get('id'), 'ref_bizy_model_id': ref_ids.get('ref_bizy_model_id'), 'ref_web_app_id': ref_ids.get('ref_web_app_id') or resolved_data.get('id'), 'resolved_from': candidate, 'support_scope': {'info': True, 'parameter_card': True, 'execute': True}, 'message': build_workflow_ref_app_ready_message(), 'feedback_channel': None}
    return {'supported': False, 'mode': 'workflow_info_only', 'reason': 'workflow_without_ref_app', 'webapp_detail': {}, 'web_app_id': None, 'ref_bizy_model_id': ref_ids.get('ref_bizy_model_id'), 'ref_web_app_id': ref_ids.get('ref_web_app_id'), 'support_scope': {'info': True, 'parameter_card': True, 'execute': False}, 'message': build_workflow_info_only_message(), 'feedback_channel': None}

def preflight_remote_app_run(api_key: str, bizy_model_id: str | int) -> dict[str, Any]:
    detail_attempt = api.fetch_remote_detail(api_key, bizy_model_id)
    original_detail = api.unwrap_result_payload(detail_attempt) if detail_attempt.get('ok') else None
    webapp_detail_attempt = api.fetch_remote_webapp_detail(api_key, bizy_model_id)
    webapp_data = api.extract_result_data(webapp_detail_attempt)
    prefer_webapp = should_prefer_webapp_identity(original_detail, webapp_detail_attempt)
    id_collision = detect_model_webapp_collision(original_detail, webapp_detail_attempt) if prefer_webapp else None
    fallback_model = None
    if prefer_webapp:
        # An app page version ID can collide with a model hub ID (e.g. 56747: App version + LoRA asset):
        # trust webapp detail so the LoRA's version_id / base model / file fields do not leak into the
        # execution summary.
        version_id = webapp_data.get('id')
        version_detail = api.fetch_remote_version_detail(api_key, version_id) if version_id is not None else {}
        effective_detail = build_webapp_app_detail(bizy_model_id, webapp_detail_attempt, version_detail)
    else:
        detail = original_detail
        detail_data = api.extract_result_data(detail)
        # An Application's bizy_model_id is sometimes rejected by /v1/bizy_models/{id}/detail (400 invalid parameter);
        # in that case find the object again by ID in the community/official list and use its
        # versions[0].id as the version ID.
        if not api.result_is_ok(detail_attempt) or not detail_data.get('versions'):
            fallback_model = api.find_remote_model_by_id(api_key, bizy_model_id, remote_source='community')
            if fallback_model is None:
                fallback_model = api.find_remote_model_by_id(api_key, bizy_model_id, remote_source='official')
        versions = detail_data.get('versions') or (fallback_model or {}).get('versions') or []
        version_id = None
        if versions and isinstance(versions[0], dict):
            version_id = versions[0].get('id')
        if version_id is None:
            version_id = detail_data.get('bizy_model_version_id') or detail_data.get('version_id')
        version_detail = api.fetch_remote_version_detail(api_key, version_id) if version_id is not None else {}
        effective_detail = detail
    workflow = api.fetch_remote_workflow(api_key, version_id) if version_id is not None else {}
    if not api.extract_result_data(workflow):
        workflow = api.fetch_remote_workflow_from_url(webapp_data.get('web_app_workflow_url'))
    execution_target = resolve_remote_execution_target(api_key, effective_detail, webapp_detail_attempt, version_detail, fallback_model=fallback_model)
    effective_webapp_detail = execution_target.get('webapp_detail') or webapp_detail_attempt
    resolved_contract = contract.resolve_remote_input_contract(execution_target=execution_target, webapp_detail=effective_webapp_detail, workflow=workflow)
    summary = build_remote_info_summary(bizy_model_id=str(bizy_model_id), detail_result=effective_detail, webapp_detail=effective_webapp_detail, fallback_model=fallback_model, version_detail=version_detail, workflow=workflow, execution_target=execution_target, resolved_contract=resolved_contract)
    return {
        'bizy_model_id': str(bizy_model_id),
        'version_id': version_id,
        'fallback_model': fallback_model,
        'detail': effective_detail,
        'raw_detail': original_detail,
        'id_collision': id_collision,
        'webapp_detail': effective_webapp_detail,
        'version_detail': version_detail,
        'workflow': workflow,
        'execution_target': execution_target,
        'resolved_contract': resolved_contract,
        'summary': summary,
    }

def collect_workflow_summary(workflow: dict[str, Any]) -> dict[str, Any]:
    nodes = workflow.get('nodes', []) if isinstance(workflow, dict) else []
    node_types: dict[str, int] = {}
    image_inputs = 0
    audio_inputs = 0
    video_inputs = 0
    prompt_like_nodes: list[str] = []
    save_like_nodes: list[str] = []
    widget_candidates: list[dict[str, Any]] = []
    preferred_param_names = {'prompt', 'text', 'seed', 'steps', 'cfg', 'strength', 'width', 'height', 'aspect_ratio', 'size', 'duration', 'model', 'images', 'image', 'audio', 'video'}
    noise_node_keywords = ['save', 'preview']
    noise_value_markers = ['comfyui']
    for node in nodes:
        node_type = str(node.get('type') or 'Unknown')
        node_types[node_type] = node_types.get(node_type, 0) + 1
        lowered = node_type.lower()
        if 'loadimage' in lowered:
            image_inputs += 1
        if 'audio' in lowered and ('load' in lowered or 'input' in lowered):
            audio_inputs += 1
        if 'video' in lowered and ('load' in lowered or 'input' in lowered):
            video_inputs += 1
        if any((x in lowered for x in ['prompt', 'text', 'banana', 'qwen', 'gemini', 'flux', 'wan'])):
            if node_type not in prompt_like_nodes:
                prompt_like_nodes.append(node_type)
        if any((x in lowered for x in noise_node_keywords)):
            if node_type not in save_like_nodes:
                save_like_nodes.append(node_type)
        widget_values = node.get('widgets_values')
        inputs = node.get('inputs') or []
        outputs = node.get('outputs') or []
        output_names = [str(o.get('name') or '') for o in outputs]
        if isinstance(widget_values, list):
            for (idx, value) in enumerate(widget_values):
                widget_name = None
                if idx < len(inputs):
                    widget_name = inputs[idx].get('name') or inputs[idx].get('label')
                if not widget_name and idx < len(output_names):
                    widget_name = output_names[idx]
                widget_name = str(widget_name or f'widget_{idx}')
                if not isinstance(value, (str, int, float, bool)) or str(value) == '':
                    continue
                widget_candidates.append({'node_id': node.get('id'), 'node_type': node_type, 'name': widget_name, 'value': value, 'confidence': 'confirmed_from_resolved_workflow'})
    top_node_types = sorted(node_types.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
    key_parameters = []
    seen_param_keys: set[tuple[str, str]] = set()
    for item in widget_candidates:
        name_low = str(item['name']).lower()
        node_low = str(item['node_type']).lower()
        value_low = str(item['value']).lower()
        if any((k in node_low for k in noise_node_keywords)):
            continue
        if any((marker == value_low for marker in noise_value_markers)):
            continue
        looks_key = name_low in preferred_param_names or any((k in name_low for k in ['prompt', 'seed', 'ratio', 'size', 'width', 'height', 'steps', 'cfg', 'strength', 'duration', 'model'])) or ('image' in name_low and 'load' not in node_low) or ('audio' in name_low and 'load' not in node_low) or ('video' in name_low and 'load' not in node_low)
        if not looks_key:
            continue
        dedupe = (str(item['node_type']), str(item['name']))
        if dedupe in seen_param_keys:
            continue
        seen_param_keys.add(dedupe)
        key_parameters.append(item)
        if len(key_parameters) >= 12:
            break
    return {'node_count': len(nodes), 'top_node_types': [{'type': k, 'count': v} for (k, v) in top_node_types], 'detected_inputs': {'image_like_loaders': image_inputs, 'audio_like_loaders': audio_inputs, 'video_like_loaders': video_inputs}, 'prompt_like_nodes': prompt_like_nodes[:8], 'save_like_nodes': save_like_nodes[:8], 'key_parameters': key_parameters, 'confidence': {'node_shape': 'confirmed_from_resolved_workflow', 'key_parameters': 'heuristic_from_resolved_workflow'}}


def build_brief_summary(identity: dict[str, Any], capability: dict[str, Any], human_summary: dict[str, Any]) -> str:
    name = identity.get('name') or identity.get('bizy_model_id') or 'unknown app'
    task = capability.get('task_type') or 'unknown_task'
    base_model = capability.get('base_model_hint') or identity.get('base_model')
    input_hint = human_summary.get('input_hint', {})
    image_count = input_hint.get('images')
    brief_parts = [str(name)]
    task_map = {'multi_image_edit_or_fusion': 'multi-image edit/fusion', 'image_to_image': 'image to image', 'text_to_image_or_general_workflow': 'text to image / general workflow', 'audio_conditioned_generation': 'audio-conditioned generation', 'video_or_animation': 'video/animation'}
    brief_parts.append(task_map.get(str(task), str(task)))
    if isinstance(image_count, int) and image_count > 0:
        brief_parts.append(f'{image_count} explicit image input(s)')
    elif image_count == 0 and task == 'text_to_image_or_general_workflow':
        brief_parts.append('no explicit image input')
    if base_model:
        brief_parts.append(f'base model: {base_model}')
    param_map = {str(item.get('name')): item.get('value') for item in human_summary.get('important_params', [])}
    if param_map.get('aspect_ratio'):
        brief_parts.append(f"aspect ratio {param_map['aspect_ratio']}")
    if param_map.get('image_size'):
        brief_parts.append(f"size {param_map['image_size']}")
    return ', '.join(brief_parts)

def get_remote_contract_binding_keys(contract_data: dict[str, Any], logical_name: str) -> list[str]:
    return contract.get_remote_contract_binding_keys(contract_data, logical_name)

def build_remote_executable_draft(*, bizy_model_id: str, detail_result: dict[str, Any] | None, webapp_detail: dict[str, Any] | None, fallback_model: dict[str, Any] | None, version_detail: dict[str, Any] | None, workflow: dict[str, Any] | None, execution_target: dict[str, Any] | None=None, resolved_contract: dict[str, Any] | None=None) -> dict[str, Any]:
    detail_data = api.extract_result_data(detail_result)
    execution = execution_target or {}
    runtime_webapp_detail = execution.get('webapp_detail') or webapp_detail or {}
    webapp_detail_data = api.extract_result_data(runtime_webapp_detail)
    version_data = api.extract_result_data(version_detail)
    workflow_data = api.extract_result_data(workflow)
    input_hints = contract.collect_required_input_hints(workflow_data)
    resolved = resolved_contract or contract.resolve_remote_input_contract(execution_target=execution, webapp_detail=webapp_detail, workflow=workflow)
    required_inputs = list(resolved.get('required_inputs') or [])
    hidden_media_slots = collect_hidden_remote_media_slots(webapp_detail_data) if webapp_detail_data else {}
    existing_required_types = {str(item.get('type')) for item in required_inputs}
    for media_type in ['image', 'audio', 'video']:
        if media_type in existing_required_types:
            continue
        hidden_count = len(hidden_media_slots.get(media_type, []))
        if hidden_count > 0:
            required_inputs.append({'type': media_type, 'count': hidden_count, 'required': True})
    if execution and (not execution.get('supported')):
        return {'required_inputs': required_inputs, 'candidate_fields': resolved.get('candidate_fields') or input_hints.get('candidate_fields', []), 'resolved_contract': resolved, 'status': 'info_only', 'support_scope': execution.get('support_scope'), 'message': execution.get('message')}
    payload = api.build_remote_run_payload(str(bizy_model_id), {'bizy_model_id': str(bizy_model_id), 'version_id': version_data.get('id') or detail_data.get('bizy_model_version_id') or detail_data.get('version_id'), 'detail': detail_result or {}, 'webapp_detail': webapp_detail or {}, 'version_detail': version_detail or {}, 'workflow': workflow or {}, 'execution_target': execution}, argparse.Namespace(prompt=None, image=None, audio=None, video=None, aspect_ratio=None, resolution=None, steps=None, duration=None, seed=None, random_seed=False, model_name=None, width=None, height=None, param=None, output=None, sync=False))
    return {'required_inputs': required_inputs, 'candidate_fields': resolved.get('candidate_fields') or input_hints.get('candidate_fields', []), 'resolved_contract': resolved, 'status': 'draft_from_detail_version_workflow', 'execution_mode': execution.get('mode') or 'direct_webapp', 'execution_web_app_id': webapp_detail_data.get('id')}

def build_remote_info_summary(*, bizy_model_id: str, detail_result: dict[str, Any] | None, webapp_detail: dict[str, Any] | None, fallback_model: dict[str, Any] | None, version_detail: dict[str, Any] | None, workflow: dict[str, Any] | None, execution_target: dict[str, Any] | None=None, resolved_contract: dict[str, Any] | None=None) -> dict[str, Any]:
    version_data = api.extract_result_data(version_detail)
    workflow_data = api.extract_result_data(workflow) or workflow
    workflow_summary = collect_workflow_summary(workflow_data or {}) if workflow_data else {}
    capability = {}
    detail_data = api.extract_result_data(detail_result)
    webapp_detail_data = api.extract_result_data(webapp_detail)
    fallback = fallback_model or {}
    execution = execution_target or {}
    resolved = resolved_contract or contract.resolve_remote_input_contract(execution_target=execution, webapp_detail=webapp_detail, workflow=workflow)
    id_role = detail_data.get('id_role')
    identity = {
        'input_id': bizy_model_id,
        'id_role': id_role,
        'bizy_model_id': None if id_role == 'app_version' else bizy_model_id,
        'bizy_model_version_id': version_data.get('id') or detail_data.get('bizy_model_version_id') or webapp_detail_data.get('id'),
        'web_app_id': webapp_detail_data.get('id'),
        'execution_web_app_id': execution.get('web_app_id'),
        'version_id': version_data.get('id') or detail_data.get('bizy_model_version_id') or (fallback.get('versions', [{}])[0].get('id') if fallback.get('versions') else None) or (webapp_detail_data.get('id') if id_role == 'app_version' else None),
        'name': detail_data.get('name') or webapp_detail_data.get('name') or fallback.get('name') or version_data.get('name'),
        'type': detail_data.get('type') or fallback.get('type'),
        'base_model': version_data.get('base_model') or webapp_detail_data.get('base_model') or (fallback.get('versions', [{}])[0].get('base_model') if fallback.get('versions') else None),
        'intro': version_data.get('intro') or webapp_detail_data.get('intro'),
        'sign': version_data.get('sign') or (fallback.get('versions', [{}])[0].get('sign') if fallback.get('versions') else None),
        'path': version_data.get('path') or (fallback.get('versions', [{}])[0].get('file_name') if fallback.get('versions') else None),
        'draft_id': version_data.get('draft_id') or (fallback.get('versions', [{}])[0].get('draft_id') if fallback.get('versions') else None),
        'public': version_data.get('public'),
        'available': version_data.get('available'),
    }
    specialized_parameters = []
    important_params = specialized_parameters or [{'name': item.get('name'), 'value': item.get('value'), 'node_type': item.get('node_type'), 'confidence': item.get('confidence')} for item in workflow_summary.get('key_parameters', [])[:8] if str(item.get('name') or '').lower() not in {'prompt', 'text', 'example_prompt'}]
    important_params = [item for item in important_params if str(item.get('name') or '').lower() not in {'prompt', 'text', 'example_prompt'}]
    human_summary = {'task_guess': capability.get('task_type'), 'input_profile': capability.get('input_profile') or {}, 'input_hint': {'images': workflow_summary.get('detected_inputs', {}).get('image_like_loaders'), 'audios': workflow_summary.get('detected_inputs', {}).get('audio_like_loaders'), 'videos': workflow_summary.get('detected_inputs', {}).get('video_like_loaders')}, 'likely_nodes': [item.get('type') for item in workflow_summary.get('top_node_types', [])[:5]], 'important_params': important_params}
    brief = build_brief_summary(identity, capability, human_summary)
    executable_draft = build_remote_executable_draft(bizy_model_id=bizy_model_id, detail_result=detail_result, webapp_detail=webapp_detail, fallback_model=fallback_model, version_detail=version_detail, workflow=workflow, execution_target=execution, resolved_contract=resolved)
    execution_support = {'supported': bool(execution.get('support_scope', {}).get('execute', execution.get('supported'))), 'mode': execution.get('mode') or 'direct_webapp', 'support_scope': execution.get('support_scope') or {'info': True, 'parameter_card': True, 'execute': True}, 'ref_bizy_model_id': execution.get('ref_bizy_model_id'), 'ref_web_app_id': execution.get('ref_web_app_id'), 'message': execution.get('message')}
    return {'identity': identity, 'capability': capability, 'brief': brief, 'workflow_summary': workflow_summary, 'human_summary': human_summary, 'resolved_contract': resolved, 'execution_support': execution_support, 'executable_draft': executable_draft}

def remote_identity_kind(identity: dict[str, Any]) -> str:
    """Normalize the object type: workflow / application / unknown.

    In bizyair.ai webapp detail the `type` is often null, so infer from available evidence:
    an execution webapp id → app; only a base model and no channel → workflow.
    """
    kind = common.normalized_text(identity.get('type'))
    if kind in {'workflow', 'application'}:
        return kind
    if identity.get('web_app_id') or identity.get('execution_web_app_id'):
        return 'application'
    if identity.get('base_model'):
        return 'workflow'
    return 'unknown'

def remote_identity_label(identity: dict[str, Any]) -> str:
    """Short type name used in the confirmation-card title."""
    return {'workflow': 'Workflow', 'application': 'App'}.get(remote_identity_kind(identity), 'Object')

def remote_identity_label_zh(identity: dict[str, Any]) -> str:
    """Type name used in the confirmation card's "type" row; when it cannot be inferred, explain why instead of just writing "object"."""
    return {'workflow': 'Workflow', 'application': 'AI app'}.get(remote_identity_kind(identity), 'Object (type not labeled by the platform)')

def infer_remote_prompt_modality(human_summary: dict[str, Any]) -> str:
    input_profile = human_summary.get('input_profile') or {}
    if common.safe_int(input_profile.get('video_inputs')) > 0:
        return 'video'
    if str(human_summary.get('task_guess') or '').startswith('video'):
        return 'video'
    return 'image'

def summarize_remote_confirmation_fields(summary: dict[str, Any]) -> list[str]:
    draft = summary.get('executable_draft') or {}
    human_summary = summary.get('human_summary') or {}
    important_params = human_summary.get('important_params') or []
    candidate_fields = draft.get('candidate_fields') or []
    resolved_contract = summary.get('resolved_contract') or draft.get('resolved_contract') or {}
    labels: list[str] = []
    seen: set[str] = set()

    def add(label: str) -> None:
        if label not in seen:
            seen.add(label)
            labels.append(label)
    raw_fields = []
    for item in important_params:
        raw_fields.append(str(item.get('name') or ''))
    for item in candidate_fields:
        raw_fields.append(str(item))
    for logical_name in ['aspect_ratio', 'resolution', 'duration', 'model_name', 'seed']:
        if contract.remote_contract_supports(resolved_contract, logical_name):
            raw_fields.append(logical_name)
    joined = '\n'.join(raw_fields).lower()
    # Chinese tokens in the matchers below are internal input aliases; never displayed
    if any((x in joined for x in ['ratio', 'aspect', '\u6bd4\u4f8b', '\u753b\u5e45'])):
        add('Aspect ratio / frame')
    if any((x in joined for x in ['size', 'resolution', 'width', 'height', '\u5c3a\u5bf8', '\u5206\u8fa8\u7387', '\u89c4\u683c'])):
        add('Size / spec')
    if any((x in joined for x in ['duration', '\u65f6\u957f', '\u79d2'])):
        add('Duration')
    if any((x in joined for x in ['seed', '\u968f\u673a'])):
        add('Randomness / Seed')
    if any((x in joined for x in ['model', 'checkpoint', '\u6863\u4f4d'])):
        add('Model tier')
    return labels

def _humanize_remote_field_label(field_label: str) -> str:
    """Turn a remote field_label into a readable English label; unknown fields keep their original text so at least the meaning is not lost."""
    raw = re.sub(r'^\s*\d+\.\s*', '', str(field_label or '').strip())
    low = raw.lower()
    known = {
        'upload image': 'upload image',
        'english prompt (optional)': 'English prompt (optional)',
        'prompt': 'prompt',
        'long-edge size of the uploaded image (in pixels) (not absolute; the value affects the generated image size)': 'long-edge size of the uploaded image (pixels)',
        'extend size to the left (in pixels)': 'extend left (pixels)',
        'upward expansion size (in pixels)': 'extend upward (pixels)',
        'expand size to the right (unit: pixels)': 'extend right (pixels)',
        'downward expansion size (pixels)': 'extend downward (pixels)',
        'feathering (unit: pixels)': 'feathering (pixels)',
        'feathering': 'feathering (pixels)',
        'bottom watermark extension': 'bottom watermark extension',
    }
    if low in known:
        return known[low]
    if low.startswith('extend size to the left'):
        return 'extend left (pixels)'
    if low.startswith('upward expansion size'):
        return 'extend upward (pixels)'
    if low.startswith('expand size to the right'):
        return 'extend right (pixels)'
    if low.startswith('downward expansion size'):
        return 'extend downward (pixels)'
    if low.startswith('long-edge size'):
        return 'long-edge size of the uploaded image (pixels)'
    simple = {
        'left': 'left extension (pixels)', 'top': 'top extension (pixels)',
        'right': 'right extension (pixels)', 'bottom': 'bottom extension (pixels)',
    }
    if low in simple:
        return simple[low]
    if low.startswith('pixels to expand'):
        for direction, label in simple.items():
            if direction in low:
                return label
    return raw or str(field_label or '')


def choose_remote_field_display_label(field: dict[str, Any], *, prefer_field_label: bool = False) -> str:
    display_name = str(field.get('display_name') or '').strip()
    field_label = str(field.get('field_label') or '').strip()
    # Duplicate-named fields must be told apart by field_label, otherwise several a's render as the same "Size / spec".
    if prefer_field_label and field_label:
        return _humanize_remote_field_label(field_label)
    # internal input alias; never displayed (legacy Chinese generic label produced by older callers)
    generic_display_prefixes = ('Other controllable parameters', '\u5176\u4ed6\u53ef\u63a7\u53c2\u6570')
    if display_name and (not display_name.startswith(generic_display_prefixes)):
        return display_name
    if field_label:
        humanized = _humanize_remote_field_label(field_label)
        if humanized:
            return humanized
    logical_name = str(field.get('logical_name') or '').strip()
    logical_labels = {'prompt': 'text instruction (tells the model what to do this round)', 'negative_prompt': 'negative prompt (what should not appear)', 'aspect_ratio': 'aspect ratio / frame (orientation and composition ratio)', 'resolution': 'size / spec (quality or output tier)', 'width': 'width (pixels)', 'height': 'height (pixels)', 'duration': 'duration (seconds)', 'seed': 'randomness / seed (fix it to reproduce)', 'steps': 'sampling steps (refinement strength)', 'model_name': 'model tier', 'batch_size': 'generation count (how many per run)', 'sound': 'sound toggle', 'cfg_scale': 'motion strength', 'image': 'image asset', 'audio': 'audio asset', 'video': 'video asset'}
    return logical_labels.get(logical_name, display_name or 'Other controllable parameters (needs confirmation)')

def format_remote_option_values(field: dict[str, Any]) -> str | None:
    options = field.get('field_options') or []
    labels: list[str] = []
    ignored_markers = {'max', 'min', 'precision', 'step', 'step2', 'hideonzoom', 'minnodesize'}
    for option in options[:8]:
        label = str(option.get('label') or option.get('value') or '').strip()
        if not label:
            continue
        if label.lower() in ignored_markers:
            continue
        if label and label not in labels:
            labels.append(label)
    if not labels:
        return None
    return ' / '.join(labels)

def prefilled_remote_contract_value(field: dict[str, Any], args: argparse.Namespace, prompt_bundle: dict[str, Any] | None=None) -> str | None:
    logical_name = str(field.get('logical_name') or '').strip()
    bundle = prompt_bundle or {}
    prompt = str(bundle.get('card_display_prompt') or getattr(args, 'prompt', None) or '').strip()
    negative_prompt = str(bundle.get('negative_prompt') or '').strip()
    field_value = field.get('field_value')
    if logical_name == 'prompt':
        if prompt:
            return prompt
    elif logical_name == 'negative_prompt':
        if negative_prompt:
            return negative_prompt
        if field_value not in (None, ''):
            return str(field_value)
        if field.get('system_inject'):
            return '`system default negative prompt`'
    elif logical_name == 'seed':
        if getattr(args, 'seed', None) is not None:
            return f'`{args.seed}`'
        if getattr(args, 'random_seed', False):
            return '`random`'
    elif logical_name == 'aspect_ratio' and getattr(args, 'aspect_ratio', None):
        return f'`{args.aspect_ratio}`'
    elif logical_name == 'resolution' and getattr(args, 'resolution', None):
        return f'`{args.resolution}`'
    elif logical_name == 'duration' and getattr(args, 'duration', None) is not None:
        return f'`{args.duration} s`'
    elif logical_name == 'model_name' and getattr(args, 'model_name', None):
        return f'`{args.model_name}`'
    elif logical_name == 'width' and getattr(args, 'width', None) is not None:
        return f'`{args.width}`'
    elif logical_name == 'height' and getattr(args, 'height', None) is not None:
        return f'`{args.height}`'
    elif logical_name == 'steps' and getattr(args, 'steps', None) is not None:
        return f'`{args.steps}`'
    override_value = match_remote_field_override(logical_name, args)
    if override_value is not None and logical_name not in {'image', 'audio', 'video'}:
        if logical_name == 'duration':
            return f'`{override_value} s`'
        return f'`{override_value}`'
    if field_value not in (None, '') and (not field.get('media_type')):
        return f'`{field_value}`' if not isinstance(field_value, str) or not str(field_value).startswith('`') else str(field_value)
    return None

def should_show_remote_contract_field(field: dict[str, Any]) -> bool:
    if str(field.get('support_level') or 'supported') != 'supported':
        return False
    if field.get('media_type'):
        return False
    logical_name = str(field.get('logical_name') or '').strip()
    if logical_name in {'image', 'audio', 'video'}:
        return False
    if logical_name == 'negative_prompt':
        return True
    return bool(field.get('user_input')) or bool(field.get('system_inject'))

def build_remote_prefilled_card(summary: dict[str, Any], args: argparse.Namespace, *, input_kind: str | None=None, original_object_id: str | None=None, resolved_object_id: str | None=None, draft_resolution: dict[str, Any] | None=None, webapp_detail_data: dict[str, Any] | None=None) -> str:
    """**The single confirmation-card renderer after routes A and B were merged.**
    The 10 fixed menu models (5 image + 5 video) + any remote app/workflow all go through it.
    summary comes from build_remote_info_output; args is an argparse.Namespace (overrides the user
    already supplied).
    Outputs a markdown string containing: identity → status → prompt → required inputs → fields set
    by args → optional contract fields.
    """
    identity = summary.get('identity') or {}
    execution_support = summary.get('execution_support') or {}
    draft = summary.get('executable_draft') or {}
    supported = bool(execution_support.get('supported'))
    resolved_contract = summary.get('resolved_contract') or draft.get('resolved_contract') or {}
    name = identity.get('name') or f"BizyAir {remote_identity_label(identity)} {resolved_object_id or original_object_id or ''}".strip()
    lines = [f'🧾 **{name} — confirmation card**']
    if supported:
        lines.append('I have prefilled everything confirmable from this request; please check it.')
    else:
        lines.append('I gathered what could be confirmed this round into one card; for this kind of object we currently stop at the parameter layer and do not submit a run.')
    lines.append('')
    lines.append(f'- **Object**: {name}')
    lines.append(f'- **Type**: {remote_identity_label_zh(identity)}')
    # App pages / execution only accept the version ID; an asset ID (e.g. 56818) must not appear as the
    # executing "ID".
    # When the user gives an asset ID, the card's main ID shows the version ID and keeps the input ID
    # separately, so the numbers still line up.
    display_object_id = resolved_object_id
    identity_version_id = identity.get('version_id') or identity.get('bizy_model_version_id')
    if str(identity.get('type') or '') == 'Application' and identity_version_id not in (None, ''):
        display_object_id = str(identity_version_id)
    if display_object_id:
        lines.append(f'- **ID**: `{display_object_id}`')
    if resolved_object_id and display_object_id and str(resolved_object_id) != str(display_object_id):
        lines.append(f'- **Input ID**: `{resolved_object_id}` (model hub asset ID; execution/page use the version ID above)')
    if input_kind == 'workflow_link' and draft_resolution and draft_resolution.get('matched') and original_object_id and resolved_object_id and (original_object_id != resolved_object_id):
        lines.append(f'- **Page restore**: workflow page ID `{original_object_id}` resolved to public object `{resolved_object_id}`')
    status_text = '✅ ready to run' if supported else '⚠️ currently stops at the info/parameter layer; not executed directly'
    lines.append(f'- **Current status**: {status_text}')
    lines.append('- **Billing confirmation**: credits are charged only on submit; one confirmation submits once. If you are not happy with the result, check the outputs first and do not rerun automatically; a rerun needs a new confirmation.')
    human_summary = summary.get('human_summary') or {}
    prompt_bundle = common.build_prompt_bundle_for_args(args, route_name=name, modality=infer_remote_prompt_modality(human_summary), task=str(human_summary.get('task_guess') or ''), input_profile=human_summary.get('input_profile') or {})
    prompt = str(prompt_bundle.get('raw_prompt') or getattr(args, 'prompt', None) or '').strip()
    card_display_prompt = str(prompt_bundle.get('card_display_prompt') or prompt).strip()
    prompt_supported = bool(contract.get_supported_remote_prompt_binding_keys(resolved_contract))
    if prompt:
        lines.append(f'- **Original request**: {prompt}')
        if prompt_bundle.get('changed') and prompt_supported:
            lines.append(f'- **Execution prompt after system normalization**: {card_display_prompt}')
    required_inputs = draft.get('required_inputs') or []
    prompt_required = any((str(item.get('type')) == 'prompt' for item in required_inputs))
    image_required = next((item for item in required_inputs if str(item.get('type')) == 'image'), None)
    audio_required = next((item for item in required_inputs if str(item.get('type')) == 'audio'), None)
    video_required = next((item for item in required_inputs if str(item.get('type')) == 'video'), None)
    if prompt_required and (not prompt):
        lines.append('- **Prompt**: [to be filled in by you]')
    image_count = len(getattr(args, 'image', None) or [])
    if image_required:
        required = int(image_required.get('count') or 0)
        if image_count >= required and required > 0:
            lines.append(f'- **Image assets**: received `{image_count} image(s)`')
        elif image_count > 0:
            lines.append(f'- **Image assets**: received `{image_count} image(s)`, `{max(required - image_count, 0)}` still missing')
        else:
            lines.append(f'- **Image assets**: [to provide `{required} image(s)`]')
    elif image_count > 0:
        lines.append(f'- **Image assets**: received `{image_count} image(s)`')
    audio_count = len(getattr(args, 'audio', None) or [])
    if audio_required:
        required = int(audio_required.get('count') or 0)
        if audio_count >= required and required > 0:
            lines.append(f'- **Audio assets**: received `{audio_count} audio clip(s)`')
        elif audio_count > 0:
            lines.append(f'- **Audio assets**: received `{audio_count} audio clip(s)`, `{max(required - audio_count, 0)}` still missing')
        else:
            lines.append(f'- **Audio assets**: [to provide `{required} audio clip(s)`]')
    elif audio_count > 0:
        lines.append(f'- **Audio assets**: received `{audio_count} audio clip(s)`')
    if video_required:
        required = int(video_required.get('count') or 0)
        video_count = len(getattr(args, 'video', None) or [])
        if video_count >= required and required > 0:
            lines.append(f'- **Video assets**: received `{video_count} video clip(s)`')
        elif video_count > 0:
            lines.append(f'- **Video assets**: received `{video_count} video clip(s)`, `{max(required - video_count, 0)}` still missing')
        else:
            lines.append(f'- **Video assets**: [to provide `{required} video clip(s)`]')
    if getattr(args, 'aspect_ratio', None) and contract.remote_contract_supports(resolved_contract, 'aspect_ratio'):
        lines.append(f'- **Aspect ratio / frame**: `{args.aspect_ratio}`')
    if getattr(args, 'resolution', None) and contract.remote_contract_supports(resolved_contract, 'resolution'):
        lines.append(f'- **Size / spec**: `{args.resolution}`')
    if getattr(args, 'duration', None) is not None and contract.remote_contract_supports(resolved_contract, 'duration'):
        lines.append(f'- **Duration**: `{args.duration} s`')
    if getattr(args, 'model_name', None) and contract.remote_contract_supports(resolved_contract, 'model_name'):
        lines.append(f'- **Model tier**: `{args.model_name}`')
    if getattr(args, 'seed', None) is not None and contract.remote_contract_supports(resolved_contract, 'seed'):
        lines.append(f'- **Fixed seed**: `{args.seed}`')
    elif getattr(args, 'random_seed', False) and contract.remote_contract_supports(resolved_contract, 'seed'):
        lines.append('- **Randomness / seed**: `random`')
    visible_fields = [field for field in resolved_contract.get('fields') or [] if should_show_remote_contract_field(field)]
    if visible_fields:
        lines.append('')
        lines.append('### Confirmable fields for this object')
        # Duplicate-named fields (e.g. four a's) must be told apart by field_label + variable_name;
        # template guesses such as "items still worth confirming this round" are no longer rendered.
        name_counts = Counter(str(field.get('field_name') or '') for field in visible_fields)
        for field in visible_fields[:8]:
            field_name = str(field.get('field_name') or '')
            duplicated = name_counts.get(field_name, 0) > 1
            label = choose_remote_field_display_label(field, prefer_field_label=duplicated)
            value = prefilled_remote_contract_value(field, args, prompt_bundle=prompt_bundle)
            if value is None:
                value = '[to be confirmed]'
            option_values = format_remote_option_values(field)
            key_hint = ''
            variable_name = str(field.get('variable_name') or '').strip()
            if duplicated and variable_name:
                key_hint = f'  parameter key `{variable_name}`'
            if option_values:
                lines.append(f'- **{label}**: {value}{key_hint}  options `{option_values}`')
            else:
                lines.append(f'- **{label}**: {value}{key_hint}')
    lines.append('')
    if supported:
        lines.append('If it all looks right, just reply "run" / "go ahead" / "confirm run"; if you want to change parameters, keep telling me.')
    else:
        lines.append('For this kind of object I am stopping at the parameter layer; if you want to push further, I can help complete this card.')
    return '\n'.join(lines)

def resolve_info_target(target: str) -> dict[str, Any]:
    raw = str(target or '').strip()
    if not raw:
        return {'ok': False, 'error': 'EMPTY_INFO_TARGET', 'message': 'Please provide a BizyAir app/workflow link or numeric id'}
    if raw.isdigit():
        return {'ok': True, 'input_kind': 'raw_id', 'input_value': raw, 'resolved_object_id': raw}
    normalized = raw
    if '://' not in normalized and any(normalized.startswith(h + '/') for h in BIZYAIR_INFO_HOSTS):
        normalized = f'https://{normalized}'
    parsed = urllib.parse.urlparse(normalized)
    if not parsed.scheme or not parsed.netloc:
        trailing_digits = re.search('(\\d+)(?:/)?$', raw)
        if trailing_digits:
            resolved = trailing_digits.group(1)
            return {'ok': True, 'input_kind': 'path_like_id', 'input_value': raw, 'resolved_object_id': resolved}
        return {'ok': False, 'error': 'UNSUPPORTED_INFO_TARGET', 'message': 'Unsupported info target format', 'input_value': raw}
    host = parsed.netloc.lower().split(':', 1)[0]
    if host not in BIZYAIR_INFO_HOSTS:
        return {'ok': False, 'error': 'UNSUPPORTED_INFO_HOST', 'message': f'Only BizyAir links are supported for --info-from-link (hosts: {", ".join(sorted(BIZYAIR_INFO_HOSTS))})', 'input_value': raw, 'host': host}
    segments = [seg for seg in (parsed.path or '').split('/') if seg]
    if len(segments) >= 2 and segments[0] == 'creation' and segments[1].isdigit():
        return {'ok': False, 'error': 'UNSUPPORTED_INFO_TARGET', 'input_kind': 'creation_link', 'input_value': raw, 'normalized_url': normalized, 'resolved_object_id': segments[1], 'message': 'This is a work link from the work gallery, not an AI app / Workflow; use gallery-detail to view works.'}
    query = urllib.parse.parse_qs(parsed.query or '')
    query_id = (query.get('id') or [None])[0]
    if len(segments) >= 3 and segments[0] == 'community' and (segments[1] == 'app') and segments[2].isdigit():
        return {'ok': True, 'input_kind': 'app_link', 'input_value': raw, 'normalized_url': normalized, 'resolved_object_id': segments[2]}
    if segments and segments[0] == 'comfy-ui' and query_id and str(query_id).isdigit():
        return {'ok': True, 'input_kind': 'workflow_link', 'input_value': raw, 'normalized_url': normalized, 'resolved_object_id': str(query_id)}
    if query_id and str(query_id).isdigit():
        return {'ok': True, 'input_kind': 'bizyair_link_with_query_id', 'input_value': raw, 'normalized_url': normalized, 'resolved_object_id': str(query_id)}
    for segment in reversed(segments):
        if segment.isdigit():
            return {'ok': True, 'input_kind': 'bizyair_link_with_path_id', 'input_value': raw, 'normalized_url': normalized, 'resolved_object_id': segment}
    return {'ok': False, 'error': 'INFO_TARGET_ID_NOT_FOUND', 'message': 'Could not extract a BizyAir object id from the provided link', 'input_value': raw}

def webapp_is_application(webapp_detail: dict[str, Any] | None) -> bool:
    """Whether webapp detail represents an app that can be run directly.

    Only objects returned by `/v1/webapp/{id}/detail` count: they have an id and at least a name or
    input_nodes.
    That endpoint only accepts version IDs; querying with an app asset ID usually 404s.
    """
    data = api.extract_result_data(webapp_detail)
    if not data.get('id'):
        return False
    return bool(data.get('name') or data.get('input_nodes'))


def build_webapp_app_detail(requested_id: str | int, webapp_detail: dict[str, Any] | None,
                            version_detail: dict[str, Any] | None=None) -> dict[str, Any]:
    """Wrap webapp detail into the detail shape so summary / draft can use it uniformly.

    Scenario: the same numeric ID is a LoRA in the model hub and a version ID on the app page (e.g. 56747).
    The app page's webapp detail is the object the user wants to run; model detail must not be used as
    the identity.
    """
    webapp = api.extract_result_data(webapp_detail)
    version = api.extract_result_data(version_detail)
    version_id = webapp.get('id')
    version_entry = {
        'id': version_id,
        'name': webapp.get('name'),
        'base_model': webapp.get('base_model') or version.get('base_model'),
        'sign': version.get('sign'),
        'path': version.get('path') or version.get('file_name'),
        'public': version.get('public'),
        'available': version.get('available'),
        'version': version.get('version') or version.get('version_name'),
        'ref_bizy_model_id': webapp.get('ref_bizy_model_id') or version.get('ref_bizy_model_id'),
        'ref_web_app_id': webapp.get('ref_web_app_id') or version.get('ref_web_app_id'),
    }
    return {
        'ok': True,
        'status': 200,
        'data': {
            'code': 20000,
            'message': 'Ok',
            'status': True,
            'data': {
                'id': requested_id,
                'bizy_model_id': None,
                'bizy_model_version_id': version_id,
                'id_role': 'app_version',
                'name': webapp.get('name'),
                'type': 'Application',
                'base_model': webapp.get('base_model') or version.get('base_model'),
                'ref_bizy_model_id': webapp.get('ref_bizy_model_id') or version.get('ref_bizy_model_id'),
                'versions': [version_entry] if version_id not in (None, '') else [],
            },
        },
    }


def detect_model_webapp_collision(detail_result: dict[str, Any] | None,
                                  webapp_detail: dict[str, Any] | None) -> dict[str, Any] | None:
    """Detect that the same numeric ID exists both in the model hub and in the app version store."""
    model = api.extract_result_data(detail_result)
    webapp = api.extract_result_data(webapp_detail)
    if not model.get('id') or not webapp.get('id'):
        return None
    return {
        'requested_id': str(model.get('id')),
        'model': {'id': model.get('id'), 'name': model.get('name'), 'type': model.get('type')},
        'webapp': {'id': webapp.get('id'), 'name': webapp.get('name')},
        'resolution': 'webapp_app_version',
        'hint': 'This numeric ID is both a model hub object and an app version ID; info/run/prefill resolve it as an app page first. To look up the model hub object, use model-detail.',
    }


def should_prefer_webapp_identity(detail_result: dict[str, Any] | None,
                                  webapp_detail: dict[str, Any] | None) -> bool:
    """Prefer the webapp identity when an app version ID collides with a model ID.

    Workflow exception: for a workflow its own detail is the identity; webapp detail may just be the
    app it references.
    """
    if not webapp_is_application(webapp_detail):
        return False
    model = api.extract_result_data(detail_result)
    return str(model.get('type') or '').strip().lower() != 'workflow'



def build_remote_info_output(app_id: str, *, api_key_arg: str | None=None, include_workflow: bool=False) -> dict[str, Any]:
    api_key = api.require_api_key(api_key_arg)
    if not api_key:
        print(json.dumps({'error': 'NO_REMOTE_CREDENTIAL', 'message': 'Remote detail/info requires api key', 'supported_inputs': {'api_key': f'--api-key / BIZYAIR_API_KEY / {common.config_display_path()} -> credentials.api_key'}}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    webapp_detail_attempt = api.fetch_remote_webapp_detail(api_key, app_id)
    webapp_data = api.extract_result_data(webapp_detail_attempt)
    detail_attempt = api.safe_request_json('GET', f'{BIZY_MODEL_DETAIL_URL}/{app_id}/detail', api_key)
    original_detail = api.unwrap_result_payload(detail_attempt) if detail_attempt.get('ok') else None
    original_detail_data = api.extract_result_data(original_detail)
    prefer_webapp = should_prefer_webapp_identity(original_detail, webapp_detail_attempt)
    id_collision = detect_model_webapp_collision(original_detail, webapp_detail_attempt) if prefer_webapp else None
    fallback_model = None
    id_diagnosis = None
    if prefer_webapp:
        version_id = webapp_data.get('id')
        version_detail = api.fetch_remote_version_detail(api_key, version_id) if version_id is not None else None
        effective_detail = build_webapp_app_detail(app_id, webapp_detail_attempt, version_detail)
    else:
        detail = original_detail
        if not detail_attempt.get('ok'):
            fallback_model = api.find_remote_model_by_id(api_key, app_id, remote_source='community')
            if fallback_model is None:
                fallback_model = api.find_remote_model_by_id(api_key, app_id, remote_source='official')
            id_diagnosis = api.diagnose_remote_numeric_id(api_key, app_id)
        versions: list[dict[str, Any]] = []
        if isinstance(detail, dict):
            versions = api.extract_result_data(detail).get('versions', []) or []
        elif fallback_model:
            versions = fallback_model.get('versions', []) or []
        version_id = versions[0].get('id') if versions else None
        version_detail = api.fetch_remote_version_detail(api_key, version_id) if version_id is not None else None
        effective_detail = detail
    workflow_raw = api.fetch_remote_workflow(api_key, version_id) if include_workflow and version_id is not None else None
    if include_workflow and (not api.extract_result_data(workflow_raw)):
        workflow_raw = api.fetch_remote_workflow_from_url(api.extract_result_data(webapp_detail_attempt).get('web_app_workflow_url'))
    # fetch_remote_workflow returns an {ok, status, data} envelope; summary/contract need the inner data.
    # Passing the envelope straight into collect_workflow_summary previously made node_count always 0.
    workflow = api.extract_result_data(workflow_raw) or workflow_raw
    execution_target = resolve_remote_execution_target(api_key, effective_detail, webapp_detail_attempt, version_detail, fallback_model=fallback_model)
    effective_webapp_detail = execution_target.get('webapp_detail') or webapp_detail_attempt
    resolved_contract = contract.resolve_remote_input_contract(execution_target=execution_target, webapp_detail=effective_webapp_detail, workflow=workflow)
    summary = build_remote_info_summary(bizy_model_id=str(app_id), detail_result=effective_detail, webapp_detail=effective_webapp_detail, fallback_model=fallback_model, version_detail=version_detail, workflow=workflow, execution_target=execution_target, resolved_contract=resolved_contract)
    summary['workflow_summary_included'] = bool(include_workflow)
    if not include_workflow:
        # Do not fake node_count=0 when the workflow was not requested, so the user does not read it as a parse failure.
        summary['workflow_summary_status'] = 'not_requested'
    out: dict[str, Any] = {
        'source': 'remote',
        'auth_mode': 'api_key_only',
        'summary': summary,
        'raw': {
            'detail': original_detail if detail_attempt.get('ok') else None,
            'detail_error': None if detail_attempt.get('ok') else {'status': detail_attempt.get('status'), 'error': detail_attempt.get('error')},
            'effective_detail_source': 'webapp_app_detail' if prefer_webapp else 'model_detail',
            'id_collision': id_collision,
            'id_diagnosis': id_diagnosis,
            'webapp_detail': api.unwrap_result_payload(effective_webapp_detail) if api.result_is_ok(effective_webapp_detail) else None,
            'webapp_detail_error': None if not isinstance(webapp_detail_attempt, dict) or webapp_detail_attempt.get('ok') else {'status': webapp_detail_attempt.get('status'), 'error': webapp_detail_attempt.get('error')},
            'fallback_model': fallback_model,
            'version_detail': version_detail,
            'execution_target': execution_target,
            'resolved_contract': resolved_contract,
        },
    }
    if include_workflow:
        out['raw']['resolved_workflow'] = workflow_raw
    return out

def resolve_remote_prefill_target(target: str, *, api_key_arg: str | None=None) -> dict[str, Any]:
    resolved = resolve_info_target(target)
    if not resolved.get('ok'):
        return resolved
    if resolved.get('input_kind') == 'workflow_link':
        return {'ok': False, 'error': 'WORKFLOW_LINK_NOT_SUPPORTED', 'input_kind': 'workflow_link', 'input_value': resolved.get('input_value'), 'draft_id': resolved.get('resolved_object_id'), 'message': common.WORKFLOW_LINK_NOT_SUPPORTED_MESSAGE}
    api_key = api.resolve_api_key(api_key_arg)
    original_object_id = str(resolved['resolved_object_id'])
    final_object_id = original_object_id
    return {'ok': True, 'input_kind': resolved.get('input_kind'), 'input_value': resolved.get('input_value'), 'normalized_url': resolved.get('normalized_url'), 'original_object_id': original_object_id, 'resolved_object_id': final_object_id, 'draft_resolution': None}

def match_remote_field_override(field_name: str, args: argparse.Namespace) -> Any:
    low = str(field_name or '').strip().lower()
    if not low:
        return None
    seed = getattr(args, 'seed', None)
    random_seed = bool(getattr(args, 'random_seed', False))
    steps = getattr(args, 'steps', None)
    duration = getattr(args, 'duration', None)
    width = getattr(args, 'width', None)
    height = getattr(args, 'height', None)
    aspect_ratio = getattr(args, 'aspect_ratio', None)
    resolution = getattr(args, 'resolution', None)
    model_name = getattr(args, 'model_name', None)
    if seed is not None and contract.is_remote_logical_field('seed', low):
        return int(seed)
    if random_seed and contract.is_remote_logical_field('seed', low):
        return api.generate_seed()
    if steps is not None and contract.is_remote_logical_field('steps', low):
        return int(steps)
    if duration is not None and contract.is_remote_logical_field('duration', low):
        return int(duration)
    if width is not None and contract.is_remote_logical_field('width', low):
        return int(width)
    if height is not None and contract.is_remote_logical_field('height', low):
        return int(height)
    if aspect_ratio is not None and contract.is_remote_logical_field('aspect_ratio', low):
        return aspect_ratio
    if resolution is not None and contract.is_remote_logical_field('resolution', low):
        return resolution
    if model_name is not None and contract.is_remote_logical_field('model_name', low):
        return model_name
    return None

def collect_hidden_remote_media_slots(webapp_detail_data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {'image': [], 'audio': [], 'video': []}
    for node in webapp_detail_data.get('input_nodes', []) or []:
        media_type = contract.classify_remote_media_slot(node)
        if not media_type:
            continue
        if str(node.get('field_type') or '').lower() != 'hidden':
            continue
        if node.get('field_value') in (None, '', [], {}):
            continue
        result[media_type].append(node)
    return result

def collect_raw_param_override_keys(args: argparse.Namespace) -> set[str]:
    keys: set[str] = set()
    for item in getattr(args, 'param', None) or []:
        if '=' not in item:
            continue
        (key, _) = item.split('=', 1)
        normalized = str(key).strip().lower()
        if normalized:
            keys.add(normalized)
    return keys

def summarize_missing_remote_media(preflight: dict[str, Any], args: argparse.Namespace, uploaded_urls: list[str] | None=None, uploaded_audios: list[str] | None=None, uploaded_videos: list[str] | None=None) -> dict[str, Any]:
    execution_target = preflight.get('execution_target') or {}
    runtime_webapp_detail = execution_target.get('webapp_detail') or preflight.get('webapp_detail')
    webapp_detail_data = api.extract_result_data(runtime_webapp_detail)
    resolved_contract = preflight.get('resolved_contract') or {}
    hidden_slots = collect_hidden_remote_media_slots(webapp_detail_data) if webapp_detail_data else {'image': [], 'audio': [], 'video': []}
    contract_media_slots = (resolved_contract.get('bindings') or {}).get('media_slots') or {}
    override_keys = collect_raw_param_override_keys(args)
    generic_override_counts = {'image': int(any((k in override_keys for k in {'image', 'images'}))), 'audio': int(any((k in override_keys for k in {'audio', 'audios'}))), 'video': int(any((k in override_keys for k in {'video', 'videos'})))}
    provided_counts = {'image': len(uploaded_urls or []), 'audio': len(uploaded_audios or []), 'video': len(uploaded_videos or [])}
    missing: dict[str, Any] = {}
    for media_type in ['image', 'audio', 'video']:
        slots = list(hidden_slots.get(media_type, []) or [])
        contract_slots = list(contract_media_slots.get(media_type, []) or [])
        exact_override_count = 0
        for node in slots:
            variable_name = str(node.get('variable_name') or '').strip().lower()
            if variable_name and variable_name in override_keys:
                exact_override_count += 1
        total_provided = provided_counts.get(media_type, 0) + exact_override_count + generic_override_counts.get(media_type, 0)
        required_count = max(len(slots), len(contract_slots))
        if required_count > total_provided:
            missing[media_type] = {'required': required_count, 'provided': total_provided, 'missing': required_count - total_provided, 'variables': [str(node.get('variable_name') or '') for node in slots] or contract_slots}
    return missing
