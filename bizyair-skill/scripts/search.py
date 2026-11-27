"""search.py — remote candidate search + fuzzy term expansion + relevance ranking + JSON/Markdown candidate replies.

Invoked by app.py --pick-image-candidates / --pick-video-candidates.

Main logic:
  - Accept "a few short words" from the user as the query (usually the LLM splits the user's sentence and feeds it in)
  - The server filters by Application + Workflow, so only apps/workflows are searched and LoRA/VAE/Checkpoint never mix in
  - Colloquial input is first expanded with synonyms/English terms, then searched with multiple keyword passes, merged and deduplicated
  - The client ranks fuzzily by name hit, base_model, and usage count
  - Reply format: reply_format='markdown' for the user, 'json' for the LLM to choose from

The search endpoint does literal keyword matching, not semantic search; expansion and ranking are what improve recall and precision.

The selection logic contains a lot of heuristics (classify_route_match / candidate_freshness_hint etc.) that are tightly
coupled to the specific business models; change them carefully.
"""
from __future__ import annotations
import json, math, re, sys, time
from typing import Any

import api
import common
from common import SEARCH_MAX_ROUNDS, SEARCH_MIN_ROUNDS

# The server only returns apps/workflows, so LoRA / VAE / Checkpoint do not crowd out candidate slots.
APP_SEARCH_MODEL_TYPES = ('Application', 'Workflow')

# Do not send these generic words to the server on their own during fallback term splitting, otherwise "lip sync" / "audio to video"
# would split into "video" / "lip" / "sync" and hit a pile of unrelated apps whose names happen to contain those substrings.
SEARCH_FALLBACK_STOPWORDS = {
    'a', 'an', 'the', 'to', 'of', 'for', 'and', 'with', 'in', 'on', 'by',
    'image', 'images', 'video', 'videos', 'audio', 'text', 'app', 'apps',
    'workflow', 'workflows', 'application', 'applications', 'lip', 'sync', 'lip-sync',
}

# Colloquial input -> English/synonym expansion. The search endpoint does literal matching, so without expansion
# the Chinese words for "retouch" / "lip-sync" easily return 0 hits. All non-ASCII entries are internal input
# aliases; never displayed.
CANDIDATE_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ('\u4fee\u56fe', '\u7f16\u8f91', 'edit', 'image edit', 'inpaint', 'retouch', '\u56fe\u50cf\u7f16\u8f91'),
    ('\u56fe\u751f\u56fe', 'image to image', 'img2img', '\u53c2\u8003\u56fe'),
    ('\u6362\u80cc\u666f', '\u80cc\u666f', 'background', 'remove background', '\u62a0\u56fe', 'cutout'),
    ('\u5bf9\u53e3\u578b', '\u53e3\u578b', 'lip sync', 'lip-sync', 'audio to video', 'talk'),
    ('\u56fe\u751f\u89c6\u9891', 'image to video', 'i2v', '\u7167\u7247\u52a8\u8d77\u6765'),
    ('\u6587\u751f\u89c6\u9891', 'text to video', 't2v'),
    ('\u9996\u5c3e\u5e27', '\u9996\u5e27', '\u5c3e\u5e27', 'first last frame', 'first-frame', 'last-frame', 'transition'),
    ('\u6362\u8138', 'face swap', 'faceswap', '\u4eba\u8138\u66ff\u6362'),
    ('\u6d77\u62a5', 'poster', 'cover', '\u5c01\u9762'),
    ('\u5546\u54c1\u56fe', '\u4ea7\u54c1\u56fe', '\u7535\u5546', 'product image', 'ecommerce', 'product'),
    ('\u8d5b\u535a\u670b\u514b', 'cyberpunk', 'neon', '\u8d5b\u535a'),
    ('\u52a8\u6f2b', 'anime', '\u4e8c\u6b21\u5143', '\u63d2\u753b'),
    ('\u5199\u771f', 'portrait', 'photorealistic', '\u5199\u5b9e'),
    ('\u653e\u5927', 'upscale', 'super resolution', '\u8d85\u5206', '\u9ad8\u6e05'),
    ('\u53bb\u6c34\u5370', 'watermark', 'remove watermark'),
    ('\u6269\u56fe', 'outpaint', 'uncrop'),
    ('kling', '\u53ef\u7075'),
    ('seedream', '\u5373\u68a6'),
    ('qwen', '\u901a\u4e49\u5343\u95ee'),
    ('wan', '\u4e07\u76f8'),
    ('minimax', '\u6d77\u87ba'),
    ('hunyuan', '\u6df7\u5143'),
)

def normalize_match_text(value: Any) -> str:
    """Normalize case, underscores, hyphens and punctuation for fuzzy matching."""
    text = common.normalized_text(value)
    text = re.sub(r'[_\-/]+', ' ', text)
    text = re.sub(r'[^\w\u4e00-\u9fff]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()

def match_tokens(value: Any) -> list[str]:
    return [token for token in normalize_match_text(value).split(' ') if token]

def edit_distance(left: str, right: str) -> int:
    """Small word-level Levenshtein, used to tolerate slight spelling differences."""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        for j, right_char in enumerate(right, start=1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (left_char != right_char)))
        previous = current
    return previous[-1]

