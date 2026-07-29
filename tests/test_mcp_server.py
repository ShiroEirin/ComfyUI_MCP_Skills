"""MCP tool and resource integration contracts."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import patch
from typing import Any

import pytest
from mcp import Client

from comfyui_mcp_skills.adapters.mcp.admin import create_admin_server
from comfyui_mcp_skills.adapters.mcp.server import create_server


class FakeGateway:
    def __init__(self) -> None:
        self.queued: list[dict[str, Any]] = []
        self.histories: dict[str, dict[str, Any]] = {}
        self.interrupted: list[str] = []
        self.uploaded: list[str] = []

    def queue_prompt(self, workflow: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.queued.append(workflow)
        return {"prompt_id": "prompt-mcp", "client_id": "client-mcp"}

    def get_history(self, prompt_id: str) -> dict[str, Any] | None:
        return self.histories.get(prompt_id)

    def get_queue(self) -> dict[str, Any]:
        return {"queue_running": [], "queue_pending": []}

    def interrupt(self, prompt_id: str = "") -> dict[str, Any]:
        self.interrupted.append(prompt_id)
        return {"success": True}

    def queue_delete(self, prompt_ids: list[str]) -> dict[str, Any]:
        return {"success": True}

    def upload_file(
        self, path: str, *, purpose: str, original_ref: str
    ) -> dict[str, Any]:
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


def _project(tmp_path: Path) -> None:
    directory = tmp_path / "data" / "local" / "txt2img"
    directory.mkdir(parents=True)
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "default_server": "local",
                "servers": [
                    {"id": "local", "name": "Local", "url": "http://127.0.0.1:8188"}
                ],
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
) -> None:
    directory = base_dir / "data" / "local" / workflow_id
    directory.mkdir(parents=True)
    (directory / "schema.json").write_text(
        json.dumps(
            {
                "description": f"Workflow {workflow_id}",
                "enabled": True,
                "parameters": parameters or {},
            }
        ),
        encoding="utf-8",
    )
    (directory / "workflow.json").write_text(
        json.dumps({"1": {"class_type": "Test", "inputs": {}}}),
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
                "2": {
                    "images": [
                        {"filename": "out.png", "subfolder": "", "type": "output"}
                    ]
                }
            },
        }
        completed = await client.call_tool(
            "comfyui.job.get",
            {"server_id": "local", "prompt_id": "prompt-mcp"},
        )
        assert completed.structured_content["status"] == "completed"
        job_uri = "comfyui://jobs/local/prompt-mcp"
        output_uri = completed.structured_content["outputs"][0]["resource_uri"]
        assert json.loads((await client.read_resource(asset_uri)).contents[0].text)["name"]
        assert json.loads((await client.read_resource(job_uri)).contents[0].text)["status"] == "completed"
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
        }
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
            },
        )
        assert deleted.structured_content["deleted"] is True
    assert not (tmp_path / "data" / "local" / "txt2img").exists()

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
async def test_bad_schema_is_isolated_and_legacy_ui_parameters_are_exposed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
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
        name.endswith((".bad", ".bad-target", ".reserved")) for name in names
    )
    legacy_tool = next(tool for tool in listed.tools if tool.name.endswith(".legacy"))
    assert "Skipping workflow local/bad" in caplog.text
    assert legacy_tool.input_schema["required"] == ["prompt"]
