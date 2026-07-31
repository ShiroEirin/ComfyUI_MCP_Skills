"""G3 MCP authoring Toolset contracts."""

from __future__ import annotations

from comfyui_mcp_skills.adapters.mcp.tooling import fixed_tools


def test_authoring_tools_publish_strict_read_only_schemas() -> None:
    tools = {tool.name: tool for tool in fixed_tools()}

    assert {"comfyui.revision.list", "comfyui.workflow.describe"} <= tools.keys()
    assert tools["comfyui.revision.list"].input_schema == {
        "type": "object",
        "properties": {"workflow_id": {"type": "string", "minLength": 1}},
        "required": ["workflow_id"],
        "additionalProperties": False,
    }
    assert tools["comfyui.workflow.describe"].input_schema == {
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string", "minLength": 1},
            "server_id": {"type": "string", "minLength": 1},
        },
        "required": ["workflow_id", "server_id"],
        "additionalProperties": False,
    }
    for name in ("comfyui.revision.list", "comfyui.workflow.describe"):
        tool = tools[name]
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.open_world_hint is False