def _contains_han(text: str) -> bool:
    return bool(re.search(r'[\u4e00-\u9fff]', str(text or '')))


def synonym_variants(term: str, *, allow_chinese: bool | None = None) -> list[str]:
    low = normalize_match_text(term)
    if not low:
        return []
    if allow_chinese is None:
        allow_chinese = _contains_han(term)
    variants: list[str] = []
    for group in CANDIDATE_SYNONYM_GROUPS:
        normalized_group = [normalize_match_text(item) for item in group]
        if any(key and key in low for key in normalized_group):
            for item in group:
                if not allow_chinese and _contains_han(item):
                    continue
                common.add_unique_key(variants, item)
    spaced = re.sub(r'[_\-]+', ' ', str(term or '')).strip()
    if spaced and spaced != str(term or '').strip():
        common.add_unique_key(variants, spaced)
    return [item for item in variants if normalize_match_text(item) != low]

def expand_candidate_search_terms(terms: list[str], modality: str | None=None) -> list[str]:
    """Keep the original terms first, then add Chinese/English synonyms, up to SEARCH_MAX_ROUNDS."""
    expanded: list[str] = []
    for term in terms:
        common.add_unique_key(expanded, term)
    allow_chinese = any(_contains_han(term) for term in terms)
    for term in list(terms):
        for variant in synonym_variants(term, allow_chinese=allow_chinese):
            if len(expanded) >= SEARCH_MAX_ROUNDS:
                break
            common.add_unique_key(expanded, variant)
        if len(expanded) >= SEARCH_MAX_ROUNDS:
            break
    return expanded[:SEARCH_MAX_ROUNDS]

def term_match_score(term: str, item: dict[str, Any]) -> tuple[float, str | None]:
    """Return (score, matched term). Ranking only; it does not replace the server-side keyword filter."""
    term_norm = normalize_match_text(term)
    if not term_norm:
        return (0.0, None)
    versions = item.get('versions') or []
    first_version = versions[0] if versions else {}
    fields = [
        ('name', normalize_match_text(item.get('name')), 1.0),
        ('base_model', normalize_match_text(first_version.get('base_model')), 0.6),
    ]
    best_score = 0.0
    for _field_name, text, weight in fields:
        if not text:
            continue
        score = 0.0
        if term_norm == text:
            score = 120.0
        elif term_norm in text:
            score = 85.0 + min(len(term_norm), 20)
        else:
            tokens = match_tokens(term_norm)
            if tokens:
                hits = sum(1 for token in tokens if token in text)
                if hits == len(tokens):
                    score = 70.0
                elif hits:
                    score = 45.0 * hits / len(tokens)
            if score == 0.0 and len(term_norm) >= 4:
                for token in match_tokens(text):
                    if abs(len(token) - len(term_norm)) <= 2 and edit_distance(term_norm, token) <= 1:
                        score = 35.0
                        break
        score *= weight
        if score > best_score:
            best_score = score
    if best_score <= 0:
        return (0.0, None)
    return (round(best_score, 2), term)

def used_count_bonus(item: dict[str, Any]) -> float:
    counter = item.get('counter') or {}
    raw = counter.get('used_count')
    try:
        used = max(0, int(raw))
    except Exception:
        used = 0
    return min(18.0, math.log10(used + 1) * 6.0)

def rank_remote_candidates(items: list[dict[str, Any]], terms: list[str], modality: str | None=None, recommended_route: str | None=None) -> list[dict[str, Any]]:
    """Stable sort by name/base_model hit + usage count + type."""
    ranked: list[tuple[float, int, dict[str, Any]]] = []
    for index, item in enumerate(items):
        best_score = 0.0
        best_term = None
        for term in terms:
            score, _ = term_match_score(term, item)
            if score > best_score:
                best_score = score
                best_term = term
        type_name = str(item.get('type') or '')
        type_bonus = 6.0 if type_name == 'Application' else 2.0 if type_name == 'Workflow' else 0.0
        available_bonus = 3.0 if ((item.get('versions') or [{}])[0].get('available') is not False) else 0.0
        total = best_score + used_count_bonus(item) + type_bonus + available_bonus
        clone = dict(item)
        reasons: list[str] = []
        if best_term:
            reasons.append(f'match:{best_term}')
        if recommended_route and recommended_route not in {'image-search', 'video-search'}:
            reasons.append(f'route:{recommended_route}')
        clone['remote_rank_reason'] = reasons
        clone['_search_score'] = round(total, 2)
        ranked.append((total, index, clone))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in ranked]

