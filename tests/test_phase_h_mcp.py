"""Focused MCP Phase H tool surface and dispatch contracts."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.shared.exceptions import MCPError

from comfyui_mcp_skills.adapters.mcp.server import create_server
from comfyui_mcp_skills.adapters.mcp.tooling import fixed_tools, job_dict, phase_h_tools
from comfyui_mcp_skills.application.authorization import (
    AuthorizationContext,
    Scope,
    Toolset,
    admitted_scopes,
    scopes_for_tool,
    tool_visible,
)
from comfyui_mcp_skills.application.jobs import JobService
from comfyui_mcp_skills.domain.models import Job
from comfyui_mcp_skills.infrastructure.persistence.repository_factory import (
    create_repository_bundle,
)

PHASE_H_NAMES = {
    "comfyui.job.list",
    "comfyui.queue.list",
    "comfyui.log.read",
    "comfyui.server.capabilities",
    "comfyui.template.list",
    "comfyui.subgraph.list",
    "comfyui.subgraph.get",
    "comfyui.server.free",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class PhaseHGateway:
    def __init__(self) -> None:
        self.free_calls: list[tuple[bool, bool]] = []

    def get_queue(self) -> dict[str, Any]:
        return {
            "queue_running": [[1, "prompt-running", {"secret": "prompt payload"}]],
            "queue_pending": [[2, "prompt-pending", {"prompt": "must not escape"}]],
        }

    def get_logs(self) -> dict[str, Any]:
        return {
            "state": "supported",
            "data": {
                "entries": [
                    {
                        "timestamp": "2026-07-31T12:00:00Z",
                        "level": "ERROR",
                        "message": "authorization=Bearer hidden C:\\Users\\alice\\workflow.json",
                    }
                ]
            },
        }

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "state": "supported",
            "data": {
                "jobs_api": {"state": "unsupported"},
                "userdata_v2": {"state": "unauthorized"},
                "userdata_traditional": {"state": "supported"},
                "userdata": {"state": "supported", "variant": "traditional"},
                "node_replacements": {"state": "temporarily_unavailable"},
                "manager_queue_status": {"state": "unsupported"},
                "manager_install": {"state": "unauthorized"},
                "logs": {"state": "supported"},
                "workflow_templates": {"state": "supported"},
                "subgraphs": {"state": "supported"},
            },
        }

    def get_workflow_templates(self) -> dict[str, Any]:
        return {
            "state": "supported",
            "data": {
                "templates": [
                    {
                        "id": "template-1",
                        "name": "Safe template",
                        "description": "Public summary",
                        "category": "image",
                        "source": "userdata",
                        "prompt": {"1": {"inputs": {"password": "hidden"}}},
                    }
                ]
            },
        }

    def get_subgraphs(self) -> dict[str, Any]:
        return {
            "state": "supported",
            "data": {
                "subgraphs": [
                    {
                        "id": "subgraph-1",
                        "name": "Reusable component",
                        "source": "userdata",
                        "info": {"node_pack": "core"},
                        "data": {"nodes": [{"secret": "hidden"}]},
                    }
                ]
            },
        }

    def get_subgraph(self, _subgraph_id: str) -> dict[str, Any]:
        return {
            "state": "supported",
            "data": {
                "id": "subgraph-1",
                "name": "Reusable component",
                "source": "userdata",
                "info": {"node_pack": "core"},
                "data": {"nodes": [{"id": 1}], "links": [[1, 2]]},
            },
        }

    def free_memory(self, *, unload_models: bool, free_memory: bool) -> dict[str, Any]:
        self.free_calls.append((unload_models, free_memory))
        return {"success": True}


def _project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(
        json.dumps(
            {
                "default_server": "local",
                "servers": [{"id": "local", "name": "Local", "url": "http://127.0.0.1:8188"}],
            }
        ),
        encoding="utf-8",
    )


def test_phase_h_publishes_exact_strict_schemas_and_safe_annotations() -> None:
    tools = {tool.name: tool for tool in phase_h_tools()}

    assert set(tools) == PHASE_H_NAMES
    assert len(tools) == 8
    assert all(tool.input_schema.get("additionalProperties") is False for tool in tools.values())
    assert all(tool.output_schema.get("additionalProperties") is False for tool in tools.values())
    assert tools["comfyui.job.list"].input_schema["properties"]["limit"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
        "default": 50,
    }
    assert tools["comfyui.log.read"].input_schema["properties"]["limit"]["maximum"] == 1000
    assert tools["comfyui.server.free"].input_schema["anyOf"]
    assert tools["comfyui.server.free"].annotations.destructive_hint is True
    assert tools["comfyui.server.free"].annotations.read_only_hint is False
    assert all(
        tools[name].annotations.read_only_hint is True
        for name in PHASE_H_NAMES - {"comfyui.server.free"}
    )


def test_phase_h_identifier_and_cursor_schemas_match_runtime_validators() -> None:
    tools = {tool.name: tool for tool in phase_h_tools()}
    server_schema = tools["comfyui.queue.list"].input_schema["properties"]["server_id"]
    subgraph_schema = tools["comfyui.subgraph.get"].input_schema["properties"]["subgraph_id"]

    def accepts(schema: dict[str, Any], value: str) -> bool:
        validator = Draft202012Validator(
            {
                "type": "object",
                "properties": {"value": schema},
                "required": ["value"],
            }
        )
        return not list(validator.iter_errors({"value": value}))

    assert accepts(server_schema, "local_server-1")
    assert not accepts(server_schema, "prefix:local")
    assert not accepts(server_schema, "local.suffix")
    assert not accepts(server_schema, "local\n")
    assert accepts(subgraph_schema, "pack.graph-1")
    assert not accepts(subgraph_schema, "pack:graph")
    assert tools["comfyui.job.list"].input_schema["properties"]["cursor"]["maxLength"] == 2048
    assert tools["comfyui.queue.list"].input_schema["properties"]["cursor"]["maxLength"] == 512


def test_public_job_projection_is_a_secret_free_allowlist() -> None:
    job = Job(
        prompt_id="prompt-1",
        server_id="local",
        workflow_id="workflow",
        status="error",
        outputs=(
            {
                "filename": "result.png",
                "subfolder": "safe",
                "type": "output",
                "media_type": "image",
                "token": "secret-token",
                "path": "C:/private/result.png",
                "prompt": "private prompt",
                "graph": {"nodes": []},
            },
        ),
        error="Authorization: Bearer secret-token C:/private/input.png",
    )
    public = job_dict(job)
    assert set(public["outputs"][0]) == {
        "filename",
        "subfolder",
        "type",
        "media_type",
        "mime_type",
        "resource_uri",
    }
    serialized = json.dumps(public)
    assert "secret-token" not in serialized
    assert "private prompt" not in serialized
    assert "C:/private" not in serialized
    assert public["error"] == "Workflow execution failed"


def test_phase_h_scopes_and_toolset_budgets_remain_fixed() -> None:
    assert scopes_for_tool("comfyui.job.list") == frozenset({Scope.EXECUTE})
    for name in PHASE_H_NAMES - {"comfyui.job.list", "comfyui.server.free"}:
        assert scopes_for_tool(name) == frozenset({Scope.OBSERVE})
    assert scopes_for_tool("comfyui.server.free") == frozenset({Scope.OPERATE})

    surface = [*fixed_tools(), *phase_h_tools()]
    execution = [
        tool
        for tool in surface
        if tool_visible(tool.name, Toolset.EXECUTION, admitted_scopes(Toolset.EXECUTION))
    ]
    operations = [
        tool
        for tool in surface
        if tool_visible(tool.name, Toolset.OPERATIONS, admitted_scopes(Toolset.OPERATIONS))
    ]
    assert len(execution) <= 16
    assert len(operations) <= 20


@pytest.mark.anyio
async def test_default_execution_surface_rejects_hidden_operations_calls(tmp_path: Path) -> None:
    _project(tmp_path)
    server = create_server(tmp_path, gateway_factory=lambda _config: PhaseHGateway())
    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert "comfyui.server.free" not in names
        with pytest.raises(MCPError, match="Unknown tool"):
            await client.call_tool(
                "comfyui.server.free",
                {"server_id": "local", "free_memory": True},
            )


@pytest.mark.anyio
async def test_operations_tools_dispatch_redacted_bounded_results_without_list_mutation(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    gateway = PhaseHGateway()
    server = create_server(
        tmp_path,
        gateway_factory=lambda _config: gateway,
        authorization=AuthorizationContext(
            "operations-test",
            frozenset({Scope.OBSERVE, Scope.OPERATE}),
            Toolset.OPERATIONS,
        ),
    )

    async with Client(server) as client:
        before = await client.list_tools()
        names = {tool.name for tool in before.tools}
        assert PHASE_H_NAMES - {"comfyui.job.list", "comfyui.server.free"} <= names
        assert {"comfyui.job.list", "comfyui.server.free"}.isdisjoint(names)

        queue = await client.call_tool("comfyui.queue.list", {"server_id": "local", "limit": 1})
        logs = await client.call_tool("comfyui.log.read", {"server_id": "local", "limit": 1})
        capabilities = await client.call_tool("comfyui.server.capabilities", {"server_id": "local"})
        templates = await client.call_tool(
            "comfyui.template.list", {"server_id": "local", "limit": 1}
        )
        subgraphs = await client.call_tool(
            "comfyui.subgraph.list", {"server_id": "local", "limit": 1}
        )
        subgraph = await client.call_tool(
            "comfyui.subgraph.get", {"server_id": "local", "subgraph_id": "subgraph-1"}
        )
        after = await client.list_tools()

    assert queue.structured_content["items"] == [
        {"state": "running", "queue_number": 1, "prompt_id": "prompt-running"}
    ]
    assert queue.structured_content["next_cursor"]
    serialized_logs = json.dumps(logs.structured_content).casefold()
    assert "bearer hidden" not in serialized_logs
    assert "users\\alice" not in serialized_logs
    assert capabilities.structured_content["capabilities"]["jobs_api"]["state"] == "unsupported"
    assert capabilities.structured_content["capabilities"]["userdata_v2"]["state"] == "unauthorized"
    assert templates.structured_content["items"][0] == {
        "template_id": "template-1",
        "name": "Safe template",
        "description": "Public summary",
        "category": "image",
        "source": "userdata",
    }
    assert "data" not in subgraphs.structured_content["items"][0]
    assert subgraph.structured_content["subgraph"]["node_count"] == 1
    assert subgraph.structured_content["subgraph"]["link_count"] == 1
    assert [tool.name for tool in before.tools] == [tool.name for tool in after.tools]


@pytest.mark.anyio
async def test_sqlite_job_list_dispatches_filters_and_rejects_unsafe_limits(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    repositories = replace(create_repository_bundle(tmp_path), run_store="sqlite")
    captured: list[dict[str, Any]] = []

    def fake_list(_self: JobService, **arguments: Any) -> dict[str, Any]:
        captured.append(arguments)
        return {"items": [], "next_cursor": ""}

    with patch.object(JobService, "list", fake_list):
        server = create_server(
            tmp_path,
            repositories=repositories,
            gateway_factory=lambda _config: PhaseHGateway(),
        )
        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            result = await client.call_tool(
                "comfyui.job.list",
                {
                    "status": "completed",
                    "workflow_id": "txt2img",
                    "server_id": "local",
                    "created_after": "2026-07-01T00:00:00Z",
                    "limit": 25,
                    "cursor": "",
                },
            )
            invalid = await client.call_tool("comfyui.job.list", {"limit": 101})

    assert "comfyui.job.list" in names
    assert result.structured_content == {"items": [], "next_cursor": ""}
    assert captured[0] == {
        "owner_id": "local-stdio",
        "status": "completed",
        "workflow_id": "txt2img",
        "server_id": "local",
        "created_after": "2026-07-01T00:00:00Z",
        "limit": 25,
        "cursor": "",
    }
    assert invalid.is_error is True
