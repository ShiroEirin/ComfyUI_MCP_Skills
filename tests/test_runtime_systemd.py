"""Systemd RuntimeController adapter contracts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from comfyui_mcp_skills.infrastructure.runtime.systemd import (
    RuntimeConfigError,
    SystemdController,
    controller_from_config,
    systemd_unit_from_config,
)


def test_unit_from_config_accepts_service_names_and_rejects_unsafe_shapes() -> None:
    assert (
        systemd_unit_from_config(
            {"runtime": {"adapter": "systemd", "unit": "comfyui-local.service"}}
        )
        == "comfyui-local.service"
    )

    for config in (
        {},
        {"runtime": {}},
        {"runtime": {"adapter": "systemd"}},
        {"runtime": {"adapter": "docker"}},
        {"runtime": {"adapter": "systemd", "unit": "comfyui-local"}},
        {"runtime": {"adapter": "systemd", "unit": "-comfyui.service"}},
        {"runtime": {"adapter": "systemd", "unit": "../comfyui.service"}},
        {"runtime": {"adapter": "systemd", "unit": "comfyui local.service"}},
    ):
        with pytest.raises(RuntimeConfigError):
            systemd_unit_from_config(config)


def test_controller_from_config_fails_closed_to_none() -> None:
    assert controller_from_config({}) is None
    assert (
        controller_from_config({"runtime": {"adapter": "systemd", "unit": "bad unit"}})
        is None
    )
    built = controller_from_config(
        {"runtime": {"adapter": "systemd", "unit": "comfyui-local.service"}}
    )
    assert isinstance(built, SystemdController)


def test_restart_uses_fixed_argv_without_shell(tmp_path: Path) -> None:
    controller = SystemdController("comfyui-local.service", timeout_seconds=5)

    with patch("comfyui_mcp_skills.infrastructure.runtime.systemd.subprocess.run") as run:
        run.return_value.returncode = 0
        result = controller.restart("local")

    assert result["adapter"] == "systemd"
    assert result["unit"] == "comfyui-local.service"
    run.assert_called_once_with(
        ["systemctl", "restart", "comfyui-local.service"],
        shell=False,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def test_restart_failures_are_bounded_and_raised(tmp_path: Path) -> None:
    controller = SystemdController("comfyui-local.service")

    with patch("comfyui_mcp_skills.infrastructure.runtime.systemd.subprocess.run") as run:
        run.return_value.returncode = 1
        with pytest.raises(RuntimeError, match="exit code 1"):
            controller.restart("local")

    with patch("comfyui_mcp_skills.infrastructure.runtime.systemd.subprocess.run") as run:
        run.side_effect = FileNotFoundError()
        with pytest.raises(RuntimeError, match="not available"):
            controller.restart("local")
