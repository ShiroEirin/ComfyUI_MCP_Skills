"""Docker RuntimeController adapter contracts."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from comfyui_mcp_skills.infrastructure.runtime.docker import (
    DockerController,
    docker_container_from_config,
)
from comfyui_mcp_skills.infrastructure.runtime.systemd import (
    RuntimeConfigError,
    controller_from_config,
)


def test_container_from_config_accepts_names_and_rejects_unsafe_shapes() -> None:
    assert (
        docker_container_from_config(
            {"runtime": {"adapter": "docker", "container": "comfyui-local"}}
        )
        == "comfyui-local"
    )

    for config in (
        {},
        {"runtime": {}},
        {"runtime": {"adapter": "docker"}},
        {"runtime": {"adapter": "systemd"}},
        {"runtime": {"adapter": "docker", "container": ""}},
        {"runtime": {"adapter": "docker", "container": "-comfyui"}},
        {"runtime": {"adapter": "docker", "container": ".comfyui"}},
        {"runtime": {"adapter": "docker", "container": "../comfyui"}},
        {"runtime": {"adapter": "docker", "container": "comfyui local"}},
        {"runtime": {"adapter": "docker", "container": "comfyui/with-slash"}},
        {"runtime": {"adapter": "docker", "container": "a" * 129}},
    ):
        with pytest.raises(RuntimeConfigError):
            docker_container_from_config(config)


def test_controller_from_config_selects_adapter_and_fails_closed() -> None:
    assert controller_from_config({}) is None
    assert (
        controller_from_config({"runtime": {"adapter": "docker", "container": "bad name"}})
        is None
    )
    assert controller_from_config({"runtime": {"adapter": "podman"}}) is None
    docker = controller_from_config(
        {"runtime": {"adapter": "docker", "container": "comfyui-local"}}
    )
    assert isinstance(docker, DockerController)


def test_restart_uses_fixed_argv_without_shell() -> None:
    controller = DockerController("comfyui-local", timeout_seconds=5)

    with patch("comfyui_mcp_skills.infrastructure.runtime.docker.subprocess.run") as run:
        run.return_value.returncode = 0
        result = controller.restart("local")

    assert result["adapter"] == "docker"
    assert result["container"] == "comfyui-local"
    run.assert_called_once_with(
        ["docker", "restart", "comfyui-local"],
        shell=False,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def test_restart_failures_are_bounded_and_raised() -> None:
    controller = DockerController("comfyui-local")

    with patch("comfyui_mcp_skills.infrastructure.runtime.docker.subprocess.run") as run:
        run.return_value.returncode = 125
        with pytest.raises(RuntimeError, match="exit code 125"):
            controller.restart("local")

    with patch("comfyui_mcp_skills.infrastructure.runtime.docker.subprocess.run") as run:
        run.side_effect = FileNotFoundError()
        with pytest.raises(RuntimeError, match="not available"):
            controller.restart("local")

    with patch("comfyui_mcp_skills.infrastructure.runtime.docker.subprocess.run") as run:
        run.side_effect = subprocess.TimeoutExpired(cmd=["docker", "restart"], timeout=5)
        with pytest.raises(RuntimeError, match="timed out"):
            controller.restart("local")
