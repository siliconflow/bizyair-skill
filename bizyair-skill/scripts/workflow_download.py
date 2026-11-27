#!/usr/bin/env python3
"""download-workflow — read-only export of one public BizyAir workflow.

Design principles:
  - Read-only: creates no task and costs nothing.
  - Exports only public, available Workflow / Application source workflows.
  - Writes ComfyUI UI format JSON plus a .meta.json describing source and version.
  - `comfy-ui?id=...` draft links have no public mapping, so they are rejected explicitly.

Usage:
  python3 scripts/cli.py download-workflow <link or ID>
  python3 scripts/cli.py download-workflow <link or ID> -o ./workflows/
  python3 scripts/cli.py download-workflow <link or ID> --version v1.0
  python3 scripts/cli.py download-workflow <link or ID> -o ./my-workflow.json --force
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import api  # noqa: E402
import common  # noqa: E402
import paths  # noqa: E402
import remote  # noqa: E402

SUPPORTED_OBJECT_TYPES = {"Workflow", "Application"}
DEFAULT_FORMAT = "comfyui-ui"
NOTICE = (
    "Exported read-only through the platform resolve API; it is not guaranteed to be "
    "identical to the web page download button, and the download counter may not increase. "
    "A workflow may depend on BizyAir custom nodes/models, so confirm the nodes and models "
    "are all present before importing into a local ComfyUI."
)


def _print_json(data: Any, *, stream=sys.stdout) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2), file=stream)


def _fail(code: str, message: str, *, exit_code: int = 2, **extra: Any) -> None:
    payload: dict[str, Any] = {
        "status": "error",
        "action": "download-workflow",
        "error": code,
        "message": message,
    }
    payload.update(extra)
    _print_json(payload, stream=sys.stderr)
    raise SystemExit(exit_code)


def _request_with_transport_retry(request_fn, *args: Any, attempts: int = 3, **kwargs: Any) -> dict[str, Any]:
    """Short retries for transport-layer resets / timeouts / 5xx; 4xx is never retried.

    The normal path still sends a single request; this fallback only covers occasional
    connection resets from BizyAir/Cloudflare.
    """
    response: dict[str, Any] = {}
    for attempt in range(attempts):
        try:
            response = request_fn(*args, **kwargs)
        except Exception as exc:
            response = {"ok": False, "status": None, "error": {"message": f"{type(exc).__name__}: {exc}"}}
        if api.result_is_ok(response):
            return response
        status = response.get("status") if isinstance(response, dict) else None
        if isinstance(status, int) and 400 <= status < 500:
            return response
        if attempt < attempts - 1:
            time.sleep(0.5 * (attempt + 1))
    return response


def _safe_version_label(value: Any) -> str:
    text = str(value or "").strip() or "version"
    text = re.sub(r"[^0-9A-Za-z._-]+", "-", text).strip("-._")
    return text[:80] or "version"


def _default_filename(object_id: str, version_label: str, stamp: str) -> str:
    return f"{stamp}-{object_id}-{_safe_version_label(version_label)}.json"


def _meta_path(workflow_path: Path) -> Path:
    if workflow_path.suffix.lower() == ".json":
        return workflow_path.with_suffix(".meta.json")
    return workflow_path.with_name(workflow_path.name + ".meta.json")


def _resolve_output_path(
    output_arg: str | None,
    *,
    object_id: str,
    version_label: str,
    stamp: str,
) -> Path:
    filename = _default_filename(object_id, version_label, stamp)
    if not output_arg:
        return (paths.resolve_output_root() / "workflows" / filename).resolve()

    expanded = Path(os.path.expandvars(os.path.expanduser(str(output_arg)))).resolve()
    if expanded.exists() and expanded.is_dir():
        return (expanded / filename).resolve()
    if str(output_arg).endswith(("/", os.sep)):
        return (expanded / filename).resolve()
    if expanded.suffix.lower() == ".json":
        return expanded
    # Without a .json suffix, treat the value as a directory so a user's directory name is not used as a file name.
    return (expanded / filename).resolve()


def _allocate_path(path: Path, *, force: bool) -> Path:
    """Return a path that will not overwrite an existing file; --force allows overwriting."""
    if force:
        return path
    if not path.exists() and not _meta_path(path).exists():
        return path
    stem, suffix = path.stem, path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists() and not _meta_path(candidate).exists():
            return candidate
    _fail("WORKFLOW_OUTPUT_PATH_CONFLICT", "Too many files with the same name in the output directory; choose a different output path.")


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)


def _fetch_detail(key: str, object_id: str, *, required: bool=True) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    detail_resp = _request_with_transport_retry(api.fetch_remote_detail, key, object_id)
    auth_status = api.auth_error_status(detail_resp)
    if auth_status:
        _fail(
            "AUTH_FAILED",
            "The API key is invalid or expired; check it and configure it again.",
            exit_code=1,
            http_status=auth_status,
        )
    detail = api.extract_result_data(detail_resp)
    if detail:
        return detail, detail_resp

    fallback = api.find_remote_model_by_id(key, object_id, remote_source="community")
    if fallback is None:
        fallback = api.find_remote_model_by_id(key, object_id, remote_source="official")
    if fallback:
        return fallback, detail_resp
    if not required:
        return None, detail_resp

    status = detail_resp.get("status") if isinstance(detail_resp, dict) else None
    _fail(
        "WORKFLOW_TARGET_NOT_FOUND",
        "This object was not found, or it is not publicly accessible.",
        exit_code=1 if status is None else 2,
        detail_error=api.request_error_message(detail_resp),
    )
    raise AssertionError("unreachable")


def _pick_version(versions: list[dict[str, Any]], wanted: str | None) -> dict[str, Any]:
    if not versions:
        _fail("WORKFLOW_NO_VERSIONS", "This object has no available version, so the workflow cannot be exported.")
    if not wanted:
        return versions[0]
    target = str(wanted).strip()
    for version in versions:
        if str(version.get("id")) == target or str(version.get("version")) == target:
            return version
    available = [f"{item.get('version')}(id={item.get('id')})" for item in versions]
    _fail(
        "WORKFLOW_VERSION_NOT_FOUND",
        f"Version {target} not found. Available versions: {', '.join(available)}",
        available_versions=available,
    )
    raise AssertionError("unreachable")


def _fetch_workflow_from_response(key: str, object_id: str, workflow_resp: dict[str, Any]) -> dict[str, Any]:
    """Unwrap the workflow response already fetched; fall back to the webapp URL when needed."""
    workflow = api.extract_result_data(workflow_resp)
    if workflow:
        return workflow

    # Fallback: some AI apps expose web_app_workflow_url.
    webapp = api.extract_result_data(_request_with_transport_retry(api.fetch_remote_webapp_detail, key, object_id))
    workflow_url = webapp.get("web_app_workflow_url") if isinstance(webapp, dict) else None
    if workflow_url:
        workflow = api.extract_result_data(_request_with_transport_retry(api.fetch_remote_workflow_from_url, workflow_url))
        if workflow:
            return workflow

    _fail(
        "WORKFLOW_FETCH_FAILED",
        "The platform returned no usable workflow content.",
        exit_code=1,
        detail_error=api.request_error_message(workflow_resp),
    )
    raise AssertionError("unreachable")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py download-workflow",
        description="Read-only export of one public BizyAir Workflow / AI app source workflow (ComfyUI UI JSON).",
    )
    parser.add_argument("target", help="link or numeric ID of a BizyAir Workflow / Application")
    parser.add_argument("-o", "--output", help="output .json file, or output directory; defaults to bizyair-outputs/workflows/")
    parser.add_argument("--version", help="version label (e.g. v1.0) or version ID; defaults to the first available version")
    parser.add_argument("--force", action="store_true", help="overwrite files with the same name; by default -1/-2 is appended to avoid overwriting")
    parser.add_argument("--no-meta", action="store_true", help="do not write the .meta.json description file")
    parser.add_argument("--api-key", help="BizyAir API key; read from the environment or config.json by default")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    key = api.require_api_key(args.api_key)

    resolved = remote.resolve_info_target(args.target)
    if not resolved.get("ok"):
        error = str(resolved.get("error") or "")
        message = resolved.get("message") or "Cannot recognize this input."
        if error in {"UNSUPPORTED_INFO_TARGET", "EMPTY_INFO_TARGET", "INFO_TARGET_ID_NOT_FOUND"}:
            message = "Provide a BizyAir community Workflow / AI app link, or a bare numeric ID."
        elif error == "UNSUPPORTED_INFO_HOST":
            message = "Only bizyair.ai links are supported."
        _fail(
            "WORKFLOW_TARGET_UNRECOGNIZED",
            message,
            accepted_forms=[
                "57862",
                "https://www.bizyair.ai/community/models/publicity/56853",
                "https://www.bizyair.ai/community/app/57862",
            ],
        )
    if resolved.get("input_kind") == "workflow_link":
        _fail(
            "WORKFLOW_LINK_NOT_SUPPORTED",
            "A draft link like comfy-ui?id=... has no public API that can restore the workflow; use a community Workflow or AI app link instead.",
        )

    object_id = str(resolved["resolved_object_id"])
    # Try the model detail first; app version IDs (e.g. 57827) often have no model detail,
    # so this must not fail early.
    detail, detail_resp = _fetch_detail(key, object_id, required=False)

    # App version IDs and model hub asset IDs can collide (e.g. 56747 = App version + LoRA),
    # or the input may already be an app version ID (e.g. 57827). Consistent with info/prefill/run:
    # when there is a webapp detail and the model is not a Workflow, resolve it as an AI app;
    # a pure Workflow stays a Workflow.
    id_collision = None
    webapp_resp = _request_with_transport_retry(api.fetch_remote_webapp_detail, key, object_id)
    webapp_auth_status = api.auth_error_status(webapp_resp)
    if webapp_auth_status and not detail:
        _fail(
            "AUTH_FAILED",
            "The API key is invalid or expired; check it and configure it again.",
            exit_code=1,
            http_status=webapp_auth_status,
        )
    webapp_data = api.extract_result_data(webapp_resp)
    if webapp_data.get("id") and str((detail or {}).get("type") or "").strip().lower() != "workflow":
        version_id = webapp_data.get("id")
        version_detail_resp = _request_with_transport_retry(api.fetch_remote_version_detail, key, version_id)
        effective = remote.build_webapp_app_detail(object_id, webapp_resp, version_detail_resp)
        detail = api.extract_result_data(effective)
        id_collision = remote.detect_model_webapp_collision(detail_resp, webapp_resp)
    if not detail:
        status = detail_resp.get("status") if isinstance(detail_resp, dict) else None
        _fail(
            "WORKFLOW_TARGET_NOT_FOUND",
            "This object was not found, or it is not publicly accessible.",
            exit_code=1 if status is None else 2,
            detail_error=api.request_error_message(detail_resp),
        )
    object_type = str(detail.get("type") or "").strip()
    if object_type not in SUPPORTED_OBJECT_TYPES:
        _fail(
            "NOT_A_WORKFLOW",
            f"This object is {object_type or 'an unknown type'}, not a Workflow / Application, so the workflow cannot be exported.",
            object_type=object_type,
            bizy_model_id=object_id,
        )

    versions = detail.get("versions") or []
    version = _pick_version(versions, args.version)
    version_id = version.get("id")
    if version_id is None:
        _fail("WORKFLOW_NO_VERSIONS", "This version has no version ID, so it cannot be exported.", bizy_model_id=object_id)
    if version.get("public") is False:
        _fail("WORKFLOW_NOT_PUBLIC", "This workflow version is not public and cannot be exported.", bizy_model_id=object_id, version_id=version_id)
    if version.get("available") is False:
        _fail("WORKFLOW_NOT_AVAILABLE", "This workflow version is currently unavailable and cannot be exported.", bizy_model_id=object_id, version_id=version_id)

    # Version detail and workflow JSON are independent, so fetch them concurrently;
    # on failure the webapp fallback still applies as before.
    with ThreadPoolExecutor(max_workers=2) as executor:
        version_detail_future = executor.submit(_request_with_transport_retry, api.fetch_remote_version_detail, key, version_id)
        workflow_future = executor.submit(_request_with_transport_retry, api.fetch_remote_workflow, key, version_id)
        version_detail_resp = version_detail_future.result()
        workflow_resp = workflow_future.result()
    version_detail = api.extract_result_data(version_detail_resp)
    workflow = _fetch_workflow_from_response(key, object_id, workflow_resp)
    if not isinstance(workflow, dict) or not isinstance(workflow.get("nodes"), list):
        _fail(
            "WORKFLOW_FORMAT_UNSUPPORTED",
            "The platform returned something that is not an exportable ComfyUI UI workflow (nodes missing).",
            exit_code=1,
            keys=sorted(workflow.keys()) if isinstance(workflow, dict) else [],
        )

    version_label = version.get("version") or version_detail.get("version") or version_id
    stamp = time.strftime("%Y%m%d-%H%M%S")
    output_path = _resolve_output_path(
        args.output,
        object_id=object_id,
        version_label=str(version_label),
        stamp=stamp,
    )
    if paths.is_protected_output_path(output_path):
        _fail(
            "WORKFLOW_OUTPUT_PATH_FORBIDDEN",
            "A workflow cannot be written into the skill install directory or the runtime batch directory; choose a workspace path instead.",
            output_path=str(output_path),
        )
    output_path = _allocate_path(output_path, force=args.force)

    node_count = len(workflow.get("nodes") or [])
    link_count = len(workflow.get("links") or [])
    page_object_id = str(version_id) if object_type == "Application" else str(object_id)
    source_url = (
        common.COMMUNITY_WORKFLOW_URL.format(id=object_id)
        if object_type == "Workflow"
        else common.COMMUNITY_APP_URL.format(id=page_object_id)
    )
    meta = {
        "schema_version": 1,
        "source": "bizyair",
        "action": "download-workflow",
        "download_mode": "resolve-export",
        "bizy_model_id": object_id,
        "name": detail.get("name"),
        "object_type": object_type,
        "author": detail.get("user_nick_name") or detail.get("nick_name") or detail.get("user_id"),
        "source_url": source_url,
        "page_object_id": page_object_id,
        "version_id": version_id,
        "version": version_label,
        "base_model": version.get("base_model") or version_detail.get("base_model"),
        "file_name": version.get("file_name") or version_detail.get("file_name"),
        "path": version_detail.get("path"),
        "sign": version.get("sign") or version_detail.get("sign"),
        "public": version.get("public"),
        "available": version.get("available"),
        "format": DEFAULT_FORMAT,
        "node_count": node_count,
        "link_count": link_count,
        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "saved_path": str(output_path),
        "notice": NOTICE,
    }
    if id_collision:
        meta["id_collision"] = id_collision
    if detail.get("description"):
        meta["description"] = str(detail.get("description"))[:2000]
    elif version_detail.get("description"):
        meta["description"] = str(version_detail.get("description"))[:2000]

    try:
        _write_json_atomic(output_path, workflow)
        meta_path = None
        if not args.no_meta:
            meta_path = _meta_path(output_path)
            _write_json_atomic(meta_path, meta)
    except OSError as exc:
        _fail(
            "WORKFLOW_WRITE_FAILED",
            f"Write failed: {exc}; point `-o` at a writable path, or set `BIZYAIR_OUTPUT_DIR` to a writable directory.",
            exit_code=1,
            output_path=str(output_path),
        )

    _print_json(
        {
            "status": "ok",
            "action": "download-workflow",
            "saved_path": str(output_path),
            "meta_path": str(meta_path) if meta_path else None,
            "workflow": {
                "bizy_model_id": object_id,
                "page_object_id": page_object_id,
                "page_url": source_url,
                "name": detail.get("name"),
                "type": object_type,
                "version": version_label,
                "version_id": version_id,
                "format": DEFAULT_FORMAT,
                "node_count": node_count,
                "link_count": link_count,
            },
            "id_collision": id_collision,
            "notice": NOTICE,
        }
    )


if __name__ == "__main__":
    main()
