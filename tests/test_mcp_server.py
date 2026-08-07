"""MCP tool and resource integration contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import anyio
import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import PromptReference

from comfyui_mcp_skills.adapters.mcp.admin import create_admin_server
from comfyui_mcp_skills.adapters.mcp.server import create_server
from comfyui_mcp_skills.adapters.mcp.subscriptions import WorkflowChangeMonitor
from comfyui_mcp_skills.application.authorization import AuthorizationContext, Scope, Toolset
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore


class FakeGateway:
    def __init__(self) -> None:
        self.queued: list[dict[str, Any]] = []
        self.queue_prompt_args: list[dict[str, Any]] = []
        self.histories: dict[str, dict[str, Any]] = {}
        self.interrupted: list[str] = []
        self.uploaded: list[str] = []

    def queue_prompt(self, workflow: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.queued.append(workflow)
        self.queue_prompt_args.append(kwargs)
        return {"prompt_id": "prompt-mcp", "client_id": "client-mcp"}

    def get_history(
        self, prompt_id: str, timeout_seconds: float | None = None
    ) -> dict[str, Any] | None:
        return self.histories.get(prompt_id)

    def get_queue(self, timeout_seconds: float | None = None) -> dict[str, Any]:
        return {"queue_running": [], "queue_pending": []}

    def interrupt(self, prompt_id: str = "") -> dict[str, Any]:
        self.interrupted.append(prompt_id)
        return {"success": True}

    def queue_delete(self, prompt_ids: list[str]) -> dict[str, Any]:
        return {"success": True}

    def upload_file(self, path: str, *, purpose: str, original_ref: str) -> dict[str, Any]:
        self.uploaded.append(path)
        return {
            "name": Path(path).name,
            "subfolder": "agent",
            "type": "input",
        }

    def download_output(
        self,
        filename: str,
        subfolder: str = "",
        output_type: str = "output",
        *,
        max_bytes: int,
    ) -> bytes:
        payload = b"generated-image"
        if len(payload) > max_bytes:
            raise ValueError("output too large")
        return payload

    def get_system_stats(self) -> dict[str, Any]:
        return {"system": {"os": "test"}, "devices": [{"name": "GPU"}]}

    def get_object_info(self) -> dict[str, Any]:
        return {
            "KSampler": {"display_name": "KSampler", "category": "sampling"},
            "CLIPTextEncode": {"display_name": "CLIP Text Encode", "category": "conditioning"},
        }

    def get_object_info_node(self, node_class: str) -> dict[str, Any] | None:
        return self.get_object_info().get(node_class)

    def get_model_folders(self) -> list[str]:
        return ["checkpoints", "loras"]

    def get_models(self, folder: str) -> list[str]:
        return {"checkpoints": ["sdxl.safetensors"], "loras": ["detail.safetensors"]}.get(
            folder, []
        )


def _project(tmp_path: Path) -> None:
    directory = tmp_path / "data" / "local" / "txt2img"
    directory.mkdir(parents=True)
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "default_server": "local",
                "servers": [{"id": "local", "name": "Local", "url": "http://127.0.0.1:8188"}],
            }
        ),
        encoding="utf-8",
    )
    (directory / "schema.json").write_text(
        json.dumps(
            {
                "description": "Generate an image from text",
                "enabled": True,
                "parameters": {
                    "prompt": {
                        "type": "string",
                        "required": True,
                        "node_id": "1",
                        "field": "text",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (directory / "workflow.json").write_text(
        json.dumps({"1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}}}),
        encoding="utf-8",
    )


def _add_workflow(
    base_dir: Path,
    workflow_id: str,
    *,
    parameters: dict[str, Any] | None = None,
    server_id: str = "local",
    enabled: bool = True,
) -> None:
    directory = base_dir / "data" / server_id / workflow_id
    directory.mkdir(parents=True)
    (directory / "schema.json").write_text(
        json.dumps(
            {
                "description": f"Workflow {workflow_id}",
                "enabled": enabled,
                "parameters": parameters or {},
            }
        ),
        encoding="utf-8",
    )
    (directory / "workflow.json").write_text(
        json.dumps({"1": {"class_type": "Test", "inputs": {"text": "default"}}}),
        encoding="utf-8",
    )


@pytest.mark.anyio
async def test_long_workflow_tool_names_are_bounded_stable_and_unique(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    first_id = "a" * 127 + "x"
    second_id = "a" * 127 + "y"
    _add_workflow(tmp_path, first_id)
    _add_workflow(tmp_path, second_id)

    server = create_server(tmp_path, gateway_factory=lambda _config: FakeGateway())
    async with Client(server) as client:
        first_listing = await client.list_tools()
        second_listing = await client.list_tools()

    first_names = sorted(
        tool.name for tool in first_listing.tools if tool.name.startswith("comfyui.run.")
    )
    second_names = sorted(
        tool.name for tool in second_listing.tools if tool.name.startswith("comfyui.run.")
    )
    assert first_names == second_names
    assert len(first_names) == 3
    assert len(first_names) == len(set(first_names))
    assert all(len(name) <= 128 for name in first_names)


@pytest.mark.anyio
async def test_dynamic_tool_visibility_budget_can_be_expanded(tmp_path: Path) -> None:
    _project(tmp_path)
    for index in range(12):
        _add_workflow(tmp_path, f"workflow-{index:02d}")

    server = create_server(
        tmp_path,
        gateway_factory=lambda _config: FakeGateway(),
        max_dynamic_tools=12,
    )
    async with Client(server) as client:
        listed = await client.list_tools()

    dynamic_names = [tool.name for tool in listed.tools if tool.name.startswith("comfyui.run.")]
    assert len(dynamic_names) == 12


@pytest.mark.anyio
async def test_portable_tool_names_are_api_compatible_and_dispatch(tmp_path: Path) -> None:
    _project(tmp_path)
    gateway = FakeGateway()
    server = create_server(
        tmp_path,
        gateway_factory=lambda _config: gateway,
        portable_tool_names=True,
    )

    async with Client(server) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
        assert all(re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in names)
        workflow_name = next(name for name in names if name.startswith("comfyui_run_local_txt2img"))
        submitted = await client.call_tool(
            workflow_name,
            {"prompt": "a white cat", "_execution": {"idempotency_key": "portable-1"}},
        )
        fetched = await client.call_tool(
            "comfyui_job_get",
            {"server_id": "local", "prompt_id": "prompt-mcp"},
        )
        searched = await client.call_tool(
            "comfyui_capability_search",
            {"query": "job status", "limit": 5},
        )
        described = await client.call_tool(
            "comfyui_capability_describe",
            {"name": "comfyui_job_get"},
        )

    assert submitted.is_error is False
    assert submitted.structured_content["prompt_id"] == "prompt-mcp"
    assert fetched.is_error is False
    discovered_names = {item["name"] for item in searched.structured_content["items"]}
    assert "comfyui_job_get" in discovered_names
    assert all(re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in discovered_names)
    assert described.structured_content["name"] == "comfyui_job_get"


@pytest.mark.anyio
async def test_portable_tool_name_collision_fails_closed(tmp_path: Path) -> None:
    _project(tmp_path)
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    config["servers"].extend(
        [
            {"id": "a_b", "name": "A B", "url": "http://127.0.0.1:8189"},
            {"id": "a", "name": "A", "url": "http://127.0.0.1:8190"},
        ]
    )
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    _add_workflow(tmp_path, "c", server_id="a_b")
    _add_workflow(tmp_path, "b_c", server_id="a")
    server = create_server(
        tmp_path,
        gateway_factory=lambda _config: FakeGateway(),
        portable_tool_names=True,
    )

    async with Client(server) as client:
        with pytest.raises(MCPError) as collision:
            await client.list_tools()

    assert collision.value.code == -32603


@pytest.mark.anyio
async def test_prompt_and_resource_completion_uses_visible_catalog(tmp_path: Path) -> None:
    _project(tmp_path)
    server = create_server(tmp_path, gateway_factory=lambda _config: FakeGateway())
    async with Client(server) as client:
        servers = await client.complete(
            PromptReference(name="select-or-import-workflow"),
            {"name": "server_id", "value": "lo"},
        )
        workflows = await client.complete(
            PromptReference(name="select-or-import-workflow"),
            {"name": "workflow_id", "value": "txt"},
        )

    assert servers.completion.values == ["local"]
    assert workflows.completion.values == ["txt2img"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_dynamic_workflow_tool_and_resources(tmp_path: Path) -> None:
    _project(tmp_path)
    gateway = FakeGateway()
    server = create_server(tmp_path, gateway_factory=lambda _config: gateway)

    async with Client(server) as client:
        listed = await client.list_tools()
        assert all(not tool.name.startswith("comfyui.admin.") for tool in listed.tools)
        workflow_tool = next(
            tool for tool in listed.tools if tool.name.startswith("comfyui.run.local.txt2img")
        )
        assert workflow_tool.input_schema["required"] == ["prompt"]
        assert workflow_tool.output_schema is not None

        result = await client.call_tool(
            workflow_tool.name,
            {"prompt": "a white cat", "_execution": {"idempotency_key": "mcp-1"}},
        )
        assert result.is_error is False
        assert result.structured_content["status"] == "submitted"
        assert result.structured_content["prompt_id"] == "prompt-mcp"
        invalid_options = await client.call_tool(
            workflow_tool.name,
            {"prompt": "cat", "_execution": "wait"},
        )
        assert invalid_options.is_error is True
        invalid_job = await client.call_tool(
            "comfyui.job.cancel",
            {"server_id": "local", "prompt_id": ""},
        )
        assert invalid_job.is_error is True

        image = tmp_path / "uploads" / "cat.png"
        image.parent.mkdir()
        image.write_bytes(b"\x89PNG\r\n\x1a\n")
        uploaded = await client.call_tool(
            "comfyui.asset.upload",
            {"server_id": "local", "local_path": str(image), "purpose": "image"},
        )
        assert uploaded.is_error is False
        asset_uri = uploaded.structured_content["resource_uri"]

        gateway.histories["prompt-mcp"] = {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {
                "2": {"images": [{"filename": "out.png", "subfolder": "", "type": "output"}]}
            },
        }
        completed = await client.call_tool(
            "comfyui.job.get",
            {"server_id": "local", "prompt_id": "prompt-mcp"},
        )
        assert completed.structured_content["status"] == "completed"
        job_uri = "comfyui://jobs/local/prompt-mcp"
        output_uri = completed.structured_content["outputs"][0]["resource_uri"]
        output_link = next(block for block in completed.content if block.type == "resource_link")
        assert str(output_link.uri) == output_uri
        assert output_link.mime_type == "image/png"
        assert json.loads((await client.read_resource(asset_uri)).contents[0].text)["name"]
        assert (
            json.loads((await client.read_resource(job_uri)).contents[0].text)["status"]
            == "completed"
        )
        output_resource = await client.read_resource(output_uri)
        assert base64.b64decode(output_resource.contents[0].blob) == b"generated-image"

        cancelled = await client.call_tool(
            "comfyui.job.cancel",
            {"server_id": "local", "prompt_id": "prompt-mcp"},
        )
        assert cancelled.structured_content["status"] == "completed"

        with pytest.raises(Exception) as unknown:
            await client.call_tool("comfyui.unknown", {})
        assert getattr(unknown.value, "code", None) == -32602

        resources = await client.list_resources()
        workflow_resource = next(
            resource
            for resource in resources.resources
            if resource.uri == "comfyui://workflows/local/txt2img"
        )
        read = await client.read_resource(workflow_resource.uri)
        document = json.loads(read.contents[0].text)
        assert document["workflow_id"] == "txt2img"
        assert "graph" not in document

    assert gateway.queued[0]["1"]["inputs"]["text"] == "a white cat"


@pytest.mark.anyio
async def test_resource_templates_describe_addressable_entities(tmp_path: Path) -> None:
    _project(tmp_path)
    server = create_server(tmp_path, gateway_factory=lambda _config: FakeGateway())

    async with Client(server) as client:
        templates = await client.list_resource_templates()

    uris = {template.uri_template for template in templates.resource_templates}
    assert {
        "comfyui://workflows/{server_id}/{workflow_id}",
        "comfyui://assets/{server_id}/{asset_id}",
        "comfyui://jobs/{server_id}/{prompt_id}",
        "comfyui://outputs/{server_id}/{prompt_id}/{index}",
    } <= uris


@pytest.mark.anyio
async def test_capability_search_does_not_mutate_active_tool_list(tmp_path: Path) -> None:
    _project(tmp_path)
    server = create_server(tmp_path, gateway_factory=lambda _config: FakeGateway())

    async with Client(server) as client:
        before = await client.list_tools()
        search = await client.call_tool(
            "comfyui.capability.search", {"query": "job status", "limit": 5}
        )
        described = await client.call_tool(
            "comfyui.capability.describe",
            {"name": "comfyui.job.get"},
        )
        after = await client.list_tools()

    assert search.structured_content["items"][0]["name"] == "comfyui.job.get"
    assert described.structured_content["fallbacks"] == {
        "elicitation": "approval_resource",
        "subscriptions": "resource_refetch",
        "tasks": "submitted_job_resource",
        "apps": "resource_link",
    }
    job_tool = next(tool for tool in before.tools if tool.name == "comfyui.job.get")
    assert described.structured_content["input_schema"] == job_tool.input_schema
    assert described.structured_content["output_schema"] == job_tool.output_schema
    assert [tool.name for tool in before.tools] == [tool.name for tool in after.tools]


@pytest.mark.anyio
async def test_read_only_discovery_tools_are_paginated(tmp_path: Path) -> None:
    _project(tmp_path)
    gateway = FakeGateway()
    server = create_server(
        tmp_path,
        gateway_factory=lambda _config: gateway,
        authorization=AuthorizationContext(
            "operations-test", frozenset({Scope.OBSERVE}), Toolset.OPERATIONS
        ),
    )

    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert {
            "comfyui.server.health",
            "comfyui.node.list",
            "comfyui.node.describe",
            "comfyui.model.list",
        } <= names
        health = await client.call_tool("comfyui.server.health", {"server_id": "local"})
        nodes = await client.call_tool(
            "comfyui.node.list",
            {"server_id": "local", "query": "clip", "limit": 1},
        )
        node = await client.call_tool(
            "comfyui.node.describe",
            {"server_id": "local", "node_class": "KSampler"},
        )
        models = await client.call_tool(
            "comfyui.model.list",
            {"server_id": "local", "kind": "checkpoints", "limit": 1},
        )

    assert health.structured_content["status"] == "online"
    assert health.structured_content["cancel_running_supported"] is False
    assert nodes.structured_content["items"][0]["class"] == "CLIPTextEncode"
    assert node.structured_content["node"]["category"] == "sampling"
    assert models.structured_content["items"] == ["sdxl.safetensors"]

    async with Client(server) as client:
        ordered_nodes = await client.call_tool(
            "comfyui.node.list",
            {"server_id": "local", "limit": 1},
        )

    assert ordered_nodes.structured_content["items"][0]["class"] == "CLIPTextEncode"


@pytest.mark.anyio
async def test_workflow_change_monitor_publishes_list_events(tmp_path: Path) -> None:
    _project(tmp_path)

    class RecordingBus:
        def __init__(self) -> None:
            self.events: list[object] = []

        async def publish(self, event: object) -> None:
            self.events.append(event)

    bus = RecordingBus()
    monitor = WorkflowChangeMonitor(tmp_path, bus)
    schema_path = tmp_path / "data" / "local" / "txt2img" / "schema.json"
    schema_path.write_text(schema_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    assert await monitor.check() is True
    assert [type(event).__name__ for event in bus.events] == [
        "ToolsListChanged",
        "ResourcesListChanged",
    ]
    assert await monitor.check() is False


@pytest.mark.anyio
async def test_workflow_change_scan_does_not_block_event_loop(tmp_path: Path) -> None:
    _project(tmp_path)

    class RecordingBus:
        async def publish(self, _event: object) -> None:
            return None

    monitor = WorkflowChangeMonitor(tmp_path, RecordingBus())
    order: list[str] = []

    def slow_scan() -> tuple[tuple[str, str], ...]:
        time.sleep(0.05)
        order.append("scan")
        return monitor._fingerprint

    monitor._scan = slow_scan  # type: ignore[method-assign]

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(monitor.check)
        await anyio.sleep(0.01)
        order.append("tick")

    assert order == ["tick", "scan"]


@pytest.mark.anyio
async def test_config_change_monitor_publishes_list_events_once(tmp_path: Path) -> None:
    _project(tmp_path)

    class RecordingBus:
        def __init__(self) -> None:
            self.events: list[object] = []

        async def publish(self, event: object) -> None:
            self.events.append(event)

    bus = RecordingBus()
    monitor = WorkflowChangeMonitor(tmp_path, bus)
    config_path = tmp_path / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["servers"][0]["name"] = "Updated"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    assert await monitor.check() is True
    assert [type(event).__name__ for event in bus.events] == [
        "ToolsListChanged",
        "ResourcesListChanged",
    ]
    assert await monitor.check() is False
    assert [type(event).__name__ for event in bus.events] == [
        "ToolsListChanged",
        "ResourcesListChanged",
    ]


def test_admin_server_requires_explicit_enablement(tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        create_admin_server(tmp_path)


@pytest.mark.anyio
async def test_admin_server_changes_and_deletes_workflow(tmp_path: Path) -> None:
    _project(tmp_path)
    server = create_admin_server(tmp_path, enabled=True)
    async with Client(server) as client:
        listed = await client.list_tools()
        assert {tool.name for tool in listed.tools} == {
            "comfyui.admin.workflow.set_enabled",
            "comfyui.admin.workflow.delete",
            "comfyui.admin.workflow.validate",
            "comfyui.admin.audit.get",
            "comfyui.admin.audit.retry",
            "comfyui.admin.audit.export",
        }
        assert all(tool.title for tool in listed.tools)
        assert all(tool.icons for tool in listed.tools)
        assert all(tool.meta and tool.meta.get("comfyui/risk") for tool in listed.tools)
        disabled = await client.call_tool(
            "comfyui.admin.workflow.set_enabled",
            {"server_id": "local", "workflow_id": "txt2img", "enabled": False},
        )
        assert disabled.structured_content["enabled"] is False
        refused = await client.call_tool(
            "comfyui.admin.workflow.delete",
            {
                "server_id": "local",
                "workflow_id": "txt2img",
                "confirmation": "wrong",
                "request_id": "delete-refused",
            },
        )
        invalid_enabled = await client.call_tool(
            "comfyui.admin.workflow.set_enabled",
            {"server_id": "local", "workflow_id": "txt2img", "enabled": "false"},
        )
        assert invalid_enabled.is_error is True
        assert refused.is_error is True
        deleted = await client.call_tool(
            "comfyui.admin.workflow.delete",
            {
                "server_id": "local",
                "workflow_id": "txt2img",
                "confirmation": "delete:local/txt2img",
                "request_id": "delete-committed",
            },
        )
        assert deleted.structured_content["deleted"] is True
        audit = await client.call_tool(
            "comfyui.admin.audit.get",
            {"request_id": "delete-committed"},
        )
        assert audit.structured_content["committed"] is True
        assert audit.structured_content["audit_status"] == "audited"
        exported = await client.call_tool(
            "comfyui.admin.audit.export",
            {"actor": "stdio-admin", "action": "workflow.delete", "limit": 100},
        )
        assert exported.structured_content["count"] >= 2
        assert all(
            event["request_id"] == "delete-committed"
            for event in exported.structured_content["events"]
            if event["outcome"] == "success"
        )
        invalid = await client.call_tool(
            "comfyui.admin.audit.export",
            {"outcomes": ["bogus"]},
        )
        assert invalid.is_error is True
    assert not (tmp_path / "data" / "local" / "txt2img").exists()


@pytest.mark.anyio
async def test_dynamic_run_forwards_priority_and_partial_targets(
    tmp_path: Path,
) -> None:
    """_execution.priority and partial_execution_targets reach the gateway."""
    _project(tmp_path)
    gateway = FakeGateway()
    server = create_server(tmp_path, gateway_factory=lambda _config: gateway)

    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.run.local.txt2img",
            {
                "prompt": "hello",
                "_execution": {
                    "priority": 5,
                    "partial_execution_targets": ["1"],
                },
            },
        )

    assert result.is_error is False
    assert gateway.queued
    call = gateway.queue_prompt_args[-1]
    assert call["priority"] == 5
    assert call["targets"] == ["1"]


@pytest.mark.anyio
async def test_dynamic_run_rejects_unknown_partial_target(tmp_path: Path) -> None:
    _project(tmp_path)
    gateway = FakeGateway()
    server = create_server(tmp_path, gateway_factory=lambda _config: gateway)

    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.run.local.txt2img",
            {
                "prompt": "hello",
                "_execution": {"partial_execution_targets": ["99"]},
            },
        )

    assert result.is_error is True
    assert "Invalid tool arguments" in str(result.content[0])


@pytest.mark.anyio
async def test_dynamic_run_includes_execution_options_in_digest(
    tmp_path: Path,
) -> None:
    """The same idempotency key with different priority conflicts."""
    _project(tmp_path)
    gateway = FakeGateway()
    server = create_server(tmp_path, gateway_factory=lambda _config: gateway)

    async with Client(server) as client:
        first = await client.call_tool(
            "comfyui.run.local.txt2img",
            {
                "prompt": "hello",
                "_execution": {"idempotency_key": "exec-options-1", "priority": 1},
            },
        )
        conflict = await client.call_tool(
            "comfyui.run.local.txt2img",
            {
                "prompt": "hello",
                "_execution": {"idempotency_key": "exec-options-1", "priority": 2},
            },
        )

    assert first.is_error is False
    assert conflict.is_error is True


@pytest.mark.anyio
async def test_dynamic_run_invalid_target_does_not_consume_idempotency_key(
    tmp_path: Path,
) -> None:
    """An invalid partial target leaves the idempotency key free for retry."""
    _project(tmp_path)
    gateway = FakeGateway()
    server = create_server(tmp_path, gateway_factory=lambda _config: gateway)

    async with Client(server) as client:
        invalid = await client.call_tool(
            "comfyui.run.local.txt2img",
            {
                "prompt": "hello",
                "_execution": {
                    "idempotency_key": "exec-retry-1",
                    "partial_execution_targets": ["99"],
                },
            },
        )
        retry = await client.call_tool(
            "comfyui.run.local.txt2img",
            {
                "prompt": "hello",
                "_execution": {
                    "idempotency_key": "exec-retry-1",
                    "partial_execution_targets": ["1"],
                },
            },
        )

    assert invalid.is_error is True
    assert retry.is_error is False
    assert gateway.queue_prompt_args[-1]["targets"] == ["1"]


@pytest.mark.anyio
async def test_dynamic_run_reordered_targets_do_not_conflict(
    tmp_path: Path,
) -> None:
    """Target order is set-like: the same key with reordered targets must not
    be treated as a different request."""
    _project(tmp_path)
    graph = tmp_path / "data" / "local" / "txt2img" / "workflow.json"
    graph.write_text(
        json.dumps(
            {
                "1": {"class_type": "Test", "inputs": {"text": "default"}},
                "2": {"class_type": "Test", "inputs": {"text": "default"}},
            }
        ),
        encoding="utf-8",
    )
    gateway = FakeGateway()
    server = create_server(tmp_path, gateway_factory=lambda _config: gateway)

    async with Client(server) as client:
        first = await client.call_tool(
            "comfyui.run.local.txt2img",
            {
                "prompt": "hello",
                "_execution": {
                    "idempotency_key": "exec-order-1",
                    "partial_execution_targets": ["1", "2"],
                },
            },
        )
        same = await client.call_tool(
            "comfyui.run.local.txt2img",
            {
                "prompt": "hello",
                "_execution": {
                    "idempotency_key": "exec-order-1",
                    "partial_execution_targets": ["2", "1"],
                },
            },
        )

    assert first.is_error is False
    assert same.is_error is False
    assert len(gateway.queue_prompt_args) == 1
    assert gateway.queue_prompt_args[0]["targets"] == ["1", "2"]


@pytest.mark.anyio
async def test_dynamic_run_rejects_target_with_line_break(tmp_path: Path) -> None:
    """A partial target containing a line break is rejected before submission."""
    _project(tmp_path)
    gateway = FakeGateway()
    server = create_server(tmp_path, gateway_factory=lambda _config: gateway)

    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.run.local.txt2img",
            {
                "prompt": "hello",
                "_execution": {"partial_execution_targets": ["1\r\n2"]},
            },
        )

    assert result.is_error is True
    assert not gateway.queued


@pytest.mark.anyio
async def test_workflow_list_lists_and_filters(tmp_path: Path) -> None:
    """workflow.list enumerates workflows with filtering and pagination."""
    _project(tmp_path)
    _add_workflow(
        tmp_path,
        "portrait",
        parameters={
            "prompt": {
                "type": "string",
                "required": True,
                "node_id": "1",
                "field": "text",
            }
        },
    )
    _add_workflow(
        tmp_path,
        "landscape",
        parameters={
            "prompt": {
                "type": "string",
                "required": True,
                "node_id": "1",
                "field": "text",
            }
        },
        enabled=False,
    )
    server = create_server(
        tmp_path,
        gateway_factory=lambda _config: FakeGateway(),
        authorization=AuthorizationContext(
            "author-a", frozenset({Scope.OBSERVE, Scope.AUTHOR}), Toolset.AUTHORING
        ),
    )
    async with Client(server) as client:
        all_workflows = await client.call_tool(
            "comfyui.workflow.list", {"include_disabled": True, "limit": 50}
        )
        enabled_only = await client.call_tool(
            "comfyui.workflow.list", {"limit": 50}
        )
        filtered = await client.call_tool(
            "comfyui.workflow.list", {"query": "portrait", "limit": 50}
        )
        paged = await client.call_tool("comfyui.workflow.list", {"limit": 1})

    assert all_workflows.structured_content["total"] == 3
    assert enabled_only.structured_content["total"] == 2
    assert all(
        item["enabled"] is True for item in enabled_only.structured_content["items"]
    )
    assert filtered.structured_content["total"] == 1
    assert filtered.structured_content["items"][0]["workflow_id"] == "portrait"
    assert len(paged.structured_content["items"]) == 1
    assert paged.structured_content["next_cursor"]
    second = await _workflow_list_page(
        server, paged.structured_content["next_cursor"]
    )
    assert second["total"] == 2
    assert len(second["items"]) == 1


async def _workflow_list_page(server: Any, cursor: str) -> dict[str, Any]:
    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.workflow.list", {"limit": 2, "cursor": cursor}
        )
    return result.structured_content


@pytest.mark.anyio
async def test_workflow_list_hidden_from_execution_surface(tmp_path: Path) -> None:
    """workflow.list requires observe scope; the default execution surface hides it."""
    _project(tmp_path)
    server = create_server(tmp_path, gateway_factory=lambda _config: FakeGateway())
    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        with pytest.raises(MCPError):
            await client.call_tool("comfyui.workflow.list", {})

    assert "comfyui.workflow.list" not in names


@pytest.mark.anyio
async def test_admin_workflow_validate_returns_structured_result(tmp_path: Path) -> None:
    """admin.workflow.validate checks a graph without executing it."""
    _project(tmp_path)

    class _ObjectInfoGateway:
        def get_object_info(self) -> dict[str, Any]:
            return {
                "CLIPTextEncode": {
                    "input": {"required": {"text": ["STRING"]}},
                    "input_order": {"required": ["text"]},
                    "output": ["CONDITIONING"],
                }
            }

        def get_models(self, folder: str) -> list[str]:
            return []

    server = create_admin_server(
        tmp_path,
        enabled=True,
        gateway_factory=lambda _config: _ObjectInfoGateway(),
    )
    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        result = await client.call_tool(
            "comfyui.admin.workflow.validate",
            {"server_id": "local", "workflow_id": "txt2img"},
        )

    assert "comfyui.admin.workflow.validate" in names
    content = result.structured_content
    assert content["workflow_id"] == "txt2img"
    assert content["server_id"] == "local"
    assert isinstance(content["valid"], bool)
    assert isinstance(content["issues"], list)
    assert content["node_count"] == 1
    assert "dependencies" in content
    assert content["dependencies"]["missing_models"] == []


@pytest.mark.anyio
async def test_admin_workflow_validate_missing_workflow_errors(tmp_path: Path) -> None:
    _project(tmp_path)

    class _ObjectInfoGateway:
        def get_object_info(self) -> dict[str, Any]:
            return {}

    server = create_admin_server(
        tmp_path,
        enabled=True,
        gateway_factory=lambda _config: _ObjectInfoGateway(),
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.admin.workflow.validate",
            {"server_id": "local", "workflow_id": "missing"},
        )
    assert result.is_error is True


@pytest.mark.anyio
async def test_admin_workflow_validate_rejects_bad_parameter_targets(
    tmp_path: Path,
) -> None:
    """A parameter pointing at a missing node/field invalidates the workflow."""
    _project(tmp_path)
    directory = tmp_path / "data" / "local" / "txt2img"
    schema = json.loads((directory / "schema.json").read_text(encoding="utf-8"))
    schema["parameters"] = {
        "broken": {
            "type": "string",
            "required": True,
            "node_id": "99",
            "field": "missing",
        }
    }
    (directory / "schema.json").write_text(
        json.dumps(schema, ensure_ascii=False), encoding="utf-8"
    )

    class _ObjectInfoGateway:
        def get_object_info(self) -> dict[str, Any]:
            return {
                "CLIPTextEncode": {
                    "input": {"required": {"text": ["STRING"]}},
                    "input_order": {"required": ["text"]},
                    "output": ["CONDITIONING"],
                }
            }

    server = create_admin_server(
        tmp_path,
        enabled=True,
        gateway_factory=lambda _config: _ObjectInfoGateway(),
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.admin.workflow.validate",
            {"server_id": "local", "workflow_id": "txt2img"},
        )

    # The file-backed repository rejects the workflow at read time (parameter
    # target missing), so validate surfaces the failure as an error; the SQLite
    # path surfaces it as invalid_parameter_schema via the service layer.
    assert result.is_error is True


@pytest.mark.anyio
async def test_admin_workflow_validate_reports_missing_models(tmp_path: Path) -> None:
    """Model contract entries missing from the server are reported as missing."""
    _project(tmp_path)
    directory = tmp_path / "data" / "local" / "txt2img"
    (directory / "schema.json").write_text(
        json.dumps({"enabled": True, "parameters": {}}), encoding="utf-8"
    )
    (directory / "workflow.json").write_text(
        json.dumps(
            {
                "1": {
                    "class_type": "LoraLoaderModelOnly",
                    "inputs": {
                        "model": ["2", 0],
                        "lora_name": "missing-lora.safetensors",
                    },
                },
                "2": {
                    "class_type": "CheckpointLoaderSimple",
                    "inputs": {"ckpt_name": "base.safetensors"},
                },
            }
        ),
        encoding="utf-8",
    )

    class _ModelsGateway:
        def get_object_info(self) -> dict[str, Any]:
            return {}

        def get_models(self, folder: str) -> list[str]:
            return []  # nothing available on the server

    server = create_admin_server(
        tmp_path,
        enabled=True,
        gateway_factory=lambda _config: _ModelsGateway(),
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.admin.workflow.validate",
            {"server_id": "local", "workflow_id": "txt2img"},
        )

    dependencies = result.structured_content["dependencies"]
    assert "missing-lora.safetensors" in dependencies["missing_models"]
    assert dependencies["is_ready"] is False


@pytest.mark.anyio
async def test_admin_workflow_validate_marks_inventory_errors_not_ready(
    tmp_path: Path,
) -> None:
    """An unreadable model inventory must never report a ready workflow."""
    _project(tmp_path)
    directory = tmp_path / "data" / "local" / "txt2img"
    (directory / "schema.json").write_text(
        json.dumps({"enabled": True, "parameters": {}}), encoding="utf-8"
    )
    (directory / "workflow.json").write_text(
        json.dumps(
            {
                "1": {
                    "class_type": "LoraLoaderModelOnly",
                    "inputs": {
                        "model": ["2", 0],
                        "lora_name": "lora.safetensors",
                    },
                },
                "2": {
                    "class_type": "CheckpointLoaderSimple",
                    "inputs": {"ckpt_name": "base.safetensors"},
                },
            }
        ),
        encoding="utf-8",
    )

    class _BrokenInventoryGateway:
        def get_object_info(self) -> dict[str, Any]:
            return {}

        def get_models(self, folder: str) -> list[str]:
            raise LookupError("inventory endpoint unavailable")

    server = create_admin_server(
        tmp_path,
        enabled=True,
        gateway_factory=lambda _config: _BrokenInventoryGateway(),
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.admin.workflow.validate",
            {"server_id": "local", "workflow_id": "txt2img"},
        )

    dependencies = result.structured_content["dependencies"]
    assert dependencies["missing_models"] == []
    assert dependencies["folder_errors"] != []
    assert dependencies["is_ready"] is False


@pytest.mark.anyio
async def test_admin_workflow_import_from_server_userdata(tmp_path: Path) -> None:
    """kind=server_userdata reads the workflow from ComfyUI userdata first."""
    import sqlite3
    from datetime import datetime, timezone
    from unittest.mock import patch

    _project(tmp_path)
    _USERDATA_BODY = b'{"1": {"class_type": "Test", "inputs": {}}}'
    store = SQLiteControlPlaneStore(tmp_path / "data" / "control-plane.sqlite3")
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        for kind_name in ("workflow", "revision", "deployment"):
            connection.execute(
                "INSERT INTO store_migrations("
                "aggregate_kind, version, status, checksum, switched_at"
                ") VALUES (?, 1, 'switched', ?, ?)",
                (kind_name, "a" * 64, datetime.now(timezone.utc).isoformat()),
            )
        connection.commit()

    class _ImportGateway:
        def get_object_info(self) -> dict[str, Any]:
            return {}

        def get_node_replacements(self) -> dict[str, Any]:
            return {}

    class _Response:
        status_code = 200
        headers = {"Content-Length": str(len(_USERDATA_BODY))}

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, chunk_size: int = 8192):
            payload = _USERDATA_BODY
            for index in range(0, len(payload), chunk_size):
                yield payload[index : index + chunk_size]

        def close(self) -> None:
            return None

    with patch(
        "comfyui_mcp_skills.infrastructure.comfyui.core_client.CoreClient._get",
        return_value=_Response(),
    ):
        server = create_admin_server(
            tmp_path,
            enabled=True,
            gateway_factory=lambda _config: _ImportGateway(),
        )
        async with Client(server) as client:
            result = await client.call_tool(
                "comfyui.admin.workflow.import",
                {
                    "server_id": "local",
                    "workflow_id": "from-server",
                    "source": {
                        "kind": "server_userdata",
                        "path": "workflows/from-server.json",
                    },
                },
            )

    assert result.is_error is False
    content = result.structured_content
    assert content["workflow_id"] == "from-server"
    assert content["source_format"] == "api"


@pytest.mark.anyio
async def test_admin_workflow_import_rejects_unsafe_userdata_path(
    tmp_path: Path,
) -> None:
    import sqlite3
    from datetime import datetime, timezone

    _project(tmp_path)
    store = SQLiteControlPlaneStore(tmp_path / "data" / "control-plane.sqlite3")
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        for kind_name in ("workflow", "revision", "deployment"):
            connection.execute(
                "INSERT INTO store_migrations("
                "aggregate_kind, version, status, checksum, switched_at"
                ") VALUES (?, 1, 'switched', ?, ?)",
                (kind_name, "a" * 64, datetime.now(timezone.utc).isoformat()),
            )
        connection.commit()

    class _ImportGateway:
        def get_object_info(self) -> dict[str, Any]:
            return {}

    server = create_admin_server(
        tmp_path,
        enabled=True,
        gateway_factory=lambda _config: _ImportGateway(),
    )
    async with Client(server) as client:
        for bad_path in (
            "../escape.json",
            "/absolute/path.json",
            "workflows\\windows.json",
            "workflows/not-json.txt",
            "workflows/with space.json",
        ):
            result = await client.call_tool(
                "comfyui.admin.workflow.import",
                {
                    "server_id": "local",
                    "workflow_id": "from-server",
                    "source": {"kind": "server_userdata", "path": bad_path},
                },
            )
            assert result.is_error is True, bad_path


@pytest.mark.anyio
async def test_admin_workflow_import_authorized_local_file(tmp_path: Path) -> None:
    """kind=authorized_local_file reads only from authorized upload roots."""
    import sqlite3
    from datetime import datetime, timezone

    _project(tmp_path)
    uploads = tmp_path / "uploads"
    uploads.mkdir(exist_ok=True)
    (uploads / "local.json").write_text(
        json.dumps({"1": {"class_type": "Test", "inputs": {}}}), encoding="utf-8"
    )
    store = SQLiteControlPlaneStore(tmp_path / "data" / "control-plane.sqlite3")
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        for kind_name in ("workflow", "revision", "deployment"):
            connection.execute(
                "INSERT INTO store_migrations("
                "aggregate_kind, version, status, checksum, switched_at"
                ") VALUES (?, 1, 'switched', ?, ?)",
                (kind_name, "a" * 64, datetime.now(timezone.utc).isoformat()),
            )
        connection.commit()

    class _ImportGateway:
        def get_object_info(self) -> dict[str, Any]:
            return {}

        def get_node_replacements(self) -> dict[str, Any]:
            return {}

    server = create_admin_server(
        tmp_path,
        enabled=True,
        gateway_factory=lambda _config: _ImportGateway(),
    )
    async with Client(server) as client:
        ok = await client.call_tool(
            "comfyui.admin.workflow.import",
            {
                "server_id": "local",
                "workflow_id": "from-local",
                "source": {
                    "kind": "authorized_local_file",
                    "path": str(uploads / "local.json"),
                },
            },
        )
        denied = await client.call_tool(
            "comfyui.admin.workflow.import",
            {
                "server_id": "local",
                "workflow_id": "from-local",
                "source": {
                    "kind": "authorized_local_file",
                    "path": str(tmp_path / "config.json"),
                },
            },
        )

    assert ok.is_error is False
    assert ok.structured_content["workflow_id"] == "from-local"
    assert denied.is_error is True


@pytest.mark.anyio
async def test_admin_import_source_schema_validates_all_forms(
    tmp_path: Path,
) -> None:
    """The import source schema accepts all four forms and rejects bad ones."""
    import sqlite3
    from datetime import datetime, timezone

    from jsonschema import Draft202012Validator

    _project(tmp_path)
    store = SQLiteControlPlaneStore(tmp_path / "data" / "control-plane.sqlite3")
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        for kind_name in ("workflow", "revision", "deployment"):
            connection.execute(
                "INSERT INTO store_migrations("
                "aggregate_kind, version, status, checksum, switched_at"
                ") VALUES (?, 1, 'switched', ?, ?)",
                (kind_name, "a" * 64, datetime.now(timezone.utc).isoformat()),
            )
        connection.commit()
    server = create_admin_server(tmp_path, enabled=True)
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    source_schema = tools["comfyui.admin.workflow.import"].input_schema[
        "properties"
    ]["source"]
    validator = Draft202012Validator(source_schema)

    valid_forms = [
        {"kind": "inline_json", "workflow": {"1": {}}},
        {"kind": "server_userdata", "path": "workflows/a.json"},
        {"kind": "authorized_local_file", "path": "local.json"},
        {"1": {"class_type": "Test", "inputs": {}}},  # legacy bare workflow
    ]
    for form in valid_forms:
        assert validator.is_valid(form), form

    invalid_forms = [
        {"kind": "inline_json"},  # missing workflow
        {"kind": "server_userdata"},  # missing path
        {"kind": "mystery"},  # unknown kind
        {"kind": "inline_json", "workflow": {"1": {}}, "path": "x"},  # extra key
    ]
    for form in invalid_forms:
        assert not validator.is_valid(form), form


@pytest.mark.anyio
async def test_admin_workflow_import_inline_json(tmp_path: Path) -> None:
    """kind=inline_json passes the embedded workflow object directly."""
    import sqlite3
    from datetime import datetime, timezone

    _project(tmp_path)
    store = SQLiteControlPlaneStore(tmp_path / "data" / "control-plane.sqlite3")
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        for kind_name in ("workflow", "revision", "deployment"):
            connection.execute(
                "INSERT INTO store_migrations("
                "aggregate_kind, version, status, checksum, switched_at"
                ") VALUES (?, 1, 'switched', ?, ?)",
                (kind_name, "a" * 64, datetime.now(timezone.utc).isoformat()),
            )
        connection.commit()

    class _ImportGateway:
        def get_object_info(self) -> dict[str, Any]:
            return {}

        def get_node_replacements(self) -> dict[str, Any]:
            return {}

    server = create_admin_server(
        tmp_path,
        enabled=True,
        gateway_factory=lambda _config: _ImportGateway(),
    )
    async with Client(server) as client:
        ok = await client.call_tool(
            "comfyui.admin.workflow.import",
            {
                "server_id": "local",
                "workflow_id": "inline-wf",
                "source": {
                    "kind": "inline_json",
                    "workflow": {"1": {"class_type": "Test", "inputs": {}}},
                },
            },
        )
        bad = await client.call_tool(
            "comfyui.admin.workflow.import",
            {
                "server_id": "local",
                "workflow_id": "inline-wf",
                "source": {"kind": "inline_json"},
            },
        )

    assert ok.is_error is False
    assert ok.structured_content["workflow_id"] == "inline-wf"
    assert bad.is_error is True


@pytest.mark.anyio
async def test_admin_workflow_import_rejects_oversized_local_file(
    tmp_path: Path,
) -> None:
    """authorized_local_file reads are bounded to 2 MiB."""
    import sqlite3
    from datetime import datetime, timezone

    _project(tmp_path)
    uploads = tmp_path / "uploads"
    uploads.mkdir(exist_ok=True)
    big = uploads / "big.json"
    big.write_text('{"padding": "' + "x" * (2 * 1024 * 1024) + '"}', encoding="utf-8")
    store = SQLiteControlPlaneStore(tmp_path / "data" / "control-plane.sqlite3")
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        for kind_name in ("workflow", "revision", "deployment"):
            connection.execute(
                "INSERT INTO store_migrations("
                "aggregate_kind, version, status, checksum, switched_at"
                ") VALUES (?, 1, 'switched', ?, ?)",
                (kind_name, "a" * 64, datetime.now(timezone.utc).isoformat()),
            )
        connection.commit()

    class _ImportGateway:
        def get_object_info(self) -> dict[str, Any]:
            return {}

    server = create_admin_server(
        tmp_path,
        enabled=True,
        gateway_factory=lambda _config: _ImportGateway(),
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.admin.workflow.import",
            {
                "server_id": "local",
                "workflow_id": "from-local",
                "source": {"kind": "authorized_local_file", "path": str(big)},
            },
        )
    assert result.is_error is True


@pytest.mark.anyio
async def test_admin_server_survives_workflow_cutover(tmp_path: Path) -> None:
    """After the workflow cutover the admin server starts; file-backed tools hide."""
    import sqlite3
    from datetime import datetime, timezone

    _project(tmp_path)
    store = SQLiteControlPlaneStore(tmp_path / "data" / "control-plane.sqlite3")
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        for kind in ("workflow", "revision", "deployment"):
            connection.execute(
                "INSERT INTO store_migrations("
                "aggregate_kind, version, status, checksum, switched_at"
                ") VALUES (?, 1, 'switched', ?, ?)",
                (kind, "a" * 64, datetime.now(timezone.utc).isoformat()),
            )
        connection.commit()

    server = create_admin_server(tmp_path, enabled=True)
    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        exported = await client.call_tool(
            "comfyui.admin.audit.export",
            {"actor": "stdio-admin", "limit": 100},
        )

    assert "comfyui.admin.workflow.set_enabled" not in names
    assert "comfyui.admin.workflow.delete" not in names
    assert "comfyui.admin.audit.export" in names
    assert "comfyui.admin.audit.get" in names
    assert "comfyui.admin.workflow.change.plan" in names
    assert "comfyui.admin.workflow.import" in names
    assert exported.structured_content["events"] == []
    assert exported.structured_content["count"] == 0


@pytest.mark.anyio
async def test_admin_unknown_tool_returns_invalid_params(tmp_path: Path) -> None:
    _project(tmp_path)
    server = create_admin_server(tmp_path, enabled=True)

    async with Client(server) as client:
        with pytest.raises(MCPError) as captured:
            await client.call_tool("comfyui.admin.unknown", {})

    assert captured.value.code == -32602


@pytest.mark.anyio
async def test_admin_unexpected_failure_returns_structured_error(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    server = create_admin_server(tmp_path, enabled=True)
    with patch(
        "comfyui_mcp_skills.application.admin.WorkflowAdmin.set_enabled",
        side_effect=RuntimeError("lock failed"),
    ):
        async with Client(server) as client:
            result = await client.call_tool(
                "comfyui.admin.workflow.set_enabled",
                {
                    "server_id": "local",
                    "workflow_id": "txt2img",
                    "enabled": False,
                },
            )

    assert result.is_error is True
    assert json.loads(result.content[0].text)["code"] == "INTERNAL_ERROR"


@pytest.mark.anyio
async def test_wait_progress_is_strictly_increasing(tmp_path: Path) -> None:
    _project(tmp_path)

    class ProgressGateway(FakeGateway):
        def ws_events(self, *_args: Any) -> Any:
            yield {"type": "progress", "data": {"node": "1", "value": 0, "max": 10}}
            yield {"type": "progress", "data": {"node": "1", "value": 5, "max": 10}}
            yield {"type": "executing", "data": {"node": None}}

        def get_history(
            self, prompt_id: str, timeout_seconds: float | None = None
        ) -> dict[str, Any] | None:
            return {"status": {"completed": True}, "outputs": {}}

    gateway = ProgressGateway()
    server = create_server(tmp_path, gateway_factory=lambda _config: gateway)
    observed: list[tuple[float, float | None, str | None]] = []

    async def record(progress: float, total: float | None, message: str | None) -> None:
        observed.append((progress, total, message))

    async with Client(server) as client:
        tool = next(
            tool
            for tool in (await client.list_tools()).tools
            if tool.name.startswith("comfyui.run.")
        )
        await client.call_tool(
            tool.name,
            {
                "prompt": "cat",
                "_execution": {"wait": True, "wait_timeout_seconds": 5},
            },
            progress_callback=record,
        )

    values = [progress for progress, _total, _message in observed]
    assert values
    assert all(current > previous for previous, current in zip(values, values[1:]))
    assert all(total is None for _progress, total, _message in observed)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "uri",
    [
        "comfyui://assets/local/asset_missing",
        "comfyui://jobs/local/job_missing",
        "comfyui://workflows/local/workflow_missing",
        "comfyui://unknown/foo",
    ],
)
async def test_missing_resource_returns_invalid_params(
    tmp_path: Path,
    uri: str,
) -> None:
    _project(tmp_path)
    server = create_server(tmp_path, gateway_factory=lambda _config: FakeGateway())

    async with Client(server) as client:
        with pytest.raises(MCPError) as captured:
            await client.read_resource(uri)

    assert captured.value.code == -32602


@pytest.mark.anyio
async def test_bad_schema_is_isolated_and_legacy_ui_parameters_are_exposed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _project(tmp_path)
    bad = tmp_path / "data" / "local" / "bad"
    bad_target = tmp_path / "data" / "local" / "bad-target"
    bad_target.mkdir()
    (bad_target / "schema.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "parameters": {
                    "prompt": {
                        "type": "string",
                        "required": True,
                        "node_id": "999",
                        "field": "text",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (bad_target / "workflow.json").write_text("{}", encoding="utf-8")
    bad.mkdir()
    (bad / "schema.json").write_text(
        json.dumps({"enabled": True, "parameters": None}), encoding="utf-8"
    )
    (bad / "workflow.json").write_text("{}", encoding="utf-8")
    invalid_schema = tmp_path / "data" / "local" / "invalid-schema"
    invalid_schema.mkdir()
    (invalid_schema / "schema.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "parameters": {
                    "prompt": {
                        "type": "string",
                        "enum": "not-an-array",
                        "node_id": "1",
                        "field": "text",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (invalid_schema / "workflow.json").write_text(
        json.dumps({"1": {"class_type": "Test", "inputs": {"text": ""}}}),
        encoding="utf-8",
    )
    reserved = tmp_path / "data" / "local" / "reserved"
    reserved.mkdir()
    (reserved / "schema.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "parameters": {
                    "_execution": {
                        "type": "string",
                        "node_id": "1",
                        "field": "text",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (reserved / "workflow.json").write_text(
        json.dumps({"1": {"class_type": "Test", "inputs": {"text": ""}}}),
        encoding="utf-8",
    )
    legacy = tmp_path / "data" / "local" / "legacy"
    legacy.mkdir()
    (legacy / "schema.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "parameters": {},
                "ui_parameters": {
                    "prompt_widget": {
                        "name": "prompt",
                        "type": "string",
                        "required": True,
                        "exposed": True,
                        "node_id": "1",
                        "field": "text",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (legacy / "workflow.json").write_text(
        json.dumps({"1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}}}),
        encoding="utf-8",
    )

    server = create_server(tmp_path, gateway_factory=lambda _config: FakeGateway())
    async with Client(server) as client:
        listed = await client.list_tools()

    names = {tool.name for tool in listed.tools}
    assert "comfyui.job.get" in names
    assert not any(
        name.endswith((".bad", ".bad-target", ".invalid-schema", ".reserved")) for name in names
    )
    legacy_tool = next(tool for tool in listed.tools if tool.name.endswith(".legacy"))
    assert "Skipping workflow local/bad" in caplog.text
    assert "Skipping workflow local/invalid-schema" in caplog.text
    assert legacy_tool.input_schema["required"] == ["prompt"]


def _copy_file_workflow_to_sqlite(base_dir: Path, store: SQLiteControlPlaneStore) -> None:
    """Import the file-backed txt2img workflow into the SQLite store (G3 cutover)."""
    from comfyui_mcp_skills.infrastructure.persistence.g3_migration import (
        build_g3_import_plan,
        cutover_g3_import_plan,
    )

    cutover_g3_import_plan(build_g3_import_plan(base_dir), store)


def _seed_owner_overlay_for(
    base_dir: Path, store: SQLiteControlPlaneStore, deployment_id: str
) -> None:
    """Insert the minimal owner overlay chain referencing one deployment."""
    import sqlite3

    owner = "alice"
    now = "2026-08-07T00:00:00+00:00"
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """INSERT INTO server_change_plans(
                plan_id,plan_digest,owner_id,operation,server_id,changes_json,
                expected_revision,impact_json,created_at,expires_at,resource_uri
            ) VALUES ('plan_a','{digest}','{owner}','upsert','local','{{}}',0,'{{}}',?,?,'comfyui://servers/local')""".format(
                digest="a" * 64, owner=owner
            ),
            (now, "2026-08-08T00:00:00+00:00"),
        )
        connection.execute(
            """INSERT INTO server_revisions(
                server_id,owner_id,revision,lifecycle_status,config_json,config_digest,
                plan_id,created_at
            ) VALUES ('local','{owner}',1,'active','{{}}','{digest}','plan_a',?)""".format(
                owner=owner, digest="b" * 64
            ),
            (now,),
        )
        connection.execute(
            """INSERT INTO managed_servers(
                server_id,owner_id,current_revision,current_digest,lifecycle_status,
                created_at,updated_at
            ) VALUES ('local','{owner}',1,'{digest}','active',?,?)""".format(
                owner=owner, digest="b" * 64
            ),
            (now, now),
        )
        connection.execute(
            """INSERT INTO config_workflow_deployments(
                owner_id,deployment_id,server_id,workflow_id
            ) VALUES (?, ?, 'local', 'txt2img')""",
            (owner, deployment_id),
        )
        connection.execute(
            """INSERT INTO config_workflow_states(
                owner_id,server_id,workflow_id,enabled,updated_at
            ) VALUES (?, 'local', 'txt2img', 1, ?)""",
            (owner, now),
        )
        connection.execute(
            "INSERT INTO config_workflow_snapshots(owner_id,updated_at) VALUES (?, ?)",
            (owner, now),
        )


@pytest.mark.anyio
async def test_admin_workflow_validate_uses_owner_bound_repository(
    tmp_path: Path,
) -> None:
    """validate resolves through the owner overlay like import/change does:
    an owner-referenced unpublished deployment is validatable."""
    import sqlite3
    from unittest.mock import MagicMock

    from comfyui_mcp_skills.application.authorization import (
        AuthorizationContext,
        Scope,
        Toolset,
    )
    from comfyui_mcp_skills.infrastructure.persistence.control_plane import (
        SQLiteControlPlaneStore,
    )

    _project(tmp_path)
    store = SQLiteControlPlaneStore(tmp_path / "data" / "control-plane.sqlite3")
    store.initialize()
    _copy_file_workflow_to_sqlite(tmp_path, store)

    # A second, unpublished deployment the owner references (published=0).
    revision_id = "revision_" + "b" * 64
    deployment_id = "deployment_" + "c" * 64
    graph = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "dog"}}}
    schema = {
        "description": "Owner deployment",
        "enabled": True,
        "parameters": {"prompt": {"type": "string", "node_id": "1", "field": "text"}},
    }
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO workflows(workflow_id, created_at) "
                "VALUES ('w1', '2026-07-30T00:00:00+00:00')"
        )
        connection.execute(
            """INSERT INTO workflow_revisions(
                revision_id, workflow_id, graph_json, parameter_schema_json,
                dependency_contract_json, content_digest, created_at
            ) VALUES (?, 'txt2img', ?, ?, '{}', ?, '2026-07-30T00:00:00+00:00')""",
            (
                revision_id,
                json.dumps(graph),
                json.dumps(schema),
                hashlib.sha256(b"owner-graph").hexdigest(),
            ),
        )
        connection.execute(
            """INSERT INTO workflow_deployments(
                deployment_id, workflow_id, revision_id, server_id, enabled,
                validation_status, published, created_at
            ) VALUES (?, 'txt2img', ?, 'local', 1, 'valid', 0,
                      '2026-07-30T00:00:00+00:00')""",
            (deployment_id, revision_id),
        )
    # Owner overlay referencing the unpublished deployment.
    _seed_owner_overlay_for(tmp_path, store, deployment_id)

    class _ObjectInfoGateway:
        def get_object_info(self) -> dict[str, Any]:
            return {
                "CLIPTextEncode": {
                    "input": {"required": {"text": ["STRING"]}},
                    "input_order": {"required": ["text"]},
                    "output": ["CONDITIONING"],
                }
            }

        def get_models(self, folder: str) -> list[str]:
            return []

    server = create_admin_server(
        tmp_path,
        enabled=True,
        gateway_factory=lambda _config: _ObjectInfoGateway(),
        authorization=AuthorizationContext(
            "alice",
            frozenset({Scope.OBSERVE, Scope.OPERATE, Scope.AUTHOR, Scope.CONFIGURE, Scope.AUDIT}),
            Toolset.ADMIN,
        ),
        provisioning_repository=MagicMock(),
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.admin.workflow.validate",
            {"server_id": "local", "workflow_id": "txt2img"},
        )

    assert result.is_error is False
    content = result.structured_content
    assert content["valid"] is True


@pytest.mark.anyio
async def test_admin_server_portable_tool_names_project_and_dispatch(
    tmp_path: Path,
) -> None:
    """portable_tool_names projects admin tools to underscore names and the
    dispatch restores canonical names so calls still work."""
    import sqlite3

    _project(tmp_path)
    store = SQLiteControlPlaneStore(tmp_path / "data" / "control-plane.sqlite3")
    store.initialize()
    _copy_file_workflow_to_sqlite(tmp_path, store)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """UPDATE workflow_deployments SET enabled=1
            WHERE workflow_id='txt2img'"""
        )

    class _ObjectInfoGateway:
        def get_object_info(self) -> dict[str, Any]:
            return {
                "CLIPTextEncode": {
                    "input": {"required": {"text": ["STRING"]}},
                    "input_order": {"required": ["text"]},
                    "output": ["CONDITIONING"],
                }
            }

        def get_models(self, folder: str) -> list[str]:
            return []

    server = create_admin_server(
        tmp_path,
        enabled=True,
        gateway_factory=lambda _config: _ObjectInfoGateway(),
        portable_tool_names=True,
    )
    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert "comfyui_admin_workflow_validate" in names
        assert "comfyui.admin.workflow.validate" not in names
        result = await client.call_tool(
            "comfyui_admin_workflow_validate",
            {"server_id": "local", "workflow_id": "txt2img"},
        )

    assert result.is_error is False
    assert result.structured_content["valid"] is True


@pytest.mark.anyio
async def test_engine_history_projects_bounded_records(tmp_path: Path) -> None:
    """engine.history returns a flat bounded projection of engine history."""
    _project(tmp_path)

    class _HistoryGateway(FakeGateway):
        def get_history_bounded(self, *, prompt_id: str = "", max_items: int = 10):
            return {
                "prompt-1": {
                    "prompt": {"1": {"class_type": "Text"}},
                    "outputs": {
                        "9": {"images": [{"filename": "a.png"}, {"filename": "b.png"}]}
                    },
                    "status": {"completed": True, "status_str": "success"},
                    "created_at": 100,
                },
                "prompt-2": {
                    "prompt": {},
                    "outputs": {},
                    "status": {"completed": False, "status_str": "error"},
                    "created_at": 200,
                },
                "prompt-3": {
                    "prompt": {},
                    "outputs": {},
                    "status": {"completed": True, "status_str": "weird_value"},
                },
            }

    server = create_server(
        tmp_path,
        gateway_factory=lambda _config: _HistoryGateway(),
        authorization=AuthorizationContext(
            "observe-test", frozenset({Scope.OBSERVE}), Toolset.OPERATIONS
        ),
    )
    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert "comfyui.engine.history" in names
        result = await client.call_tool(
            "comfyui.engine.history",
            {"server_id": "local", "limit": 5},
        )

    assert result.is_error is False
    content = result.structured_content
    assert content["total"] == 3
    assert content["items"][0] == {
        "prompt_id": "prompt-2",
        "status": "error",
        "outputs_count": 0,
        "created_at": "200",
    }
    assert content["items"][1] == {
        "prompt_id": "prompt-1",
        "status": "success",
        "outputs_count": 2,
        "created_at": "100",
    }
    # Unknown status_str maps to "unknown"; entries without created_at sort
    # after timed ones via reverse engine insertion order.
    assert content["items"][2] == {
        "prompt_id": "prompt-3",
        "status": "unknown",
        "outputs_count": 0,
    }
    assert "prompt" not in content["items"][0]


@pytest.mark.anyio
async def test_engine_history_single_prompt_lookup(tmp_path: Path) -> None:
    """prompt_id restricts the engine query to one record."""
    _project(tmp_path)

    class _HistoryGateway(FakeGateway):
        def get_history_bounded(self, *, prompt_id: str = "", max_items: int = 10):
            assert prompt_id == "prompt-7"
            return {
                "prompt-7": {
                    "prompt": {},
                    "outputs": {},
                    "status": {"completed": False, "status_str": "running"},
                }
            }

    server = create_server(
        tmp_path,
        gateway_factory=lambda _config: _HistoryGateway(),
        authorization=AuthorizationContext(
            "observe-test", frozenset({Scope.OBSERVE}), Toolset.OPERATIONS
        ),
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.engine.history",
            {"server_id": "local", "prompt_id": "prompt-7"},
        )

    assert result.is_error is False
    assert result.structured_content["items"][0]["status"] == "running"


@pytest.mark.anyio
async def test_engine_history_numeric_timestamps_sort_numerically(
    tmp_path: Path,
) -> None:
    """Fractional numeric created_at values must sort numerically, newest first,
    not lexicographically."""
    _project(tmp_path)

    class _HistoryGateway(FakeGateway):
        def get_history_bounded(self, *, prompt_id: str = "", max_items: int = 10):
            return {
                "prompt-a": {
                    "prompt": {}, "outputs": {},
                    "status": {"status_str": "success"}, "created_at": 11,
                },
                "prompt-b": {
                    "prompt": {}, "outputs": {},
                    "status": {"status_str": "success"}, "created_at": 9.5,
                },
                "prompt-c": {
                    "prompt": {}, "outputs": {},
                    "status": {"status_str": "success"}, "created_at": 10.5,
                },
            }

    server = create_server(
        tmp_path,
        gateway_factory=lambda _config: _HistoryGateway(),
        authorization=AuthorizationContext(
            "observe-test", frozenset({Scope.OBSERVE}), Toolset.OPERATIONS
        ),
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.engine.history",
            {"server_id": "local", "limit": 10},
        )

    assert result.is_error is False
    items = result.structured_content["items"]
    assert [item["prompt_id"] for item in items] == ["prompt-a", "prompt-c", "prompt-b"]
    assert [item["created_at"] for item in items] == ["11", "10.5", "9.5"]


@pytest.mark.anyio
async def test_node_blueprint_projects_bounded_signatures(tmp_path: Path) -> None:
    """blueprint keyword-matches nodes and bounds fields/options/outputs."""
    _project(tmp_path)

    class _ObjectInfoGateway(FakeGateway):
        def get_object_info(self) -> dict[str, Any]:
            return {
                "KSampler": {
                    "display_name": "KSampler",
                    "category": "sampling",
                    "input": {
                        "required": {
                            "sampler_name": [["euler", "ddim", "uni_pc"]],
                            "cfg": ["FLOAT", {"min": 0.0, "max": 20.0}],
                        },
                        "optional": {"seed": ["INT", {"default": 0}]},
                    },
                    "input_order": {"required": ["sampler_name", "cfg"]},
                    "output": ["LATENT"],
                },
                "UpscaleImage": {
                    "display_name": "Upscale Image",
                    "category": "image/upscaling",
                    "input": {
                        "required": {"upscale_method": [["nearest", "bilinear"]]}
                    },
                    "input_order": {"required": ["upscale_method"]},
                    "output": ["IMAGE"],
                },
                "Text": {
                    "display_name": "Text",
                    "category": "text",
                    "input": {"required": {"text": ["STRING"]}},
                    "input_order": {"required": ["text"]},
                    "output": ["STRING"],
                },
            }

    server = create_server(
        tmp_path,
        gateway_factory=lambda _config: _ObjectInfoGateway(),
        authorization=AuthorizationContext(
            "observe-test", frozenset({Scope.OBSERVE}), Toolset.OPERATIONS
        ),
    )
    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert "comfyui.node.blueprint" in names
        result = await client.call_tool(
            "comfyui.node.blueprint",
            {"server_id": "local", "query": "upscale"},
        )

    assert result.is_error is False
    content = result.structured_content
    assert content["total_matches"] == 1
    item = content["items"][0]
    assert item["class_type"] == "UpscaleImage"
    assert item["inputs"] == [
        {
            "name": "upscale_method",
            "type": "COMBO",
            "required": True,
            "options": ["nearest", "bilinear"],
        }
    ]
    assert item["outputs"] == ["IMAGE"]


@pytest.mark.anyio
async def test_node_blueprint_limits_and_bounds_inputs(tmp_path: Path) -> None:
    """More than 8 fields: only the ordered required ones are projected."""
    _project(tmp_path)

    class _WideGateway(FakeGateway):
        def get_object_info(self) -> dict[str, Any]:
            required = {f"field_{index}": ["STRING"] for index in range(12)}
            return {
                "WideNode": {
                    "display_name": "Wide Node",
                    "category": "wide",
                    "input": {"required": required},
                    "input_order": {"required": list(required)},
                    "output": ["IMAGE"],
                }
            }

    server = create_server(
        tmp_path,
        gateway_factory=lambda _config: _WideGateway(),
        authorization=AuthorizationContext(
            "observe-test", frozenset({Scope.OBSERVE}), Toolset.OPERATIONS
        ),
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.node.blueprint",
            {"server_id": "local", "query": "wide", "limit": 1},
        )

    assert result.is_error is False
    inputs = result.structured_content["items"][0]["inputs"]
    assert len(inputs) == 8
    assert inputs[0]["name"] == "field_0"
    assert inputs[-1]["name"] == "field_7"


@pytest.mark.anyio
async def test_workflow_visualize_renders_bounded_mermaid(tmp_path: Path) -> None:
    """visualize renders a bounded Mermaid flowchart with aliased nodes."""
    from comfyui_mcp_skills.application.authorization import (
        AuthorizationContext,
        Scope,
        Toolset,
    )
    from comfyui_mcp_skills.infrastructure.persistence.control_plane import (
        SQLiteControlPlaneStore,
    )

    _project(tmp_path)
    graph_path = tmp_path / "data" / "local" / "txt2img" / "workflow.json"
    graph_path.write_text(
        json.dumps(
            {
                "1": {"class_type": "Text", "inputs": {"text": "hello"}},
                "2": {
                    "class_type": "SaveImage",
                    "inputs": {"images": ["1", 0], "filename_prefix": "out"},
                },
            }
        ),
        encoding="utf-8",
    )
    store = SQLiteControlPlaneStore(tmp_path / "data" / "control-plane.sqlite3")
    store.initialize()
    _copy_file_workflow_to_sqlite(tmp_path, store)

    server = create_server(
        tmp_path,
        gateway_factory=lambda _config: FakeGateway(),
        authorization=AuthorizationContext(
            "observe-test", frozenset({Scope.OBSERVE}), Toolset.OPERATIONS
        ),
    )
    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert "comfyui.workflow.visualize" in names
        result = await client.call_tool(
            "comfyui.workflow.visualize",
            {"server_id": "local", "workflow_id": "txt2img"},
        )

    assert result.is_error is False
    content = result.structured_content
    assert content["node_count"] == 2
    assert content["mermaid"].startswith("flowchart LR")
    assert 'N1["Text"]' in content["mermaid"]
    assert 'N2["SaveImage"]' in content["mermaid"]
    assert "N1 --> N2" in content["mermaid"]


def test_graph_mermaid_bounds_and_aliases() -> None:
    """Over-limit graphs fail loudly; labels are sanitized."""
    from comfyui_mcp_skills.application.workflow_change import _graph_mermaid

    graph = {
        "a": {"class_type": 'Text"weird', "inputs": {}},
        "b": {"class_type": "SaveImage", "inputs": {"images": ["a", 0]}},
    }
    rendered = _graph_mermaid(graph)
    assert "N1 --> N2" in rendered
    assert '"weird' not in rendered  # quotes neutralized

    big = {str(index): {"class_type": "Text", "inputs": {}} for index in range(51)}
    try:
        _graph_mermaid(big)
    except ValueError as exc:
        assert "visualization limit of 50 nodes" in str(exc)
    else:
        raise AssertionError("over-limit graph must fail loudly")


@pytest.mark.anyio
async def test_workflow_visualize_hidden_in_file_mode_and_not_found_errors(
    tmp_path: Path,
) -> None:
    """visualize is gated to the SQLite store and missing workflows surface a
    workflow-not-found error instead of an internal failure."""
    _project(tmp_path)  # file-backed store

    server = create_server(
        tmp_path,
        gateway_factory=lambda _config: FakeGateway(),
        authorization=AuthorizationContext(
            "observe-test", frozenset({Scope.OBSERVE}), Toolset.OPERATIONS
        ),
    )
    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert "comfyui.workflow.visualize" not in names
        # Direct call in file mode is an unknown tool (SQLite-gated surface).
        with pytest.raises(MCPError):
            await client.call_tool(
                "comfyui.workflow.visualize",
                {"server_id": "local", "workflow_id": "txt2img"},
            )


@pytest.mark.anyio
async def test_workflow_visualize_missing_workflow_is_not_found(tmp_path: Path) -> None:
    """A missing workflow id yields a workflow-not-found error, not INTERNAL."""

    from comfyui_mcp_skills.infrastructure.persistence.control_plane import (
        SQLiteControlPlaneStore,
    )

    _project(tmp_path)
    store = SQLiteControlPlaneStore(tmp_path / "data" / "control-plane.sqlite3")
    store.initialize()
    _copy_file_workflow_to_sqlite(tmp_path, store)

    server = create_server(
        tmp_path,
        gateway_factory=lambda _config: FakeGateway(),
        authorization=AuthorizationContext(
            "observe-test", frozenset({Scope.OBSERVE}), Toolset.OPERATIONS
        ),
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.workflow.visualize",
            {"server_id": "local", "workflow_id": "missing"},
        )
    assert result.is_error is True
    assert "not found" in str(result.content[0]).lower()


def test_model_guidance_returns_family_starting_points() -> None:
    """guidance matches families by keyword and returns the full catalog on an
    empty query without ever guessing on unknown input."""
    from comfyui_mcp_skills.application.model_guidance import guidance, list_families

    assert len(list_families()) >= 5
    flux = guidance("flux")
    assert flux["items"][0]["family"] == "FLUX.1"
    assert flux["items"][0]["cfg"] == 1.0
    empty = guidance("")
    assert len(empty["items"]) == len(list_families())
    unknown = guidance("definitely-not-a-model")
    assert unknown["items"] == []


@pytest.mark.anyio
async def test_history_suggest_counts_successful_parameter_values(
    tmp_path: Path,
) -> None:
    """suggest tallies resolved plan inputs against job outcomes."""
    import sqlite3
    from datetime import datetime, timezone

    from comfyui_mcp_skills.application.suggestions import SuggestionService

    _project(tmp_path)
    store = SQLiteControlPlaneStore(tmp_path / "data" / "control-plane.sqlite3")
    store.initialize()
    _copy_file_workflow_to_sqlite(tmp_path, store)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        deployment_id = connection.execute(
            "SELECT deployment_id FROM workflow_deployments LIMIT 1"
        ).fetchone()[0]
        revision_id = connection.execute(
            "SELECT revision_id FROM workflow_deployments LIMIT 1"
        ).fetchone()[0]
        plan_ids = []
        for index, (status, steps) in enumerate(
            [("completed", 25), ("completed", 25), ("failed", 50)], start=1
        ):
            plan_id = "plan_" + "a" * 30 + f"{index:02d}"
            plan_ids.append(plan_id)
            connection.execute(
                """INSERT INTO execution_plans(
                    plan_id, workflow_id, revision_id, deployment_id, server_id,
                    resolved_inputs_json, input_digest, plan_digest, created_at
                ) VALUES (?, 'txt2img', ?, ?, 'local', ?, ?, ?, ?)""",
                (
                    plan_id,
                    revision_id,
                    deployment_id,
                    json.dumps({"steps": steps, "cfg": 7.0, "sampler_name": "euler_a"}),
                    "d" * 62 + f"{index:02d}",
                    "e" * 62 + f"{index:02d}",
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO execution_plan_owners(
                    plan_id, owner_id, revision_id, deployment_id, created_at
                ) VALUES (?, 'owner-x', ?, ?, ?)""",
                (plan_id, revision_id, deployment_id, now),
            )
            connection.execute(
                """INSERT INTO jobs(
                    job_id, workflow_id, plan_id, revision_id, deployment_id,
                    owner_id, status, created_at, created_at_source,
                    legacy_migrated, error, outputs_json, execution_origin
                ) VALUES (?, 'txt2img', ?, ?, ?, 'owner-x', ?, ?, 'suggest-test',
                          0, '', '[]', 'planned')""",
                (
                    "job_" + "b" * 31 + str(index),
                    plan_id,
                    revision_id,
                    deployment_id,
                    status,
                    now,
                ),
            )

    service = SuggestionService(store.path)
    result = service.suggest("owner-x")

    assert result["scanned_jobs"] == 3
    by_name = {item["parameter"]: item for item in result["suggestions"]}
    steps = by_name["steps"]["values"][0]
    assert steps["value"] == "25"
    assert steps["runs"] == 2
    assert steps["success_rate"] == 1.0
    failed = by_name["steps"]["values"][1]
    assert failed["value"] == "50"
    assert failed["success_rate"] == 0.0