def summarize_remote_candidate(item: dict[str, Any]) -> dict[str, Any]:
    versions = item.get('versions') or []
    first_version = versions[0] if versions else {}
    used_count = (item.get('counter') or {}).get('used_count')
    cover_urls = first_version.get('cover_urls') or []
    preview_image = None
    preview_video = None
    for url in cover_urls:
        low = str(url).lower()
        if preview_image is None and any((low.endswith(ext) or ext in low for ext in ['.webp', '.jpg', '.jpeg', '.png', '.gif'])):
            preview_image = url
        if preview_video is None and any((low.endswith(ext) or ext in low for ext in ['.mp4', '.mov', '.webm'])):
            preview_video = url
    object_type = item.get('type')
    asset_id = item.get('id')
    version_id = first_version.get('id')
    # The app page / run / prefill only accept the version ID; the model hub lists the asset ID. Keep id and page_url
    # from the same source, and carry the asset ID separately in bizy_model_id / asset_id, so pick output does not
    # mislead with id=55649 and an app/56747 link.
    page_id = version_id if object_type == 'Application' and version_id not in (None, '') else asset_id
    return {
        'id': page_id,
        'bizy_model_id': asset_id,
        'asset_id': asset_id,
        'bizy_model_version_id': version_id,
        'version_id': version_id,
        'page_id': page_id,
        'run_target_id': page_id if object_type == 'Application' else None,
        'name': item.get('name'),
        'type': object_type,
        'base_model': first_version.get('base_model'),
        'sign': first_version.get('sign'),
        'draft_id': first_version.get('draft_id'),
        'used_count': used_count,
        'available': first_version.get('available'),
        'cover_urls': cover_urls,
        'preview_image': preview_image,
        'preview_video': preview_video,
        'cover_image_url': preview_image,
        'cover_video_url': preview_video,
        'official_url': common.COMMUNITY_APP_URL.format(id=page_id) if object_type == 'Application' else None,
        'page_url': common.object_page_url(object_type, asset_id, version_id=version_id),
        'remote_rank_reason': item.get('remote_rank_reason') or [],
        'stability': 'remote-only-analysis',
        'layer': 'platform-result',
    }

def number_badge(index: int) -> str:
    return f'{index}.'

def candidate_cover_markdown(item: dict[str, Any]) -> str | None:
    image_url = item.get('preview_image') or item.get('cover_image_url')
    if not image_url:
        return None
    title = str(item.get('name') or 'BizyAir candidate')
    return f'![{title}]({image_url})'

def candidate_link(item: dict[str, Any]) -> str:
    if item.get('page_url'):
        return str(item.get('page_url'))
    if item.get('official_url'):
        return str(item.get('official_url'))
    if item.get('id'):
        return common.COMMUNITY_APP_URL.format(id=item.get('id'))
    return ''

def candidate_execution_hint(item: dict[str, Any]) -> str:
    kind = common.normalized_text(item.get('type'))
    if kind == 'workflow':
        return '⚠️ The match is a workflow; whether it can run directly still needs confirmation'
    if item.get('available') is False:
        return '⚠️ For now, keep looking at the information and parameters'
    return '✅ Can run directly'

def candidate_fit_summary(item: dict[str, Any], modality: str | None) -> str:
    reasons = item.get('remote_rank_reason') or []
    for raw in reasons:
        reason = str(raw or '').strip()
        if reason.startswith('match:'):
            term = reason.split(':', 1)[1].strip()
            if term:
                return f'Closer to "{term}" that you are looking for this round.'
        if reason.startswith('route:'):
            route = reason.split(':', 1)[1].strip()
            if 'image-edit' in route:
                return 'More of an image editing / background swap / local repaint route.'
            if 'commercial' in route:
                return 'More of a product image / e-commerce image / commercial visual route.'
            if 'lip-sync' in route or 'audio-video' in route:
                return 'More of a lip-sync / audio-driven / digital-human talking-head route.'
            if 'first-last-frame' in route:
                return 'More of a first/last frame / transition shot route.'
        if reason == 'pack:illustration':
            return 'More of an illustration / anime / 2D art style.'
    base_model = str(item.get('base_model') or '').strip()
    if base_model:
        return f'Its underlying route leans toward {base_model}, worth a closer look as a candidate this round.'
    if common.normalized_text(modality) == 'video':
        return 'Fits this video direction better; start from this one.'
    return 'Fits this image direction better; start from this one.'

def build_candidate_reply_markdown(candidates: list[dict[str, Any]], *, modality: str | None=None, request_failed: bool=False) -> str:
    if not candidates:
        if request_failed:
            return '⚠️ **Search request failed for now**\nThe list endpoint did not return normally (possibly a network blip). Retry once shortly; if it keeps failing, send the app link / numeric ID directly.'
        return '📭 **No suitable BizyAir object found**\nI already tried keywords and common synonym expansions, but nothing matched this round.\n\nTry a more specific task word / model name, or send the app link / numeric ID directly; for underlying models you can also go back to option 6 ModelZoo.'
    heading = '🎯 **Found a few BizyAir objects**'
    intro = 'Candidates below (sorted by keyword relevance + BizyAir usage count; judge the modality yourself from the user need):'
    lines = [heading, '', intro, '']
    for (idx, item) in enumerate(candidates, start=1):
        title = item.get('name') or item.get('title') or f"BizyAir object {item.get('id')}"
        lines.append(f'{number_badge(idx)} **{title}**')
        lines.append('')
        lines.append(f'- **Best for**: {candidate_fit_summary(item, modality)}')
        cover_md = candidate_cover_markdown(item)
        if cover_md:
            lines.append(f'- **Cover image**: {cover_md}')
        lines.append(f'- **Can it run directly**: {candidate_execution_hint(item)}')
        link = candidate_link(item)
        if link:
            lines.append(f'- **Link**: {link}')
        if item.get('type') == 'Application':
            execution_id = item.get('run_target_id') or item.get('version_id') or item.get('id')
            if execution_id:
                lines.append(f"- **App execution ID**: `{execution_id}` (same source as the link; use this for run/prefill)")
            if item.get('bizy_model_id') and str(item.get('bizy_model_id')) != str(execution_id):
                lines.append(f"- **Model hub asset ID**: `{item.get('bizy_model_id')}` (model hub only, not used for run)")
        elif item.get('id'):
            lines.append(f"- **ID**: `{item.get('id')}`")
        lines.append('')
    lines.append('Which one do you like? Tell me the number or ID and I will continue running it for you.')
    return '\n'.join(lines).strip()

