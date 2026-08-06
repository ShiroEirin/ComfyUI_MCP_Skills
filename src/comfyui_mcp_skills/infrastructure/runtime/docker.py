"""Opt-in Docker RuntimeController adapter with a fixed, safe argv contract."""

from __future__ import annotations

import re
import subprocess
from typing import Any

from comfyui_mcp_skills.infrastructure.runtime.systemd import RuntimeConfigError

# Docker container names: [a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}; leading ./- are invalid.
_CONTAINER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def docker_container_from_config(config: dict[str, Any]) -> str:
    """Return the validated Docker container name from a server record, or raise."""
    raw = config.get("runtime")
    if raw is None:
        raise RuntimeConfigError("no runtime binding configured")
    if not isinstance(raw, dict):
        raise RuntimeConfigError("runtime must be an object")
    if raw.get("adapter") != "docker":
        raise RuntimeConfigError("unsupported runtime adapter")
    container = raw.get("container")
    if not isinstance(container, str) or _CONTAINER_PATTERN.fullmatch(container) is None:
        raise RuntimeConfigError("runtime.container must be a safe Docker container name")
    return container


class DockerController:
    """Restart exactly one validated Docker container; never accepts commands."""

    def __init__(self, container: str, *, timeout_seconds: float = 120.0) -> None:
        if _CONTAINER_PATTERN.fullmatch(container) is None:
            raise RuntimeConfigError("unsafe Docker container name")
        self._container = container
        self._timeout = timeout_seconds

    def restart(self, server_id: str) -> dict[str, Any]:
        try:
            result = subprocess.run(
                ["docker", "restart", self._container],
                shell=False,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("docker executable is not available") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("docker restart timed out") from exc
        if result.returncode != 0:
            raise RuntimeError(f"docker restart failed with exit code {result.returncode}")
        return {
            "server_id": server_id,
            "adapter": "docker",
            "container": self._container,
            "completed": True,
        }


__all__ = ["DockerController", "docker_container_from_config"]
