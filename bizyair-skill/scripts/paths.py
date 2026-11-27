"""Path and runtime directory resolution (no business logic).

Answers four questions:
  1) Where is the skill root?              -> SKILL_ROOT
  2) Which config.json is read?            -> resolve_config_path_for_read()
  3) Where do task outputs land?           -> resolve_output_root()
  4) Where are resume / batch state files? -> resolve_runtime_state_file() / resolve_batch_runs_dir()

The skill install directory holds only code / docs / config templates; by default no
runtime state or output is written there.

Priority:
  - Runtime state: BIZYAIR_STATE_DIR > BIZYAIR_HOME > user state directory
  - Output: BIZYAIR_OUTPUT_DIR > BIZYAIR_HOME/outputs > current workspace/bizyair-outputs
  - If the current workspace is inside the skill directory, output falls back to outputs
    under the runtime state directory, so the skill stays clean.

This file depends on no other script module and is the lowest layer of the whole skill.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


APP_NAME = "bizyair"
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
# Kept for old code/docs; semantically this is the skill root, not the user workspace.
WORKSPACE_ROOT = SKILL_ROOT

HOME_ENV = "BIZYAIR_HOME"
STATE_DIR_ENV = "BIZYAIR_STATE_DIR"
OUTPUT_DIR_ENV = "BIZYAIR_OUTPUT_DIR"
DEFAULT_OUTPUT_DIRNAME = "bizyair-outputs"


def _expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _default_state_root() -> Path:
    """User-level state directory; not the skill directory, and not tied to the current workspace."""
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg_state:
        return (_expand_path(xdg_state) / APP_NAME).resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / APP_NAME).resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "").strip() or os.environ.get("APPDATA", "").strip()
        if base:
            return (_expand_path(base) / APP_NAME).resolve()
    return (Path.home() / ".local" / "state" / APP_NAME).resolve()


def _temp_state_root() -> Path:
    suffix = f"-{os.getuid()}" if hasattr(os, "getuid") else ""
    return (Path(tempfile.gettempdir()) / f"{APP_NAME}{suffix}").resolve()


def display_path(path: str | Path) -> str:
    raw = str(path)
    home = str(Path.home())
    if raw == home:
        return "~"
    if raw.startswith(home + os.sep):
        return "~" + raw[len(home):]
    return raw


def preferred_config_path() -> Path:
    return (SKILL_ROOT / "config.json").resolve()


def resolve_config_path_for_read() -> Path:
    return preferred_config_path()


def config_display_path() -> str:
    return display_path(preferred_config_path())


def resolve_runtime_root() -> Path:
    state_override = os.environ.get(STATE_DIR_ENV, "").strip()
    if state_override:
        return _expand_path(state_override)

    home_override = os.environ.get(HOME_ENV, "").strip()
    if home_override:
        return _expand_path(home_override)

    try:
        return _default_state_root()
    except Exception:
        return _temp_state_root()


def resolve_runtime_state_file() -> Path:
    return (resolve_runtime_root() / "runtime_state.json").resolve()


def resolve_batch_runs_dir() -> Path:
    return (resolve_runtime_root() / "batch_runs").resolve()


def resolve_output_root() -> Path:
    output_override = os.environ.get(OUTPUT_DIR_ENV, "").strip()
    if output_override:
        return _expand_path(output_override)

    home_override = os.environ.get(HOME_ENV, "").strip()
    if home_override:
        return (_expand_path(home_override) / "outputs").resolve()

    try:
        invocation_cwd = Path.cwd().resolve()
    except OSError:
        invocation_cwd = SKILL_ROOT
    if _is_relative_to(invocation_cwd, SKILL_ROOT):
        # When commands are run from inside the skill directory, do not write results back into the install directory.
        return (resolve_runtime_root() / "outputs").resolve()
    return (invocation_cwd / DEFAULT_OUTPUT_DIRNAME).resolve()


def resolve_batch_outputs_dir() -> Path:
    """Batch task output root. Status / logs are not here; they live in resolve_batch_runs_dir()."""
    return (resolve_output_root() / "batch_runs").resolve()


def protected_output_roots() -> list[Path]:
    """Directory roots where explicit task output is forbidden.

    - Skill install directory: only code / docs / config templates; it must stay clean.
    - Runtime batch directory: `batch-status.json` / heartbeat / lock files live here,
      so task output written here would corrupt state.
    """
    roots = [SKILL_ROOT]
    try:
        roots.append(resolve_batch_runs_dir())
    except Exception:
        pass
    return [root.resolve() for root in roots]


def is_protected_output_path(path: str | Path) -> bool:
    """Whether an explicit output lands inside a protected directory (including the directory itself)."""
    resolved = _expand_path(str(path))
    return any(_is_relative_to(resolved, root) for root in protected_output_roots())


def load_config_json() -> dict:
    config_path = resolve_config_path_for_read()
    try:
        if config_path.exists():
            parsed = json_load(config_path)
            if isinstance(parsed, dict):
                return parsed
    except Exception:
        pass
    return {}


def json_load(path: Path) -> object:
    import json

    return json.loads(path.read_text(encoding="utf-8"))
