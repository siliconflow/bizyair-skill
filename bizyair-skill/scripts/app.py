"""app.py — CLI entry point for BizyAir remote objects.

Every CLI that actually calls the BizyAir API lives here. dispatch.py is only a
wrapper; underneath it shells out to this file.

Entry points:
  --check / --wallet                       account check + balance
  --browse / --search                      list / keyword search
  --pick-image-candidates / --pick-video-... pick model candidates (for the LLM to choose)
  --info / --info-from-link                remote object metadata + contract (full debug
                                            payload by default trimmed, see
                                            _strip_info_debug_payload)
  --prefill-card <link-or-id>              build a confirmation card (single renderer, used by
                                            the 10 fixed menu models and any remote app)
  --run <app-id>                           actually submit a run (create task, poll, download)

api.py / remote.py / contract.py are the muscles behind app.py; this file mainly does
CLI parsing + routing.
"""
from __future__ import annotations
import argparse, json, sys, time
from typing import Any

import api
import remote
import search
import common
from common import WALLET_URL, WEBAPP_RETRY_LIMIT

def _wallet_request(key: str, *, attempts: int = 3) -> dict:
    """Wallet GET is idempotent: only transport-layer failures get bounded retries;
    HTTP errors are returned immediately.

    On the current network urllib intermittently raises "Connection reset by peer"; a
    single blip should not make check / wallet fail outright. If retries still fail,
    report the error as usual without masking a real outage.
    """
    resp = {}
    for attempt in range(attempts):
        resp = api.request_json('GET', WALLET_URL, key)
        if api.result_is_ok(resp):
            return resp
        if isinstance(resp, dict) and resp.get('status') is not None:
            return resp
        if attempt < attempts - 1:
            time.sleep(0.8 * (attempt + 1))
    return resp

def _wallet_amount(data: dict, *keys: str):
    for key in keys:
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None

def _wallet_total(data: dict):
    """Balance aggregation: prefer charge + gift; fall back to total only when missing.

    The international and mainland sites expose different fields: when gift_balance=0
    and charge_balance>0, the old logic read gift_balance first, got 0, and misjudged
    it as no_balance.
    """
    charge = _wallet_amount(data, 'charge_balance_amount', 'charge_balance')
    gift = _wallet_amount(data, 'gift_balance_amount', 'gift_balance')
    if charge is not None or gift is not None:
        return (charge or 0) + (gift or 0)
    return _wallet_amount(data, 'total_balance_amount', 'total_balance')