def emit_candidate_reply(payload: dict[str, Any], *, reply_format: str='json') -> None:
    if common.normalized_text(reply_format) == 'markdown':
        print(payload.get('reply_markdown') or '')
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))

def deduplicate_app_workflow_pairs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detect duplicate (App, Workflow shell) pairs for the same underlying app and keep only the Application in each pair.

    BizyAir often has a workflow shell page wrapping a real app (the shell has input_nodes=0, so running it fails);
    when both show up in search results they take up precious candidate slots.

    Dedupe signal priority:
      ① Strong signal: workflow.versions[0].ref_bizy_model_id points at an app in the same result set (hard evidence)
      ② Heuristic: only applies when all three conditions hold (avoids killing a result on ID proximity alone)
                 - exactly the same name (normalized_text equal)
                 - numerically adjacent IDs (|id_a - id_b| <= 2)
                 - one Application, one Workflow

    Who to keep: keep the Application and drop the Workflow (apps are usually the real execution body, shell pages fail more often).
    """
    by_id: dict[str, dict[str, Any]] = {}
    for it in items:
        item_id = str(it.get('id') or '').strip()
        if item_id:
            by_id[item_id] = it
    drop: set[str] = set()

    # ① Strong signal: ref_bizy_model_id points directly at some app in the same result set
    for it in items:
        if str(it.get('type') or '').lower() != 'workflow':
            continue
        first_version = (it.get('versions') or [{}])[0] if it.get('versions') else {}
        ref_id = first_version.get('ref_bizy_model_id')
        if ref_id is None:
            continue
        ref_str = str(ref_id).strip()
        if ref_str and ref_str in by_id and ref_str != str(it.get('id') or ''):
            drop.add(str(it.get('id') or ''))

    # ② Heuristic: same name + numerically adjacent ID + one app and one workflow (all three conditions must hold)
    items_with_int_id: list[tuple[int, dict[str, Any]]] = []
    for it in items:
        raw_id = str(it.get('id') or '').strip()
        if raw_id.isdigit():
            items_with_int_id.append((int(raw_id), it))
    for i, (id_a, item_a) in enumerate(items_with_int_id):
        if str(id_a) in drop:
            continue
        for (id_b, item_b) in items_with_int_id[i + 1:]:
            if str(id_b) in drop or abs(id_b - id_a) > 2:
                continue
            type_a = str(item_a.get('type') or '').lower()
            type_b = str(item_b.get('type') or '').lower()
            if {type_a, type_b} != {'application', 'workflow'}:
                continue
            name_a = common.normalized_text(item_a.get('name'))
            name_b = common.normalized_text(item_b.get('name'))
            if not name_a or name_a != name_b:
                continue
            # All three conditions hold: drop the workflow, keep the app
            drop.add(str(id_a) if type_a == 'workflow' else str(id_b))

    if not drop:
        return items
    return [it for it in items if str(it.get('id') or '') not in drop]


def format_remote_candidates(items: list[dict[str, Any]], semantic_plan: dict[str, Any], modality: str | None, limit: int | None=None) -> list[dict[str, Any]]:
    """Take the first N items and format them for output. Callers should rank with rank_remote_candidates first."""
    selected = items[:limit] if limit is not None else items
    return [summarize_remote_candidate(item) for item in selected]

def clean_search_term(value: str) -> str:
    text = str(value or '').strip()
    text = re.sub('^[\\-\\*\\d\\.\\)\\(]+\\s*', '', text)
    text = text.strip('`\'"“”‘’[](){}')
    text = re.sub('\\s+', ' ', text).strip()
    return text[:48]

def derive_subword_variants(term: str) -> list[str]:
    """Derive short sub-words from a multi-token compound term, as fallback keywords when the long term misses.

    Scenario: the BizyAir server keyword search is basically fuzzy matching on the app name. Passing a long term like
    'Flux Kontext Max' returns 0 hits when no app in the library contains 'flux' and 'kontext' and 'max' together.
    By splitting it into short sub-words like ['Flux', 'Kontext', 'Max'] and appending them to the search list,
    a long term that misses can still have a chance to catch apps whose name only contains 'Kontext'.

    Rules: split tokens on whitespace, drop too-short (< 2 chars) and punctuation-only ones, dedupe, keep the original order.
    """
    text = str(term or '').strip()
    if not text or not re.search('\\s', text):
        return []
    raw_tokens = re.split('\\s+', text)
    seen: set[str] = set()
    variants: list[str] = []
    for tok in raw_tokens:
        cleaned = clean_search_term(tok)
        if not cleaned or cleaned == text:
            continue
        if len(cleaned) < 2:
            continue
        if not re.search('[\\w\\u4e00-\\u9fff]', cleaned):
            continue
        low = normalize_match_text(cleaned)
        if low in SEARCH_FALLBACK_STOPWORDS or low in seen:
            continue
        seen.add(low)
        variants.append(cleaned)
    return variants

def build_candidate_search_terms(query: str, modality: str | None=None) -> list[str]:
    raw = str(query or '').strip()
    # internal input alias; never displayed
    if not raw:
        return ['image to video' if common.normalized_text(modality) == 'video' else 'general image generation']
    normalized = raw
    for sep in ['\n', '\r', '，', ',', '、', '|', '；', ';', '/']:
        normalized = normalized.replace(sep, '\n')
    terms: list[str] = []
    if normalized != raw:
        for part in normalized.splitlines():
            cleaned = clean_search_term(part)
            if cleaned:
                common.add_unique_key(terms, cleaned)
    else:
        cleaned = clean_search_term(raw)
        if cleaned:
            common.add_unique_key(terms, cleaned)
    if not terms:
        # internal input alias; never displayed
        common.add_unique_key(terms, 'image to video' if common.normalized_text(modality) == 'video' else 'general image generation')
    # Colloquial input is expanded into English/synonyms first; the search endpoint does literal matching, so
    # the Chinese words for "retouch" / "lip-sync" easily return 0 hits without expansion.
    terms = expand_candidate_search_terms(terms, modality)
    # Fallback fan-out: when the term count has not used up SEARCH_MAX_ROUNDS, add the sub-words of every
    # multi-token compound term too (appended after the original terms, so the full name is searched first and
    # sub-words later). This rescues the case where the LLM did not split short words on its own.
    if len(terms) < SEARCH_MAX_ROUNDS:
        for original in list(terms):
            for variant in derive_subword_variants(original):
                if len(terms) >= SEARCH_MAX_ROUNDS:
                    break
                common.add_unique_key(terms, variant)
            if len(terms) >= SEARCH_MAX_ROUNDS:
                break
    return terms[:SEARCH_MAX_ROUNDS]

def infer_high_confidence_route_from_terms(terms: list[str], modality: str | None, *, has_image: bool=False, has_audio: bool=False) -> str:
    joined = ' '.join((common.normalized_text(term) for term in terms if term))
    if common.normalized_text(modality) == 'video':
        # internal input alias; never displayed
        if has_audio or any((x in joined for x in ['\u5bf9\u53e3\u578b', '\u53e3\u64ad', '\u5634\u578b', '\u97f3\u9891\u9a71\u52a8', 'lip sync', 'lip-sync', '\u56fe\u7247\u52a0\u97f3\u9891'])):
            return 'lip-sync-or-audio-video'
        # internal input alias; never displayed
        if any((x in joined for x in ['\u9996\u5c3e\u5e27', '\u9996\u5e27', '\u5c3e\u5e27', '\u5e27\u95f4\u8fc7\u6e21', '\u8f6c\u573a'])):
            return 'first-last-frame-video'
        # internal input alias; never displayed
        if has_image or any((x in joined for x in ['\u56fe\u751f\u89c6\u9891', '\u7167\u7247\u52a8\u8d77\u6765', '\u56fe\u7247\u52a8\u8d77\u6765', 'image to video'])):
            return 'image-to-video'
        return 'video-search'
    # internal input alias; never displayed
    if has_image or any((x in joined for x in ['\u6539\u56fe', '\u91cd\u7ed8', '\u5c40\u90e8\u91cd\u7ed8', '\u6362\u80cc\u666f', '\u56fe\u751f\u56fe', '\u4fee\u56fe', '\u62a0\u56fe', '\u6269\u56fe', '\u53c2\u8003\u56fe', 'inpaint', 'image edit'])):
        return 'image-edit-or-reference-search'
    # internal input alias; never displayed
    if any((x in joined for x in ['\u5546\u54c1\u56fe', '\u4ea7\u54c1\u56fe', '\u7535\u5546', '\u4e3b\u56fe', '\u8be6\u60c5\u9875', '\u767d\u5e95', '\u6a21\u7279\u4e0a\u8eab', 'product image', 'ecommerce'])):
        return 'commercial-image-search'
    # internal input alias; never displayed
    if any((x in joined for x in ['\u5b57\u4f53', '\u4e2d\u6587\u6392\u7248', '\u6587\u5b57\u6d77\u62a5', 'logo\u5b57', 'text layout', 'font design'])):
        return 'text-capable-image-search'
    return 'image-search'

def build_semantic_plan(query: str, modality: str | None, *, has_image: bool=False, has_audio: bool=False) -> dict[str, Any]:
    search_terms = build_candidate_search_terms(query, modality)
    recommended_route = infer_high_confidence_route_from_terms(search_terms, modality, has_image=has_image, has_audio=has_audio)
    required_input_kind = None
    primary_intent = recommended_route if recommended_route not in {'image-search', 'video-search'} else 'generic-video' if common.normalized_text(modality) == 'video' else 'generic-image'
    reasoning_summary = f"search terms={', '.join(search_terms[:SEARCH_MAX_ROUNDS])}"
    if recommended_route not in {'image-search', 'video-search'}:
        reasoning_summary += f', high-confidence route={recommended_route}'
    return {'query': str(query or '').strip(), 'modality': common.normalized_text(modality) or None, 'has_image': has_image, 'has_audio': has_audio, 'primary_intent': primary_intent, 'secondary_intents': [], 'matched_packs': [], 'search_queries': search_terms, 'negative_queries': [], 'local_hints': [], 'recommended_route': recommended_route, 'required_input_kind': required_input_kind, 'reasoning_summary': reasoning_summary}

def is_remote_candidate_object(item: dict[str, Any]) -> bool:
    return str(item.get('type') or '') in {'Application', 'Workflow'}


def _cmd_pick_remote_candidates(query: str, modality: str, *, api_key_arg: str | None=None, remote_source: str='community', page: int=1, page_size: int=24, sort: str | None=None, base_models: str | None=None, limit: int=10, reply_format: str='json') -> None:
    """Unified AI app candidate search (shared by image / video).

    Design changes (vs the old cmd_pick_image_candidates / cmd_pick_video_candidates):
    - The server filters by Application + Workflow. Previously a mixed list was fetched and filtered on the client,
      so the first 48 `image` entries were almost all LoRA/VAE/Checkpoint, apps were squeezed out, and it looked like nothing was found.
    - Colloquial input is expanded first: retouch -> edit/inpaint, lip-sync -> lip sync, image-to-video -> image to video.
    - 0-hit term-splitting fallback: the server keyword does not support whitespace tokenization (e.g. "Seedream text-to-image" returns 0 directly);
      when nothing is hit, derive_subword_variants splits the terms and tries them one by one.
    - The list endpoint is public, so search does not require an API key; candidate ranking happens on the client and does not hard-filter by modality.
      AI apps have no authoritative modality field, so the LLM decides from name / base_model itself.
    """
    if not common.CATALOG_SEARCH_SUPPORTED:
        print(json.dumps({'source': 'remote-pick', 'remote_source': remote_source, 'query': query, 'modality': modality, 'catalog_search_supported': False, 'candidates': [], 'platform_results': [], 'notice': f'Community/official list search for {common.SITE_LABEL} has been disabled by configuration. Alternatives: ① send me the BizyAir app link or numeric ID directly; ② use ModelZoo search (menu 6) to pick an underlying model.'}, ensure_ascii=False, indent=2))
        return
    api_key = api_key_arg or ''
    semantic_plan = build_semantic_plan(query, modality)
    resolved_sort = sort or ('Auto' if common.normalized_text(remote_source) == 'official' else 'Most Used')
    search_terms = list(semantic_plan.get('search_queries', [])) or build_candidate_search_terms(query, modality)
    collected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    attempts: list[dict[str, Any]] = []
    internal_cap = max(limit * 4, 30)
    max_pages_per_term = 2
    terms_to_search = list(search_terms[:SEARCH_MAX_ROUNDS])

    def run_term(term: str) -> int:
        """Run one keyword and return how many candidates it added this round."""
        term_added = 0
        for current_page in range(page, page + max_pages_per_term):
            resp: dict[str, Any] = {}
            # Retry transport resets / 5xx; 4xx is a parameter problem, so retrying is pointless.
            for retry_index in range(3):
                resp = api.fetch_remote_models(api_key, remote_source=remote_source, page=current_page, page_size=page_size, keyword=term, sort=resolved_sort, model_types=APP_SEARCH_MODEL_TYPES, base_models=base_models)
                status = resp.get('status') if isinstance(resp, dict) else None
                if api.result_is_ok(resp) or (isinstance(status, int) and 400 <= status < 500):
                    break
                time.sleep(0.5 * (retry_index + 1))
            resp_data = api.extract_result_data(resp) if isinstance(resp, dict) else {}
            items = resp_data.get('list', [])
            attempt = {'keyword': term, 'raw_total': resp_data.get('total'), 'page': resp_data.get('current'), 'raw_page_count': len(items), 'accepted_count': 0}
            if isinstance(resp, dict) and not api.result_is_ok(resp):
                attempt['error'] = api.request_error_message(resp)
            attempts.append(attempt)
            if not items:
                break
            for item in items:
                item_id = str(item.get('id'))
                if not item_id or item_id in seen_ids:
                    continue
                if not is_remote_candidate_object(item):
                    continue
                seen_ids.add(item_id)
                collected.append(item)
                attempt['accepted_count'] += 1
                term_added += 1
            if len(collected) >= internal_cap:
                break
        return term_added

    for (term_index, term) in enumerate(terms_to_search, start=1):
        run_term(term)
        if term_index < SEARCH_MIN_ROUNDS:
            continue
        if len(collected) >= internal_cap:
            break

    # Fallback: if every original search_term returned 0 hits → split the terms and retry once
    if not collected:
        fallback_terms: list[str] = []
        for original in terms_to_search:
            for variant in derive_subword_variants(original):
                if variant not in terms_to_search and variant not in fallback_terms:
                    fallback_terms.append(variant)
                if len(fallback_terms) >= SEARCH_MAX_ROUNDS:
                    break
            if len(fallback_terms) >= SEARCH_MAX_ROUNDS:
                break
        for term in fallback_terms:
            run_term(term)
            if collected:
                break

    collected = deduplicate_app_workflow_pairs(collected)
    collected = rank_remote_candidates(collected, search_terms, modality, semantic_plan.get('recommended_route'))
    platform_results = format_remote_candidates(collected, semantic_plan, modality, limit=limit)
    source_tag = 'remote-pick-video' if common.normalized_text(modality) == 'video' else 'remote-pick'
    search_errors = [str(a.get('error')) for a in attempts if a.get('error')]
    request_failed = bool(attempts) and len(search_errors) == len(attempts)
    payload = {'source': source_tag, 'remote_source': remote_source, 'query': query, 'semantic_plan': semantic_plan, 'expanded_keywords': search_terms, 'attempts': attempts, 'search_errors': search_errors, 'limit': limit, 'platform_results': platform_results, 'candidates': platform_results, 'raw_total': sum((a.get('raw_total') or 0 for a in attempts)), 'reply_markdown': build_candidate_reply_markdown(platform_results, modality=modality, request_failed=request_failed)}
    if request_failed:
        # Every keyword attempt failed. Do not let an empty candidate list look like a
        # successful search; the caller would keep going on a false "no results".
        payload['error'] = 'REMOTE_REQUEST_FAILED'
        payload['message'] = search_errors[0] if search_errors else 'The list endpoint did not return normally; retry later.'
        if common.normalized_text(reply_format) == 'markdown':
            print(payload['reply_markdown'], file=sys.stderr)
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1)
    emit_candidate_reply(payload, reply_format=reply_format)


def cmd_pick_image_candidates(query: str, *, api_key_arg: str | None=None, remote_source: str='community', page: int=1, page_size: int=24, sort: str | None=None, base_models: str | None=None, limit: int=10, reply_format: str='json'):
    _cmd_pick_remote_candidates(query, 'image', api_key_arg=api_key_arg, remote_source=remote_source, page=page, page_size=page_size, sort=sort, base_models=base_models, limit=limit, reply_format=reply_format)


def cmd_pick_video_candidates(query: str, *, api_key_arg: str | None=None, remote_source: str='community', page: int=1, page_size: int=24, sort: str | None=None, base_models: str | None=None, limit: int=10, reply_format: str='json'):
    _cmd_pick_remote_candidates(query, 'video', api_key_arg=api_key_arg, remote_source=remote_source, page=page, page_size=page_size, sort=sort, base_models=base_models, limit=limit, reply_format=reply_format)


# ---------------------------------------------------------------------------
# Workflow-only exact search (v1.8.1)
#
# Difference from pick-image / pick-video:
#   - only Workflow is searched, Application is never mixed in;
#   - one keyword, one search round (the normal path sends a single request), no synonym expansion, no multi-round splitting;
#   - Recently by default, which fits "I clearly want to find one workflow";
#   - results only carry Workflow metadata plus a read-only export command: no detail lookup, no task creation.
# ---------------------------------------------------------------------------

def summarize_workflow_candidate(item: dict[str, Any]) -> dict[str, Any]:
    """Compress a Workflow entry from the list endpoint into a compact structure the agent can consume directly."""
    versions = item.get('versions') or []
    version = versions[0] if versions and isinstance(versions[0], dict) else {}
    cover_urls = [str(url) for url in (version.get('cover_urls') or []) if str(url or '').strip()]
    workflow_id = item.get('id')
    linked_app_id = version.get('ref_bizy_model_id')
    source_url = common.COMMUNITY_WORKFLOW_URL.format(id=workflow_id) if workflow_id is not None else None
    return {
        'id': workflow_id,
        'name': item.get('name') or item.get('title'),
        'type': 'Workflow',
        'version_id': version.get('id'),
        'version': version.get('version'),
        'base_model': version.get('base_model'),
        'sign': version.get('sign'),
        'public': version.get('public'),
        'available': version.get('available'),
        'counter': item.get('counter') or version.get('counter') or {},
        'cover_urls': cover_urls,
        'source_url': source_url,
        'page_url': source_url,
        'download_command': f'python3 scripts/cli.py download-workflow {workflow_id}' if workflow_id is not None else None,
        'has_linked_app': bool(linked_app_id),
        'linked_app_id': linked_app_id,
    }


def build_workflow_reply_markdown(payload: dict[str, Any]) -> str:
    query = payload.get('query') or {}
    keyword = query.get('keyword') or ''
    if payload.get('error'):
        return f"⚠️ **Workflow search request failed**\n{payload.get('message') or 'The list endpoint did not return normally; retry once shortly.'}"
    results = payload.get('results') or []
    if not results:
        return (
            f"📭 **No Workflow found**\nKeyword: `{keyword}`\n\n"
            "This is the Workflow-only exact search, so it does not expand terms automatically. Try a more specific model name / task word, "
            "or drop qualifiers such as anime or photorealistic and retry; you can also send the workflow link or numeric ID directly."
        )
    lines = [
        f"## Workflow search results: {keyword}",
        '',
        'Only Workflows are returned; AI apps are not mixed in.',
        '',
    ]
    for (idx, item) in enumerate(results, start=1):
        title = item.get('name') or f"Workflow {item.get('id')}"
        lines.append(f"{idx}. **{title}**")
        details: list[str] = []
        if item.get('id') is not None:
            details.append(f"ID `{item.get('id')}`")
        if item.get('version'):
            details.append(f"{item.get('version')} (version_id `{item.get('version_id')}`)")
        if item.get('base_model'):
            details.append(f"base_model `{item.get('base_model')}`")
        if details:
            lines.append('   - ' + ' · '.join(details))
        if item.get('source_url'):
            lines.append(f"   - Source: {item.get('source_url')}")
        if item.get('download_command'):
            lines.append(f"   - Read-only export: `{item.get('download_command')}`")
        linked = 'yes (metadata only; whether it can run directly still needs confirmation)' if item.get('has_linked_app') else 'no (workflow only; view / export only)'
        lines.append(f"   - Linked app: {linked}")
        lines.append('')
    lines.append('Send the ID or link when you want to export; `download-workflow` is read-only, creates no task and charges nothing.')
    return '\n'.join(lines).strip()


def cmd_search_workflows(query: str, *, api_key_arg: str | None=None, remote_source: str='community', page: int=1, page_size: int=50, sort: str | None=None, base_models: str | None=None, limit: int=20, reply_format: str='json') -> None:
    """Workflow-only exact search: one keyword, one search round, Workflow results only.

    This command deliberately skips synonym expansion and term splitting, so "find a Workflow" is not diluted by
    Applications or unrelated LoRAs. Use pick-image / pick-video when fuzzy recall is needed.
    """
    keyword = str(query or '').strip()
    if not keyword:
        payload = {
            'source': 'remote-workflow-search',
            'error': 'EMPTY_QUERY',
            'message': 'Please provide a Workflow keyword, for example: qwen image 2.1 anime.',
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(2)

    if not common.CATALOG_SEARCH_SUPPORTED:
        payload = {
            'source': 'remote-workflow-search',
            'remote_source': remote_source,
            'query': {'keyword': keyword, 'model_types': ['Workflow']},
            'error': 'CATALOG_SEARCH_DISABLED',
            'message': f'Community/official list search for {common.SITE_LABEL} has been disabled by configuration.',
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1)

    normalized_source = common.normalized_text(remote_source) or 'community'
    resolved_sort = sort or ('Auto' if normalized_source == 'official' else 'Recently')
    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or 50)))
    limit = max(1, min(100, int(limit or 20)))

    attempts: list[dict[str, Any]] = []
    response: dict[str, Any] = {}
    for retry_index in range(3):
        try:
            response = api.fetch_remote_models(
                api_key_arg or '',
                remote_source=normalized_source,
                page=page,
                page_size=page_size,
                keyword=keyword,
                sort=resolved_sort,
                model_types=('Workflow',),
                base_models=base_models,
            )
        except Exception as exc:
            response = {'ok': False, 'status': None, 'error': {'message': f'{type(exc).__name__}: {exc}'}}
        ok = api.result_is_ok(response)
        status = response.get('status') if isinstance(response, dict) else None
        attempts.append({
            'keyword': keyword,
            'page': page,
            'page_size': page_size,
            'sort': resolved_sort,
            'ok': ok,
            'http_status': status,
            'error': None if ok else api.request_error_message(response),
        })
        if ok or (isinstance(status, int) and 400 <= status < 500):
            break
        if retry_index < 2:
            time.sleep(0.5 * (retry_index + 1))

    ok = api.result_is_ok(response)
    data = api.extract_result_data(response) if ok else {}
    raw_items = data.get('list') or []
    items = [item for item in raw_items if isinstance(item, dict) and str(item.get('type') or '') == 'Workflow']
    results = [summarize_workflow_candidate(item) for item in items[:limit]]
    payload = {
        'source': 'remote-workflow-search',
        'remote_source': normalized_source,
        'query': {
            'keyword': keyword,
            'page': page,
            'page_size': page_size,
            'sort': resolved_sort,
            'model_types': ['Workflow'],
            'base_models': base_models,
        },
        'count': len(results),
        'total': data.get('total') if data.get('total') is not None else len(items),
        'attempts': attempts,
        'results': results,
        'notice': 'Workflow-only exact search: one keyword, one round (the normal path sends a single request), no synonym expansion, and no AI apps mixed in.',
    }
    if not ok:
        payload['error'] = 'REMOTE_REQUEST_FAILED'
        payload['message'] = api.request_error_message(response)
        payload['search_errors'] = [str(item.get('error')) for item in attempts if item.get('error')]
        payload['reply_markdown'] = build_workflow_reply_markdown(payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1)

    payload['reply_markdown'] = build_workflow_reply_markdown(payload)
    if common.normalized_text(reply_format) == 'markdown':
        print(payload['reply_markdown'])
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def compact_candidates_markdown(candidates: list[dict[str, Any]], *, keyword: str='') -> str:
    """Compact Markdown table: only the fields needed to pick, so a whole page of raw JSON is not poured into the context."""
    head = f'Search "{keyword}": {len(candidates)} results' if keyword else f'{len(candidates)} results in total'
    lines = [head, '', '| Name | Type | Base model | Execution ID | Asset ID | Version ID |', '|---|---|---|---:|---:|---:|']
    for item in candidates:
        name = str(item.get('name') or '').replace('|', '\\|')
        lines.append('| {} | {} | {} | {} | {} | {} |'.format(
            name, item.get('type') or '-', item.get('base_model') or '-',
            item.get('id') or '-', item.get('bizy_model_id') or '-', item.get('version_id') or '-'))
    return '\n'.join(lines)
