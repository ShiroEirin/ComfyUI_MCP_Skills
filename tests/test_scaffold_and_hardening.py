"""Rounding tests for scaffold baselines, secret refs, and risk-rule tuning."""

from __future__ import annotations

from typing import Any

import pytest

from comfyui_mcp_skills.application.execution import scan_risky_nodes
from comfyui_mcp_skills.application.workflow_conversion import detect_workflow_format
from comfyui_mcp_skills.application.workflow_graph import (
    WorkflowGraphService,
    WorkflowValidationService,
)
from comfyui_mcp_skills.application.workflow_import import WorkflowImportService
from comfyui_mcp_skills.domain.workflow_semantics import (
    DependencyExtractorRegistry,
    ParameterRoleRegistry,
)
from comfyui_mcp_skills.infrastructure.comfyui.gateway import _resolve_secret_refs


class _RevisionWriter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_revision(
        self,
        *,
        workflow_id: str,
        server_id: str,
        graph: dict[str, Any],
        parameter_schema: dict[str, Any],
        dependency_contract: dict[str, Any],
        content_digest: str,
    ) -> dict[str, Any]:
        call = {
            "workflow_id": workflow_id,
            "server_id": server_id,
            "graph": graph,
            "parameter_schema": parameter_schema,
            "dependency_contract": dependency_contract,
            "content_digest": content_digest,
        }
        self.calls.append(call)
        return {"revision_id": "revision_" + "0" * 64, **call}


def _import_service() -> tuple[WorkflowImportService, _RevisionWriter]:
    graph = WorkflowGraphService(
        ParameterRoleRegistry.default(), DependencyExtractorRegistry.default()
    )
    writer = _RevisionWriter()
    return WorkflowImportService(graph, WorkflowValidationService(), writer), writer


# --- scaffold baseline -------------------------------------------------------


def test_empty_mapping_is_an_empty_api_graph() -> None:
    assert detect_workflow_format({}) == "api"
    assert detect_workflow_format({"1": {"class_type": "KSampler", "inputs": {}}}) == "api"
    with pytest.raises(ValueError):
        detect_workflow_format({"1": {"nope": True}})


def test_empty_graph_is_invalid_unless_the_caller_asks_for_a_scaffold() -> None:
    validation = WorkflowValidationService()

    rejected = validation.validate_api({})
    assert rejected["valid"] is False
    assert rejected["issues"][0]["code"] == "invalid_graph"

    assert validation.validate_api({}, allow_empty=True)["valid"] is True


def test_preview_and_commit_accept_an_empty_scaffold_baseline() -> None:
    service, writer = _import_service()

    preview = service.preview(
        {},
        workflow_id="scaffold",
        server_id="local",
        object_info={},
        allow_empty=True,
    )

    assert preview.graph == {}
    assert preview.allow_empty_graph is True
    assert preview.requires_manual_review is False

    committed = service.commit(preview)
    assert committed["revision_id"]
    assert writer.calls[0]["graph"] == {}


def test_preview_still_rejects_an_empty_graph_by_default() -> None:
    service, _writer = _import_service()

    preview = service.preview(
        {},
        workflow_id="scaffold",
        server_id="local",
        object_info={},
    )

    assert preview.requires_manual_review is True


# --- secret_refs -------------------------------------------------------------


def test_secret_refs_resolve_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_COMFY_AUTH", "Bearer abc")
    monkeypatch.setenv("TEST_COMFY_KEY", "key-123")

    resolved = _resolve_secret_refs(
        {"secret_refs": {"auth": "env:TEST_COMFY_AUTH", "comfy_api_key": "env:TEST_COMFY_KEY"}}
    )

    assert resolved == {"auth": "Bearer abc", "comfy_api_key": "key-123"}


def test_secret_refs_absent_keeps_the_plain_fields() -> None:
    assert _resolve_secret_refs({}) == {}


def test_secret_refs_reject_unknown_scheme_field_and_missing_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="unsupported scheme"):
        _resolve_secret_refs({"secret_refs": {"auth": "vault:path"}})

    with pytest.raises(ValueError, match="not a supported credential field"):
        _resolve_secret_refs({"secret_refs": {"token": "env:SOMETHING"}})

    monkeypatch.delenv("TEST_MISSING_SECRET", raising=False)
    with pytest.raises(ValueError, match="which is not set"):
        _resolve_secret_refs({"secret_refs": {"auth": "env:TEST_MISSING_SECRET"}})

    with pytest.raises(ValueError, match="must be an object"):
        _resolve_secret_refs({"secret_refs": ["env:X"]})


# --- risk rule tuning -------------------------------------------------------


def _graph(class_type: str, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"1": {"class_type": class_type, "inputs": inputs or {}}}


def test_local_nodes_no_longer_match_broad_tokens() -> None:
    # Each of these used to match the old "execute" / "api" / "code" tokens.
    assert scan_risky_nodes(_graph("ExecutePrompt")) == ()
    assert scan_risky_nodes(_graph("APIImageLoader")) == ()
    assert scan_risky_nodes(_graph("StringConstant", {"code": "hello"})) == ()


def test_real_risky_nodes_still_match() -> None:
    assert scan_risky_nodes(_graph("PythonNode"))[0]["category"] == "code_execution"
    assert scan_risky_nodes(_graph("OpenAIAPI"))[0]["category"] == "external_api"
    assert scan_risky_nodes(_graph("HTTPRequest"))[0]["category"] == "network_request"
