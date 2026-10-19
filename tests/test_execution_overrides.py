"""Tests for one-run-only node input overrides (`_overrides`)."""

from __future__ import annotations

import pytest

from comfyui_mcp_skills.adapters.mcp.tooling import OVERRIDES_PROPERTY
from comfyui_mcp_skills.application.execution import _apply_overrides
from comfyui_mcp_skills.domain.errors import WorkflowArgumentsError
from comfyui_mcp_skills.domain.workflow_schema import normalize_parameters


def _graph() -> dict:
    return {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "base.safetensors"},
        },
        "7": {"class_type": "KSampler", "inputs": {"steps": 20, "seed": 1}},
    }


def test_override_replaces_scalar_inputs() -> None:
    graph = _graph()

    _apply_overrides(graph, {"4.ckpt_name": "other.safetensors", "7.steps": 30})

    assert graph["4"]["inputs"]["ckpt_name"] == "other.safetensors"
    assert graph["7"]["inputs"]["steps"] == 30
    assert graph["7"]["inputs"]["seed"] == 1


def test_override_rejects_unknown_node() -> None:
    with pytest.raises(WorkflowArgumentsError):
        _apply_overrides(_graph(), {"99.steps": 1})


def test_override_rejects_missing_input() -> None:
    with pytest.raises(WorkflowArgumentsError):
        _apply_overrides(_graph(), {"7.sampler_name": "euler"})


def test_override_rejects_target_without_field() -> None:
    with pytest.raises(WorkflowArgumentsError):
        _apply_overrides(_graph(), {"7": 1})


def test_override_rejects_graph_wiring() -> None:
    with pytest.raises(WorkflowArgumentsError):
        _apply_overrides(_graph(), {"7.steps": ["4", 0]})


def test_override_rejects_too_many_entries() -> None:
    graph = _graph()
    graph["9"] = {"class_type": "Any", "inputs": {f"f{index}": 1 for index in range(70)}}

    with pytest.raises(ValueError):
        _apply_overrides(graph, {f"9.f{index}": 1 for index in range(65)})


def test_override_rejects_non_object_payload() -> None:
    with pytest.raises(TypeError):
        _apply_overrides(_graph(), ["7.steps"])


def test_reserved_parameter_names_are_rejected() -> None:
    for name in ("_execution", "_overrides"):
        with pytest.raises(ValueError):
            normalize_parameters(
                {"parameters": {name: {"node_id": "7", "field": "steps"}}}
            )


def test_overrides_schema_accepts_scalars_only() -> None:
    assert OVERRIDES_PROPERTY["type"] == "object"
    assert OVERRIDES_PROPERTY["maxProperties"] == 64
    assert OVERRIDES_PROPERTY["additionalProperties"]["type"] == [
        "boolean",
        "integer",
        "number",
        "string",
    ]
