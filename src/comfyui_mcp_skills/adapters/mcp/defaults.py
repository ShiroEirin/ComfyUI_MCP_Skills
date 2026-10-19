"""Persistent generation defaults applied to workflow parameters at call time.

Defaults are *configuration*, not business data: they live in a JSON file under
``COMFYUI_MCP_DIR`` (falling back to the working directory) so operators can also
edit them by hand. A run then only needs the parameters that actually change.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS

DEFAULTS_PATH_ENV = "COMFYUI_MCP_DEFAULTS_PATH"
BASE_DIR_ENV = "COMFYUI_MCP_DIR"
DEFAULTS_FILENAME = "defaults.json"
MAX_DEFAULTS = 64
_ACTIONS = frozenset({"get", "set", "clear"})


def defaults_path() -> Path:
    """Return the defaults file location (env override wins)."""
    explicit = os.environ.get(DEFAULTS_PATH_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    base = Path(os.environ.get(BASE_DIR_ENV, os.getcwd())).resolve()
    return base / DEFAULTS_FILENAME


def _is_scalar(value: object) -> bool:
    return isinstance(value, (bool, int, float, str))


def load_defaults() -> dict[str, Any]:
    """Read stored defaults; a missing file means "no defaults"."""
    path = defaults_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise MCPError(
            code=INVALID_PARAMS, message="Generation defaults are unreadable"
        ) from exc
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise MCPError(
            code=INVALID_PARAMS, message="Generation defaults are invalid JSON"
        ) from exc
    if not isinstance(document, dict):
        raise MCPError(code=INVALID_PARAMS, message="Generation defaults must be an object")
    values = document.get("values", {})
    if not isinstance(values, dict):
        raise MCPError(
            code=INVALID_PARAMS, message="Generation defaults values must be an object"
        )
    return {key: values[key] for key in sorted(values) if _is_scalar(values[key])}


def _validate(values: object) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise TypeError("values must be an object")
    if len(values) > MAX_DEFAULTS:
        raise ValueError(f"values must not exceed {MAX_DEFAULTS} entries")
    checked: dict[str, Any] = {}
    for name, value in values.items():
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 128
            or "\r" in name
            or "\n" in name
        ):
            raise ValueError("default parameter names must be plain strings")
        if not _is_scalar(value):
            raise ValueError(f'default value for "{name}" must be a scalar')
        checked[name] = value
    return {name: checked[name] for name in sorted(checked)}


def describe_defaults() -> dict[str, Any]:
    return {"values": load_defaults(), "path": str(defaults_path())}


def save_defaults(values: dict[str, Any]) -> dict[str, Any]:
    """Persist defaults atomically and return the stored document."""
    checked = _validate(values)
    path = defaults_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps({"values": checked}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return {"values": checked, "path": str(path)}


def clear_defaults() -> dict[str, Any]:
    path = defaults_path()
    path.unlink(missing_ok=True)
    return {"values": {}, "path": str(path)}


def read_action(action: str) -> str:
    if action not in _ACTIONS:
        raise ValueError("action must be one of: get, set, clear")
    return action


def apply_defaults(
    arguments: dict[str, Any],
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """Fill parameters the caller omitted with stored defaults.

    Only parameters the workflow actually declares are considered, and explicit
    arguments always win, so defaults never widen a workflow's contract.
    """
    if not parameters:
        return arguments
    defaults = load_defaults()
    if not defaults:
        return arguments
    merged = dict(arguments)
    for name in parameters:
        if name not in merged and name in defaults:
            merged[name] = defaults[name]
    return merged
