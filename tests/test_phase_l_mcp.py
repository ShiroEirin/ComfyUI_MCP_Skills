"""Focused MCP Phase L asset-library surface contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError

from comfyui_mcp_skills.adapters.mcp.server import create_server
from comfyui_mcp_skills.adapters.mcp.tooling import phase_l_tools
from comfyui_mcp_skills.application.authorization import (
    Scope,
    Toolset,
    admitted_scopes,
    scopes_for_tool,
    tool_visible,
)

PHASE_L_NAMES = {
    "comfyui.asset.list",
    "comfyui.asset.describe",
    "comfyui.asset.collection.update",
    "comfyui.asset.metadata.extract",
    "comfyui.asset.import_output",
    "comfyui.asset.delete.plan",
    "comfyui.asset.delete.commit",
    "comfyui.asset.transfer.plan",
    "comfyui.asset.transfer.commit",
    "comfyui.asset.transfer.get",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_phase_l_declares_exact_strict_tools_and_annotations() -> None:
    tools = {tool.name: tool for tool in phase_l_tools()}

    assert set(tools) == PHASE_L_NAMES
    assert len(tools) == 10
    assert all(tool.input_schema["additionalProperties"] is False for tool in tools.values())
    assert tools["comfyui.asset.collection.update"].input_schema["properties"]["action"] == {
        "type": "string",
        "enum": ["add", "remove"],
    }
    assert tools["comfyui.asset.delete.commit"].annotations.destructive_hint is True
    assert tools["comfyui.asset.describe"].annotations.read_only_hint is True
    assert tools["comfyui.asset.transfer.get"].annotations.read_only_hint is True
    assert "reviewable" in tools["comfyui.asset.import_output"].description
    assert "Verify source bytes" in tools["comfyui.asset.transfer.plan"].description
    assert "read back" in tools["comfyui.asset.transfer.commit"].description


def test_phase_l_tools_are_execution_scoped() -> None:
    assert all(scopes_for_tool(name) == frozenset({Scope.EXECUTE}) for name in PHASE_L_NAMES)
    assert all(
        not tool_visible(name, Toolset.AUTHORING, admitted_scopes(Toolset.AUTHORING))
        for name in PHASE_L_NAMES
    )
    assert all(
        not tool_visible(name, Toolset.OPERATIONS, admitted_scopes(Toolset.OPERATIONS))
        for name in PHASE_L_NAMES
    )


@pytest.mark.anyio
async def test_file_backend_hides_phase_l_tools_and_rejects_calls(tmp_path: Path) -> None:
    server = create_server(tmp_path)

    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert not (names & PHASE_L_NAMES)
        capabilities = await client.call_tool(
            "comfyui.capability.search", {"query": "transfer", "limit": 50}
        )
        discovered = {item["name"] for item in capabilities.structured_content.get("items", [])}
        assert not (discovered & PHASE_L_NAMES)
        with pytest.raises(MCPError, match="Unknown tool"):
            await client.call_tool("comfyui.asset.list", {})
