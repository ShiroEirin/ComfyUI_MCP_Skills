"""G2 fixed Toolset and authorization contracts."""

from __future__ import annotations

import os

import pytest

from comfyui_mcp_skills.application.authorization import (
    Scope,
    Toolset,
    authorization_for_stdio,
    scopes_for_tool,
    tool_visible,
)


def test_authorization_matrix_keeps_execution_and_operations_separate() -> None:
    assert scopes_for_tool("comfyui.job.get") == frozenset({Scope.EXECUTE})
    assert scopes_for_tool("comfyui.node.list") == frozenset({Scope.OBSERVE})
    assert tool_visible("comfyui.job.get", Toolset.EXECUTION, frozenset({Scope.EXECUTE}))
    assert not tool_visible("comfyui.node.list", Toolset.EXECUTION, frozenset({Scope.EXECUTE}))
    assert tool_visible("comfyui.node.list", Toolset.OPERATIONS, frozenset({Scope.OBSERVE}))
    assert not tool_visible("comfyui.job.get", Toolset.AUTHORING, frozenset({Scope.AUTHOR}))


def test_stdio_defaults_to_local_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "COMFYUI_MCP_PRINCIPAL_ID",
        "COMFYUI_MCP_SCOPES",
        "COMFYUI_MCP_TOOLSET",
        "COMFYUI_MCP_ENABLE_HIGH_RISK",
    ):
        monkeypatch.delenv(name, raising=False)

    context = authorization_for_stdio(os.environ)

    assert context.principal_id == "local-stdio"
    assert context.scopes == frozenset({Scope.EXECUTE})
    assert context.toolset is Toolset.EXECUTION


def test_stdio_high_risk_toolsets_require_explicit_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMFYUI_MCP_PRINCIPAL_ID", "local-author")
    monkeypatch.setenv("COMFYUI_MCP_SCOPES", "comfyui:author")
    monkeypatch.setenv("COMFYUI_MCP_TOOLSET", "authoring")
    monkeypatch.delenv("COMFYUI_MCP_ENABLE_HIGH_RISK", raising=False)

    with pytest.raises(PermissionError, match="high-risk"):
        authorization_for_stdio(os.environ)

    monkeypatch.setenv("COMFYUI_MCP_ENABLE_HIGH_RISK", "1")
    context = authorization_for_stdio(os.environ)
    assert context.toolset is Toolset.AUTHORING
    assert context.scopes == frozenset({Scope.AUTHOR})


def test_stdio_rejects_unknown_and_unrelated_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMFYUI_MCP_PRINCIPAL_ID", "operator")
    monkeypatch.setenv("COMFYUI_MCP_TOOLSET", "operations")
    monkeypatch.setenv("COMFYUI_MCP_ENABLE_HIGH_RISK", "1")
    monkeypatch.setenv("COMFYUI_MCP_SCOPES", "comfyui:execute")

    with pytest.raises(PermissionError, match="does not admit"):
        authorization_for_stdio(os.environ)

    monkeypatch.setenv("COMFYUI_MCP_SCOPES", "comfyui:unknown")
    with pytest.raises(ValueError, match="unknown scope"):
        authorization_for_stdio(os.environ)
