"""capabilities.py — map colloquial user intent to ModelZoo capabilities (v1.12.5).

Background: the ModelZoo list uses literal keyword matching, so when the user says
"remove the background" it does not necessarily hit `background-remover`. This module
normalizes synonyms through a local capability table, then the modelzoo picker expands
those capability words. Dedicated endpoints win; general editing is used as the
fallback only when no dedicated capability exists.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common import normalized_text

SKILL_ROOT = Path(__file__).resolve().parent.parent
CAPABILITIES_PATH = SKILL_ROOT / 'config' / 'capabilities.json'
_CACHE: dict[str, Any] | None = None


def load_capabilities() -> dict[str, Any]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        parsed = json.loads(CAPABILITIES_PATH.read_text(encoding='utf-8'))
    except Exception:
        parsed = {}
    caps = parsed.get('capabilities') if isinstance(parsed, dict) else {}
    _CACHE = caps if isinstance(caps, dict) else {}
    return _CACHE


def detect(query: str | None) -> dict[str, Any] | None:
    """Return the matched capability (with name/label/aliases/...); the longest alias wins."""
    q = normalized_text(query)
    if not q:
        return None
    best_len = 0
    best: tuple[str, dict[str, Any]] | None = None
    for name, cap in load_capabilities().items():
        if not isinstance(cap, dict):
            continue
        for alias in cap.get('aliases') or []:
            a = normalized_text(alias)
            if not a or a not in q:
                continue
            if len(a) > best_len:
                best_len = len(a)
                best = (name, cap)
    if not best:
        return None
    name, cap = best
    return {'name': name, **cap}


def get_capability(name: str | None) -> dict[str, Any] | None:
    if not name:
        return None
    cap = load_capabilities().get(name)
    if not isinstance(cap, dict):
        return None
    return {'name': name, **cap}


def search_terms(query: str | None, capability: dict[str, Any] | None) -> list[str]:
    """Capability words first; fall back to the raw query when there is no capability. Dedupe, max 3."""
    terms: list[str] = []
    if capability:
        for term in capability.get('search_terms') or []:
            term = str(term).strip()
            if term and term not in terms:
                terms.append(term)
    if not terms:
        q = str(query or '').strip()
        if q:
            terms.append(q)
    return terms[:3]


def preferred_endpoints(capability: dict[str, Any] | None) -> set[str]:
    if not capability:
        return set()
    return {str(x).strip() for x in (capability.get('preferred_endpoints') or []) if str(x).strip()}


def dedicated_endpoints(capability: dict[str, Any] | None) -> set[str]:
    if not capability:
        return set()
    return {str(x).strip() for x in (capability.get('dedicated_endpoints') or []) if str(x).strip()}


def suggested_prompt(capability: dict[str, Any] | None) -> str | None:
    """Prompt used by the general-editing fallback; a dedicated cutout endpoint needs no prompt."""
    if not capability:
        return None
    prompt = capability.get('suggested_prompt')
    return str(prompt) if prompt else None


def fallback_policy(capability: dict[str, Any] | None) -> str | None:
    if capability and capability.get('do_not_search_apps'):
        return 'do_not_search_apps'
    return None
