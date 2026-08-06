"""Opt-in systemd RuntimeController adapter with a fixed, safe argv contract."""

from __future__ import annotations

import re
import subprocess
from typing import Any

from comfyui_mcp_skills.application.runtime_control import RuntimeController

_UNIT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\.service$")


class RuntimeConfigError(ValueError):
    """The server runtime binding is malformed or unsupported; adapter stays unavailable."""


def systemd_unit_from_config(config: dict[str, Any]) -> str:
    """Return the validated systemd unit name from a server record, or raise."""
    raw = config.get("runtime")
    if raw is None:
        raise RuntimeConfigError("no runtime binding configured")
    if not isinstance(raw, dict):
        raise RuntimeConfigError("runtime must be an object")
    if raw.get("adapter") != "systemd":
        raise RuntimeConfigError("unsupported runtime adapter")
    unit = raw.get("unit")
    if not isinstance(unit, str) or _UNIT_PATTERN.fullmatch(unit) is None:
        raise RuntimeConfigError("runtime.unit must be a safe systemd service unit name")
    return unit


class SystemdController:
    """Restart exactly one validated systemd unit; never accepts commands."""

    def __init__(self, unit: str, *, timeout_seconds: float = 60.0) -> None:
        if _UNIT_PATTERN.fullmatch(unit) is None:
            raise RuntimeConfigError("unsafe systemd unit name")
        self._unit = unit
        self._timeout = timeout_seconds

    def restart(self, server_id: str) -> dict[str, Any]:
        try:
            result = subprocess.run(
                ["systemctl", "restart", self._unit],
                shell=False,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("systemctl executable is not available") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("systemctl restart timed out") from exc
        if result.returncode != 0:
            raise RuntimeError(f"systemctl restart failed with exit code {result.returncode}")
        return {
            "server_id": server_id,
            "adapter": "systemd",
            "unit": self._unit,
            "completed": True,
        }


def controller_from_config(config: dict[str, Any]) -> RuntimeController | None:
    """Build the configured controller; any malformed binding fails closed to None."""
    try:
        unit = systemd_unit_from_config(config)
    except RuntimeConfigError:
        return None
    return SystemdController(unit)