def test_history_suggest_handles_nested_snapshots_error_status_and_truncation(
    tmp_path: Path,
) -> None:
    """Canonical planner snapshots ({arguments, resolved_inputs}) yield
    suggestions; error status counts as failure; long values truncate."""
    import sqlite3
    from datetime import datetime, timezone

    from comfyui_mcp_skills.application.suggestions import SuggestionService

    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO workflows(workflow_id, created_at) VALUES ('w1', ?)", (now,)
        )
        connection.execute(
            """INSERT INTO workflow_revisions(
                revision_id, workflow_id, graph_json, parameter_schema_json,
                dependency_contract_json, content_digest, created_at
            ) VALUES ('revision_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                      'w1', '{}', '{}', '{}',
                      'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                      ?)""",
            (now,),
        )
        connection.execute(
            """INSERT INTO workflow_deployments(
                deployment_id, workflow_id, revision_id, server_id, enabled,
                validation_status, published, created_at
            ) VALUES ('deployment_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',

                      'w1',
                      'revision_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                      'local', 1, 'valid', 1, ?)""",
            (now,),
        )
        for index, status in enumerate([("completed"), ("error")], start=1):
            plan_id = "plan_" + "c" * 62 + f"{index:02d}"
            connection.execute(
                """INSERT INTO execution_plans(
                    plan_id, workflow_id, revision_id, deployment_id, server_id,
                    resolved_inputs_json, input_digest, plan_digest, created_at
                ) VALUES (?, 'w1',
                          'revision_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                          'deployment_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                          'local', ?, ?, ?, ?)""",
                (
                    plan_id,
                    json.dumps(
                        {
                            "arguments": {"prompt": "x"},
                            "resolved_inputs": {
                                "steps": 30,
                                "long_text": "z" * 300,
                                "cfg": 7.0,
                            },
                        }
                    ),
                    "d" * 62 + f"{index:02d}",
                    "e" * 62 + f"{index:02d}",
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO execution_plan_owners(
                    plan_id, owner_id, revision_id, deployment_id, created_at
                ) VALUES (?, 'owner-y',
                          'revision_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                          'deployment_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                          ?)""",
                (plan_id, now),
            )
            connection.execute(
                """INSERT INTO jobs(
                    job_id, workflow_id, plan_id, revision_id, deployment_id,
                    owner_id, status, created_at, created_at_source,
                    legacy_migrated, error, outputs_json, execution_origin
                ) VALUES (?, 'w1', ?,
                          'revision_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                          'deployment_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                          'owner-y', ?, ?, 'suggest-test', 0, '', '[]', 'planned')""",
                ("job_" + "c" * 31 + str(index), plan_id, status, now),
            )

    result = SuggestionService(store.path).suggest("owner-y")
    assert result["scanned_jobs"] == 2
    by_name = {item["parameter"]: item for item in result["suggestions"]}
    steps = by_name["steps"]["values"][0]
    assert steps["value"] == "30"
    assert steps["runs"] == 2
    assert steps["success_rate"] == 0.5  # one completed, one error
    long_text = by_name["long_text"]["values"][0]
    assert long_text["value"] == "z" * 256  # truncated, not dropped
    assert long_text["runs"] == 2
