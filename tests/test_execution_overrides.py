"""Tests for one-run-only node input overrides (`_overrides`)."""

from __future__ import annotations

import pytest

from comfyui_mcp_skills.adapters.mcp.tooling import OVERRIDES_PROPERTY
from comfyui_mcp_skills.application.execution import (
    RISKY_NODE_RULES,
    RiskyWorkflowNodesError,
    _apply_overrides,
    scan_risky_nodes,
)
from comfyui_mcp_skills.application.planning import normalize_overrides
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


def test_normalize_overrides_is_order_independent() -> None:
    first = normalize_overrides({"7.steps": 30, "4.ckpt_name": "b"})
    second = normalize_overrides({"4.ckpt_name": "b", "7.steps": 30})

    assert first == second == {"4.ckpt_name": "b", "7.steps": 30}
    assert list(first) == ["4.ckpt_name", "7.steps"]


def test_override_rejects_declared_media_parameter_with_upload_hint() -> None:
    graph = {"5": {"class_type": "CustomImageLoader", "inputs": {"source": "base.png"}}}
    parameters = {"image": {"node_id": "5", "field": "source", "type": "image"}}

    with pytest.raises(WorkflowArgumentsError) as excinfo:
        _apply_overrides(graph, {"5.source": "asset_" + "a" * 32}, parameters=parameters)

    message = str(excinfo.value)
    assert "5.source" in message
    assert "comfyui.asset.upload" in message
    assert excinfo.value.details == {
        "target": "5.source",
        "field": "source",
        "parameter_type": "image",
    }
    assert graph["5"]["inputs"]["source"] == "base.png"


def test_override_rejects_undeclared_media_field_by_name() -> None:
    graph = {
        "9": {
            "class_type": "VHS_LoadVideo",
            "inputs": {"video": "clip.mp4", "frame_load_cap": 1},
        }
    }

    with pytest.raises(WorkflowArgumentsError) as excinfo:
        _apply_overrides(graph, {"9.video": "other.mp4"})

    assert "9.video" in str(excinfo.value)
    assert graph["9"]["inputs"]["video"] == "clip.mp4"

    _apply_overrides(graph, {"9.frame_load_cap": 8})
    assert graph["9"]["inputs"]["frame_load_cap"] == 8


def test_risk_rule_table_covers_code_network_and_paid_api() -> None:
    assert {rule.category for rule in RISKY_NODE_RULES} == {
        "code_execution",
        "network_request",
        "external_api",
    }


def test_scan_risky_nodes_leaves_local_graphs_clean() -> None:
    graph = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "m.safetensors"}},
        "2": {"class_type": "Capitalize", "inputs": {"text": "x"}},
        "3": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 20}},
    }

    assert scan_risky_nodes(graph) == ()


def test_scan_risky_nodes_returns_sorted_structured_findings() -> None:
    graph = {
        "10": {"class_type": "SomeNode", "inputs": {"api_key": "k"}},
        "2": {"class_type": "HTTP Request", "inputs": {}},
        "1": {"class_type": "EvalNode", "inputs": {}},
    }

    findings = scan_risky_nodes(graph)

    assert [(item["node_id"], item["category"]) for item in findings] == [
        ("1", "code_execution"),
        ("2", "network_request"),
        ("10", "external_api"),
    ]
    assert {item["severity"] for item in findings} == {"high"}
    assert all(item["reason"] for item in findings)
    error = RiskyWorkflowNodesError(findings)
    assert error.code == "RISKY_WORKFLOW_NODES"
    assert error.details["allow_risky_nodes"] is True
    assert len(error.details["risky_nodes"]) == 3
