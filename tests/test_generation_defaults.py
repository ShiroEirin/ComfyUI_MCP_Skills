"""Tests for persistent generation defaults (`comfyui.defaults`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.shared.exceptions import MCPError

from comfyui_mcp_skills.adapters.mcp.defaults import (
    DEFAULTS_PATH_ENV,
    apply_defaults,
    clear_defaults,
    describe_defaults,
    load_defaults,
    read_action,
    save_defaults,
)


def _use_path(monkeypatch: pytest.MonkeyPatch, path: Path) -> Path:
    monkeypatch.setenv(DEFAULTS_PATH_ENV, str(path))
    return path


def test_missing_file_means_no_defaults(tmp_path: Path, monkeypatch) -> None:
    _use_path(monkeypatch, tmp_path / "defaults.json")

    assert load_defaults() == {}
    assert describe_defaults()["values"] == {}


def test_save_then_load_round_trip(tmp_path: Path, monkeypatch) -> None:
    target = _use_path(monkeypatch, tmp_path / "defaults.json")

    saved = save_defaults({"steps": 30, "cfg": 7.5, "sampler": "euler"})

    assert saved["values"] == {"cfg": 7.5, "sampler": "euler", "steps": 30}
    assert target.is_file()
    assert load_defaults() == {"cfg": 7.5, "sampler": "euler", "steps": 30}


def test_save_rejects_non_scalar_values(tmp_path: Path, monkeypatch) -> None:
    _use_path(monkeypatch, tmp_path / "defaults.json")

    with pytest.raises(ValueError):
        save_defaults({"prompt": ["a", "b"]})


def test_save_rejects_too_many_entries(tmp_path: Path, monkeypatch) -> None:
    _use_path(monkeypatch, tmp_path / "defaults.json")

    with pytest.raises(ValueError):
        save_defaults({f"p{index}": index for index in range(65)})


def test_clear_removes_the_file(tmp_path: Path, monkeypatch) -> None:
    target = _use_path(monkeypatch, tmp_path / "defaults.json")
    save_defaults({"steps": 30})

    cleared = clear_defaults()

    assert cleared["values"] == {}
    assert not target.exists()


def test_apply_defaults_fills_only_missing_declared_parameters(
    tmp_path: Path, monkeypatch
) -> None:
    _use_path(monkeypatch, tmp_path / "defaults.json")
    save_defaults({"steps": 30, "cfg": 7, "unknown": "x"})
    parameters = {"prompt": {}, "steps": {}, "cfg": {}}

    merged = apply_defaults({"prompt": "hello"}, parameters)

    assert merged == {"prompt": "hello", "steps": 30, "cfg": 7}


def test_apply_defaults_keeps_explicit_arguments(tmp_path: Path, monkeypatch) -> None:
    _use_path(monkeypatch, tmp_path / "defaults.json")
    save_defaults({"steps": 30})

    merged = apply_defaults({"steps": 8}, {"steps": {}})

    assert merged == {"steps": 8}


def test_apply_defaults_without_defaults_is_a_passthrough(
    tmp_path: Path, monkeypatch
) -> None:
    _use_path(monkeypatch, tmp_path / "defaults.json")
    arguments = {"prompt": "x"}

    assert apply_defaults(arguments, {"prompt": {}}) is arguments


def test_read_action_validates() -> None:
    assert read_action("get") == "get"

    with pytest.raises(ValueError):
        read_action("destroy")


def test_corrupt_file_is_reported(tmp_path: Path, monkeypatch) -> None:
    target = _use_path(monkeypatch, tmp_path / "defaults.json")
    target.write_text("{not json", encoding="utf-8")

    with pytest.raises(MCPError):
        load_defaults()


def test_apply_defaults_ignores_a_corrupt_file(tmp_path: Path, monkeypatch) -> None:
    target = _use_path(monkeypatch, tmp_path / "defaults.json")
    target.write_text("{not json", encoding="utf-8")

    assert apply_defaults({"prompt": "x"}, {"prompt": {}, "steps": {}}) == {
        "prompt": "x"
    }


def test_defaults_tool_is_registered_for_execution_only() -> None:
    from comfyui_mcp_skills.adapters.mcp.tooling import fixed_tools
    from comfyui_mcp_skills.application.authorization import (
        Toolset,
        admitted_scopes,
        tool_visible,
    )

    tools = {tool.name: tool for tool in fixed_tools()}

    assert "comfyui.defaults" in tools
    assert tools["comfyui.defaults"].input_schema["required"] == []
    assert tool_visible(
        "comfyui.defaults", Toolset.EXECUTION, admitted_scopes(Toolset.EXECUTION)
    )
    assert not tool_visible(
        "comfyui.defaults", Toolset.OPERATIONS, admitted_scopes(Toolset.OPERATIONS)
    )