def validate_raw_param_pairs(args: argparse.Namespace) -> None:
    issues = []
    for item in getattr(args, 'param', None) or []:
        text = str(item)
        if '=' not in text:
            issues.append({'param': text, 'error': 'MISSING_EQUALS', 'message': f'--param expects key=value form, got {text!r}'})
            continue
        key = text.split('=', 1)[0].strip()
        if not key:
            issues.append({'param': text, 'error': 'EMPTY_PARAM_NAME', 'message': f'--param key must not be empty, got {text!r}'})
    if issues:
        print(json.dumps({'error': 'INVALID_PARAM', 'message': issues[0]['message'], 'issues': issues}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)

def _remote_info_missing(out: dict[str, Any]) -> bool:
    identity = ((out or {}).get('summary') or {}).get('identity') or {}
    raw = (out or {}).get('raw') or {}
    if raw.get('fallback_model'):
        return False
    return not any(identity.get(key) for key in ('name', 'version_id', 'web_app_id', 'execution_web_app_id'))

def _remote_info_transport_error(out: dict[str, Any]) -> bool:
    raw = (out or {}).get('raw') or {}
    for key in ('detail_error', 'webapp_detail_error'):
        error = raw.get(key)
        if isinstance(error, dict) and error.get('status') is None and error.get('error'):
            return True
    return False

def ensure_remote_info_found(out: dict[str, Any], target: str) -> None:
    # Invalid/expired keys are not "object missing". Check 401/403 first so users are not
    # told to verify the link/ID when the real problem is authentication.
    auth = api.auth_error_from_raw(out.get('raw') if isinstance(out, dict) else None)
    if auth:
        print(json.dumps({
            'error': 'AUTH_FAILED',
            'message': 'The API key is invalid or expired; check it and configure it again.',
            'target': str(target),
            'http_status': auth.get('status'),
            'source': auth.get('source'),
        }, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    if not _remote_info_missing(out):
        return
    transport = _remote_info_transport_error(out)
    print(json.dumps({
        'error': 'REMOTE_OBJECT_NOT_FOUND' if not transport else 'REMOTE_REQUEST_FAILED',
        'message': (f'No BizyAir object found for {target}; check that the link or ID is correct.' if not transport else f'Network request failed while looking up BizyAir object {target}; retry later.'),
        'target': str(target),
    }, ensure_ascii=False), file=sys.stderr)
    sys.exit(1 if transport else 2)

def _invalid_input(code: str, message: str, **extra: Any) -> None:
    payload: dict[str, Any] = {'error': code, 'message': message}
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    sys.exit(2)

def _positive_int(value: Any, flag: str, *, default: int | None=None) -> int:
    """Validate --page / --page-size / --limit locally instead of silently clamping negatives to a default."""
    if value is None:
        if default is None:
            _invalid_input('INVALID_PARAM', f'{flag} requires a positive integer')
        return int(default)
    try:
        number = int(value)
    except (TypeError, ValueError):
        _invalid_input('INVALID_PARAM', f'{flag} must be an integer >= 1', given=value)
    if number < 1:
        _invalid_input('INVALID_PARAM', f'{flag} must be an integer >= 1', given=value)
    return number

def _validated_remote_query(*, modality: str | None, sort: str | None, remote_source: str, page: Any, page_size: Any) -> tuple[str | None, str, int, int]:
    source = common.normalized_text(remote_source) or 'community'
    if source not in {'community', 'official'}:
        _invalid_input('INVALID_PARAM', '--remote-source only supports community or official', given=remote_source)
    model_types = remote.normalize_remote_type(modality)
    if modality is not None and model_types not in remote.SUPPORTED_REMOTE_TYPES:
        _invalid_input(
            'TYPE_NOT_SUPPORTED',
            '--modality is not a supported BizyAir object type.',
            given=modality,
            supported=sorted(remote.SUPPORTED_REMOTE_TYPES),
        )
    resolved_sort = remote.resolve_list_sort(sort, remote_source=source, default='Recently')
    if resolved_sort not in remote.SUPPORTED_SORTS:
        _invalid_input(
            'SORT_NOT_SUPPORTED',
            'The public list supports only --sort "Most Used" / Recently / "Most Liked" / Auto.',
            given=sort,
            supported=sorted(remote.SUPPORTED_SORTS),
        )
    return (model_types, resolved_sort, _positive_int(page, '--page', default=1), _positive_int(page_size, '--page-size', default=10))

def cmd_check(api_key_arg: str | None):
    key = api.resolve_api_key(api_key_arg)
    if not key:
        print(json.dumps({'status': 'no_key', 'message': 'No BizyAir API key configured', 'supported_inputs': {'cli': '--api-key YOUR_BIZYAIR_KEY', 'env': 'BIZYAIR_API_KEY', 'config': f'{common.config_display_path()} -> credentials.api_key'}, 'recommended': f'Save the key to {common.config_display_path()} at credentials.api_key'}, ensure_ascii=False))
        sys.exit(1)
    # Placeholders are already filtered into no_key inside resolve_api_key; once we have a
    # candidate key, the server response is still the source of truth.
    resp = _wallet_request(key)
    http_status = (resp or {}).get('status')
    if not api.result_is_ok(resp):
        # Only 401/403 mean the key is invalid; everything else (network/5xx) must surface
        # as-is and must not masquerade as invalid_key.
        if http_status in (401, 403):
            print(json.dumps({'status': 'invalid_key', 'key_prefix': key[:4] + '****', 'source': api.get_key_source(api_key_arg), 'message': 'The API key is invalid or expired; check it and configure it again', 'http_status': http_status}, ensure_ascii=False))
            sys.exit(1)
        print(json.dumps({'status': 'request_failed', 'error': 'REMOTE_REQUEST_FAILED', 'key_prefix': key[:4] + '****', 'source': api.get_key_source(api_key_arg), 'http_status': http_status, 'message': api.request_error_message(resp)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    # Success envelope: {ok: True, status: 200, data: {code: 20000, data: {wallet}}}
    inner = resp.get('data') if isinstance(resp, dict) else {}
    biz_code = inner.get('code') if isinstance(inner, dict) else None
    if biz_code != 20000:
        print(json.dumps({'status': 'invalid_key', 'key_prefix': key[:4] + '****', 'source': api.get_key_source(api_key_arg), 'message': 'The API key is invalid or expired; check it and configure it again', 'http_status': http_status, 'server_code': biz_code}, ensure_ascii=False))
        sys.exit(1)
    data = api.extract_result_data(resp)
    total_amount = _wallet_total(data)
    if isinstance(total_amount, (int, float)) and total_amount <= 0:
        print(json.dumps({'status': 'no_balance', 'key_prefix': key[:4] + '****', 'source': api.get_key_source(api_key_arg), 'message': 'The API key is valid but the balance is 0; top up before use'}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps({'status': 'ok', 'key_prefix': key[:4] + '****', 'source': api.get_key_source(api_key_arg)}, ensure_ascii=False))

def cmd_wallet(api_key_arg: str | None):
    api_key = api.require_api_key(api_key_arg)
    resp = _wallet_request(api_key)
    if not api.result_is_ok(resp):
        # Never print "balance retrieved" on failure.
        print(json.dumps({
            'status': 'request_failed',
            'error': 'REMOTE_REQUEST_FAILED',
            'source': api.get_key_source(api_key_arg),
            'http_status': resp.get('status') if isinstance(resp, dict) else None,
            'message': api.request_error_message(resp),
        }, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    source = api.get_key_source(api_key_arg)
    data = api.extract_result_data(resp)
    # The .cn wallet returns total / charge / gift; .ai currently returns only
    # gift_balance. Aggregate charge + gift here so a zero gift is not misread as a
    # zero balance.
    total_amount = _wallet_total(data)
    if isinstance(total_amount, (int, float)):
        if total_amount >= 50000:
            verdict = 'Very healthy — go ahead, that is serious firepower~'
        elif total_amount >= 10000:
            verdict = 'Still comfortable; a normal run for a while should be fine~'
        elif total_amount >= 3000:
            verdict = 'It can hold for a bit, but do not go wild — keep an eye on the balance.'
        else:
            verdict = 'Getting tight; top up soon so a run does not stall halfway through.'
    else:
        verdict = 'Balance fields are incomplete; hold off on running tasks for now and retry later or check the endpoint response.'
    charge_display = _wallet_amount(data, 'charge_balance', 'charge_balance_amount')
    gift_display = _wallet_amount(data, 'gift_balance', 'gift_balance_amount')
    total_display = total_amount if isinstance(total_amount, (int, float)) else None

    # Raw API values are in credits; USD converts at 1,000 credits = $1.
    # *_display keeps the old numeric / "—" semantics; *_text / *_usd are new display text.
    charge_credits = common.format_credits(charge_display)
    gift_credits = common.format_credits(gift_display)
    total_credits = common.format_credits(total_display)
    charge_usd = common.format_usd(charge_display)
    gift_usd = common.format_usd(gift_display)
    total_usd = common.format_usd(total_display)
    charge_text = common.format_credits_and_usd(charge_display) or '—'
    gift_text = common.format_credits_and_usd(gift_display) or '—'
    total_text = common.format_credits_and_usd(total_display) or '—'
    summary = (
        f"Available balance {total_usd or '—'} ({total_credits or '—'}; "
        f"charge {charge_credits or '—'}, gift {gift_credits or '—'})"
    )
    print(json.dumps({
        'status': 'ok',
        'source': source,
        'wallet': {
            'charge_balance': charge_display,
            'gift_balance': gift_display,
            'total_balance': total_display,
            'charge_balance_amount': charge_display,
            'gift_balance_amount': gift_display,
            'total_balance_amount': total_display,
            'charge_display': charge_display if charge_display is not None else '—',
            'gift_display': gift_display if gift_display is not None else '—',
            'total_display': total_display if total_display is not None else '—',
            'charge_text': charge_text,
            'gift_text': gift_text,
            'total_text': total_text,
            'charge_usd': charge_usd,
            'gift_usd': gift_usd,
            'total_usd': total_usd,
            'unit': 'credits',
            'credits_per_usd': int(common.CREDITS_PER_USD),
        },
        'summary': summary,
        'verdict': verdict,
        'raw': resp,
    }, ensure_ascii=False, indent=2))

def cmd_browse(modality: str | None=None, stability: str | None=None, *, api_key_arg: str | None=None, remote_source: str='community', page: int=1, page_size: int=10, base_models: str | None=None, sort: str | None=None):
    # The list endpoint is public; an invalid key would 401, so no key is enforced here.
    api_key = api_key_arg or ''
    model_types, resolved_sort, page, page_size = _validated_remote_query(
        modality=modality, sort=sort, remote_source=remote_source, page=page, page_size=page_size,
    )
    resp = api.fetch_remote_models(api_key, remote_source=remote_source, page=page, page_size=page_size, keyword='', sort=resolved_sort, model_types=model_types, base_models=base_models)
    payload = {'source': 'remote', 'remote_source': remote_source, 'query': {'page': page, 'page_size': page_size, 'model_types': model_types, 'base_models': base_models, 'sort': resolved_sort}, 'result': resp}
    api.finish_remote_result(resp, payload, action='browse')

def cmd_search(query: str, *, api_key_arg: str | None=None, remote_source: str='community', page: int=1, page_size: int=10, modality: str | None=None, sort: str | None=None, base_models: str | None=None, compact: bool=False, limit: int=10):
    # The list endpoint is public; an invalid key would 401, so no key is enforced here.
    api_key = api_key_arg or ''
    model_types, resolved_sort, page, page_size = _validated_remote_query(
        modality=modality, sort=sort, remote_source=remote_source, page=page, page_size=page_size,
    )
    limit = _positive_int(limit, '--limit', default=10)
    semantic_plan = search.build_semantic_plan(query, modality)
    primary_keyword = (semantic_plan.get('search_queries') or [query])[0]
    resp = api.fetch_remote_models(api_key, remote_source=remote_source, page=page, page_size=page_size, keyword=primary_keyword, sort=resolved_sort, model_types=model_types, base_models=base_models)
    payload = {'source': 'remote', 'remote_source': remote_source, 'query': {'keyword': primary_keyword, 'page': page, 'page_size': page_size, 'model_types': model_types, 'base_models': base_models, 'sort': resolved_sort}, 'semantic_plan': semantic_plan, 'platform_results': [], 'candidates': [], 'result': resp}
    if not api.result_is_ok(resp):
        api.finish_remote_result(resp, payload, action='search')
    items = api.extract_result_data(resp).get('list', [])
    platform_results = search.format_remote_candidates(items, semantic_plan, common.normalized_text(modality) if modality else None)
    payload['platform_results'] = platform_results
    payload['candidates'] = platform_results
    if compact:
        print(search.compact_candidates_markdown(platform_results[:limit], keyword=primary_keyword))
        return
    api.finish_remote_result(resp, payload, action='search')

def cmd_info(app_id: str, *, remote: bool=False, api_key_arg: str | None=None, include_workflow: bool=False):
    # Note: the parameter name `remote` shadows the `remote` module imported at the top;
    # calling `remote.build_remote_info_output(...)` directly raises AttributeError.
    # Fetch the module explicitly through globals() to avoid the shadow.
    if remote:
        remote_module = globals()['remote']
        resolved = remote_module.resolve_info_target(app_id)
        if not resolved.get('ok'):
            print(json.dumps(resolved, ensure_ascii=False, indent=2), file=sys.stderr)
            sys.exit(2)
        out = remote_module.build_remote_info_output(str(resolved['resolved_object_id']), api_key_arg=api_key_arg, include_workflow=include_workflow)
        ensure_remote_info_found(out, str(resolved['resolved_object_id']))
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

def cmd_info_from_link(target: str, *, api_key_arg: str | None=None, include_workflow: bool=True, debug: bool=False):
    """`--info-from-link X`. Print the full info JSON for the remote object.
    debug=False (default): drop the result.raw branch + every fields[].raw_node child
    (fields[].field_value already holds the raw values), cutting ~60% of the bytes.
    debug=True: keep everything for development debugging.
    cmd_prefill_card also calls build_remote_info_output internally, but in-process rather
    than through stdout, so it is unaffected by the strip here.
    """
    resolved = remote.resolve_info_target(target)
    if not resolved.get('ok'):
        print(json.dumps(resolved, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(2)
    if resolved.get('input_kind') == 'workflow_link':
        print(json.dumps({'error': 'WORKFLOW_LINK_NOT_SUPPORTED', 'input_kind': 'workflow_link', 'input_value': resolved.get('input_value'), 'draft_id': resolved.get('resolved_object_id'), 'message': common.WORKFLOW_LINK_NOT_SUPPORTED_MESSAGE}, ensure_ascii=False, indent=2))
        sys.exit(2)
    api_key = api.resolve_api_key(api_key_arg)
    original_object_id = str(resolved['resolved_object_id'])
    final_object_id = original_object_id
    out = remote.build_remote_info_output(final_object_id, api_key_arg=api_key_arg, include_workflow=include_workflow)
    ensure_remote_info_found(out, final_object_id)
    out_for_print = out if debug else _strip_info_debug_payload(out)
    identity = ((out.get('summary') or {}).get('identity') or {}) if isinstance(out, dict) else {}
    page_url = common.object_page_url(identity.get('type'), final_object_id, version_id=identity.get('web_app_id') or identity.get('version_id'))
    execution_object_id = str(identity.get('version_id') or identity.get('bizy_model_version_id') or final_object_id) if str(identity.get('type') or '') == 'Application' else str(final_object_id)
    print(json.dumps({'source': 'remote-info-from-link', 'page_url': page_url, 'page_object_id': str(identity.get('web_app_id') or identity.get('version_id') or final_object_id) if str(identity.get('type') or '') == 'Application' else str(final_object_id), 'execution_object_id': execution_object_id, 'input_kind': resolved.get('input_kind'), 'input_value': resolved.get('input_value'), 'normalized_url': resolved.get('normalized_url'), 'original_object_id': original_object_id, 'resolved_object_id': final_object_id, 'result': out_for_print}, ensure_ascii=False, indent=2))


def _strip_info_debug_payload(out: dict[str, Any]) -> dict[str, Any]:
    """Return a deep-copied view of build_remote_info_output() with raw branch / raw_node trimmed.

    - Drops top-level `out['raw']` (debug-only branch; ~50% of bytes).
    - Recursively drops every `raw_node` key inside `summary` (each contract field
      currently carries a self-duplicating raw_node copy; appears under both
      `summary.resolved_contract.fields[]` and `summary.executable_draft.resolved_contract.fields[]`).

    Caller-side cosmetic stripping only — does not mutate the original `out`,
    so other consumers (cmd_prefill_card) keep getting the full structure.
    """
    if not isinstance(out, dict):
        return out

    def _scrub(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: _scrub(v) for k, v in node.items() if k != 'raw_node'}
        if isinstance(node, list):
            return [_scrub(v) for v in node]
        return node

    trimmed = {k: v for k, v in out.items() if k != 'raw'}
    summary = trimmed.get('summary')
    if isinstance(summary, dict):
        trimmed['summary'] = _scrub(summary)
    return trimmed

def cmd_prefill_card(target: str, args: argparse.Namespace):
    validate_raw_param_pairs(args)
    target_info = remote.resolve_remote_prefill_target(target, api_key_arg=args.api_key)
    if not target_info.get('ok'):
        print(json.dumps(target_info, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(2)
    out = remote.build_remote_info_output(str(target_info['resolved_object_id']), api_key_arg=args.api_key, include_workflow=True)
    ensure_remote_info_found(out, str(target_info['resolved_object_id']))
    summary = out.get('summary') or {}
    raw = out.get('raw') or {}
    execution_target = raw.get('execution_target') or {}
    webapp_detail_data = execution_target.get('webapp_detail') or raw.get('webapp_detail') or {}
    if isinstance(webapp_detail_data, dict) and isinstance(webapp_detail_data.get('data'), dict):
        webapp_detail_data = webapp_detail_data.get('data', {})
    print(remote.build_remote_prefilled_card(summary, args, input_kind=target_info.get('input_kind'), original_object_id=target_info.get('original_object_id'), resolved_object_id=target_info.get('resolved_object_id'), draft_resolution=target_info.get('draft_resolution'), webapp_detail_data=webapp_detail_data))
    return

def cmd_run(args: argparse.Namespace):
    validate_raw_param_pairs(args)
    api_key = api.resolve_api_key(args.api_key)
    raw_app = str(args.app or '').strip()
    resolved_app = api.extract_remote_target_id(raw_app)
    if resolved_app is None:
        print(json.dumps({'error': 'REMOTE_TARGET_UNRECOGNIZED', 'message': raw_app, 'detail': 'I cannot tell which object this input refers to, so nothing was submitted. Provide a numeric ID, or the full link to the community/app page.', 'accepted_forms': ['57862', 'https://www.bizyair.ai/community/app/57862', 'https://www.bizyair.ai/.../webapp/57862', '...?app_id=57862'], 'hint': 'I extract the object ID from links automatically; no need to trim it yourself.'}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(2)
    if resolved_app != raw_app:
        print(f'Note: extracted object ID `{resolved_app}` from the input.', file=sys.stderr)
    raw_app = resolved_app
    if raw_app.isdigit():
        if not api_key:
            print(json.dumps({'error': 'NO_REMOTE_CREDENTIAL', 'message': 'Remote app run preflight requires api key', 'supported_inputs': {'api_key': f'--api-key / BIZYAIR_API_KEY / {common.config_display_path()} -> credentials.api_key'}}, ensure_ascii=False), file=sys.stderr)
            sys.exit(1)
        preflight = remote.preflight_remote_app_run(api_key, raw_app)
        id_diagnosis = api.diagnose_remote_numeric_id(api_key, raw_app)
        uploaded_urls = [api.upload_input_file(api_key, p) for p in args.image or []]
        uploaded_audios = [api.upload_input_file(api_key, p) for p in args.audio or []]
        uploaded_videos = [api.upload_input_file(api_key, p) for p in args.video or []]
        preflight_summary = remote.build_remote_info_summary(bizy_model_id=str(raw_app), detail_result=preflight.get('detail'), webapp_detail=preflight.get('webapp_detail'), fallback_model=None, version_detail=preflight.get('version_detail'), workflow=preflight.get('workflow'), execution_target=preflight.get('execution_target'), resolved_contract=preflight.get('resolved_contract'))
        param_issues = api.validate_remote_param_keys(preflight, args)
        if param_issues:
            print(json.dumps({'error': 'AMBIGUOUS_PARAM', 'message': param_issues[0]['message'], 'issues': param_issues, 'submitted': False}, ensure_ascii=False, indent=2), file=sys.stderr)
            sys.exit(2)
        execution_target = preflight.get('execution_target') or {}
        runtime_webapp = api.extract_result_data(execution_target.get('webapp_detail'))
        if not execution_target.get('supported'):
            print(json.dumps({'error': 'REMOTE_APP_RUN_NOT_SUPPORTED', 'message': raw_app, 'detail': execution_target.get('message') or 'The parameters and description were retrieved, but this object cannot be run on your behalf right now. Take the prepared parameters to the web page to run it.', 'reason': execution_target.get('reason'), 'support_scope': execution_target.get('support_scope'), 'preflight': {'id_diagnosis': id_diagnosis, 'summary': preflight_summary}}, ensure_ascii=False, indent=2), file=sys.stderr)
            sys.exit(1)
        if not runtime_webapp.get('id'):
            print(json.dumps({'error': 'REMOTE_APP_RUN_NOT_SUPPORTED', 'message': raw_app, 'detail': 'I have the parameters and description for this object, but the backend execution channel is not fully wired up yet. To avoid a bad run, I will not submit it this round; we can try again once the route is open!', 'reason': 'missing_execution_webapp_id', 'preflight': {'id_diagnosis': id_diagnosis, 'summary': preflight_summary}}, ensure_ascii=False, indent=2), file=sys.stderr)
            sys.exit(1)
        missing_media = remote.summarize_missing_remote_media(preflight, args, uploaded_urls=uploaded_urls, uploaded_audios=uploaded_audios, uploaded_videos=uploaded_videos)
        if missing_media:
            print(json.dumps({'error': 'REMOTE_APP_RUN_REQUIRES_USER_MEDIA', 'message': raw_app, 'detail': 'This app ships with on-site demo media, but a run must use your own media; the on-site media cannot be reused as-is.', 'reason': 'station_demo_media_must_be_replaced', 'missing_inputs': missing_media, 'classification': {'bucket': 'user_or_material', 'reason': 'missing_user_media', 'retryable': False}, 'preflight': {'id_diagnosis': id_diagnosis, 'summary': preflight_summary}}, ensure_ascii=False, indent=2), file=sys.stderr)
            sys.exit(1)
        final_attempt = None
        last_poll_result = None
        for retry_index in range(WEBAPP_RETRY_LIMIT + 1):
            attempt = api.create_remote_task_attempt(api_key, raw_app, preflight, args, uploaded_urls=uploaded_urls, uploaded_audios=uploaded_audios, uploaded_videos=uploaded_videos, use_async=not args.sync)
            final_attempt = attempt
            attempt_result = attempt.get('result') or {}
            nested_data = attempt_result.get('data', {}) if isinstance(attempt_result, dict) else {}
            if isinstance(nested_data, dict) and isinstance(nested_data.get('data'), dict):
                nested_data = nested_data.get('data', {})
            request_id = attempt_result.get('data', {}).get('requestId') or attempt_result.get('data', {}).get('request_id') or nested_data.get('requestId') or nested_data.get('request_id') or attempt_result.get('requestId') or attempt_result.get('request_id') if isinstance(attempt_result, dict) else None
            task_id = attempt_result.get('data', {}).get('task_id') or attempt_result.get('data', {}).get('taskId') or nested_data.get('task_id') or nested_data.get('taskId') or attempt_result.get('task_id') or attempt_result.get('taskId') if isinstance(attempt_result, dict) else None
            if request_id:
                print(f'REQUEST_ID:{request_id}')
                outputs_resp = api.poll_until_done(api_key, request_id)
                outputs = api.extract_result_data(outputs_resp).get('outputs', [])
                if not outputs:
                    print(json.dumps({'error': 'NO_OUTPUTS', 'detail': outputs_resp}, ensure_ascii=False), file=sys.stderr)
                    sys.exit(1)
                saved_paths = api.download_outputs(outputs, args.output)
                try:
                    api.write_run_manifest(
                        args.output,
                        request_id=request_id,
                        outputs=outputs,
                        saved_files=saved_paths,
                        slug=raw_app,
                        prompt=args.prompt,
                    )
                except Exception as exc:
                    print(json.dumps({'error': 'RUN_MANIFEST_WRITE_FAILED', 'message': str(exc), 'output': args.output, 'request_id': request_id}, ensure_ascii=False), file=sys.stderr)
                    sys.exit(1)
                api.emit_downloaded_outputs(outputs, saved_paths)
                return
            if task_id:
                print(f'TASK_ID:{task_id}')
                task_status = attempt_result.get('data', {}).get('task_status') or nested_data.get('task_status') or attempt_result.get('task_status')
                if task_status:
                    print(f'TASK_STATUS:{task_status}')
                ws_url = attempt_result.get('data', {}).get('ws_url') or nested_data.get('ws_url') or attempt_result.get('ws_url')
                if ws_url:
                    print(f'WS_URL:{ws_url}')
                return
            break
        print(json.dumps({'error': 'REMOTE_APP_RUN_ATTEMPT_FAILED', 'message': raw_app, 'detail': 'Remote app submit was attempted with currently confirmed fields. Service response attached for next payload alignment.', 'attempt': final_attempt, 'last_poll_result': last_poll_result, 'preflight': {'auth_mode': 'api_key_only', 'bizy_model_id': preflight.get('bizy_model_id'), 'version_id': preflight.get('version_id'), 'detail_ok': api.result_is_ok(preflight.get('detail')), 'webapp_detail_ok': api.result_is_ok(preflight.get('webapp_detail')), 'version_detail_ok': api.result_is_ok(preflight.get('version_detail')), 'workflow_ok': api.result_is_ok(preflight.get('workflow')), 'id_diagnosis': id_diagnosis, 'summary': preflight_summary, 'detail': api.unwrap_result_payload(preflight.get('detail')), 'webapp_detail': api.unwrap_result_payload(preflight.get('webapp_detail')), 'version_detail': api.unwrap_result_payload(preflight.get('version_detail')), 'workflow': api.unwrap_result_payload(preflight.get('workflow'))}}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)

def _validated_pick_args(args: argparse.Namespace) -> tuple[str, int, int, int]:
    source = common.normalized_text(args.remote_source) or 'community'
    if source not in {'community', 'official'}:
        _invalid_input('INVALID_PARAM', '--remote-source only supports community or official', given=args.remote_source)
    sort = remote.resolve_list_sort(args.sort, remote_source=source, default='Most Used')
    if sort not in remote.SUPPORTED_SORTS:
        _invalid_input(
            'SORT_NOT_SUPPORTED',
            'The public list supports only --sort "Most Used" / Recently / "Most Liked" / Auto.',
            given=args.sort,
            supported=sorted(remote.SUPPORTED_SORTS),
        )
    return (
        sort,
        _positive_int(args.page, '--page', default=1),
        _positive_int(args.page_size, '--page-size', default=10),
        _positive_int(args.limit, '--limit', default=10),
    )

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description='BizyAir curated AI app client')
    p.add_argument('--api-key')
    p.add_argument('--check', action='store_true')
    p.add_argument('--wallet', action='store_true')
    p.add_argument('--browse', action='store_true')
    p.add_argument('--search')
    p.add_argument('--pick-image-candidates')
    p.add_argument('--pick-video-candidates')
    p.add_argument('--modality')
    p.add_argument('--stability')
    p.add_argument('--info')
    p.add_argument('--info-from-link', help='Accept a BizyAir app/workflow link or raw numeric id and return remote info')
    p.add_argument('--prefill-card', help='Accept a BizyAir app/workflow link or raw numeric id and return a remote prefilled confirmation card')
    p.add_argument('--remote', action='store_true', help='Use API-key-backed remote discovery/detail')
    p.add_argument('--remote-source', choices=['community', 'official'], default='community')
    p.add_argument('--page', type=int, default=1)
    p.add_argument('--page-size', type=int, default=10)
    p.add_argument('--reply-format', choices=['json', 'markdown'], default='json')
    p.add_argument('--compact', action='store_true', help='When searching, output only a compact Markdown table (saves context)')
    p.add_argument('--limit', type=int, default=10, help='Top-N candidates returned by picker commands. Default 10. Use a smaller value (e.g. 3) for concise recommendations or a larger value (e.g. 20) when user asks to inventory all available models.')
    p.add_argument('--sort')
    p.add_argument('--base-models')
    p.add_argument('--with-workflow', action='store_true', help='When used with --info --remote, also resolve the first version workflow JSON')
    p.add_argument('--run', dest='app')
    p.add_argument('--prompt')
    p.add_argument('--image', action='append')
    p.add_argument('--audio', action='append')
    p.add_argument('--video', action='append')
    p.add_argument('--aspect-ratio')
    p.add_argument('--resolution')
    p.add_argument('--steps', type=int)
    p.add_argument('--duration', type=int)
    p.add_argument('--seed', type=int)
    p.add_argument('--random-seed', action='store_true')
    p.add_argument('--model-name')
    p.add_argument('--width', type=int)
    p.add_argument('--height', type=int)
    p.add_argument('--param', action='append', help='Extra raw input_values override: key=value')
    p.add_argument('-o', '--output')
    p.add_argument('--sync', action='store_true', help='Use sync mode instead of async polling mode')
    p.add_argument('--debug', action='store_true', help='Include the raw debug branch in --info-from-link / --info output')
    return p

def main():
    args = build_parser().parse_args()
    if args.check:
        cmd_check(args.api_key)
        return
    if args.wallet:
        cmd_wallet(args.api_key)
        return
    if args.browse:
        cmd_browse(args.modality, args.stability, api_key_arg=args.api_key, remote_source=args.remote_source, page=args.page, page_size=args.page_size, base_models=args.base_models, sort=args.sort)
        return
    if args.search:
        cmd_search(args.search, api_key_arg=args.api_key, remote_source=args.remote_source, page=args.page, page_size=args.page_size, modality=args.modality, sort=args.sort, base_models=args.base_models, compact=bool(args.compact), limit=args.limit)
        return
    if args.pick_image_candidates:
        sort, page, page_size, limit = _validated_pick_args(args)
        search.cmd_pick_image_candidates(args.pick_image_candidates, api_key_arg=args.api_key, remote_source=args.remote_source, page=page, page_size=page_size, sort=sort, base_models=args.base_models, limit=limit, reply_format=args.reply_format)
        return
    if args.pick_video_candidates:
        sort, page, page_size, limit = _validated_pick_args(args)
        search.cmd_pick_video_candidates(args.pick_video_candidates, api_key_arg=args.api_key, remote_source=args.remote_source, page=page, page_size=page_size, sort=sort, base_models=args.base_models, limit=limit, reply_format=args.reply_format)
        return
    if args.info_from_link:
        cmd_info_from_link(args.info_from_link, api_key_arg=args.api_key, include_workflow=True, debug=bool(getattr(args, 'debug', False)))
        return
    if args.prefill_card:
        cmd_prefill_card(args.prefill_card, args)
        return
    if args.info:
        cmd_info(args.info, remote=args.remote, api_key_arg=args.api_key, include_workflow=args.with_workflow)
        return
    if args.app:
        cmd_run(args)
        return
    build_parser().print_help()

if __name__ == "__main__":
    main()
