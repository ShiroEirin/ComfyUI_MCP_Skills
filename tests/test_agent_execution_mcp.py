"""Agent-native execution MCP integration contracts."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError

from comfyui_mcp_skills.adapters.mcp.server import create_server
from comfyui_mcp_skills.application.authorization import AuthorizationContext, Scope, Toolset
from comfyui_mcp_skills.application.capabilities import ToolInventory
from comfyui_mcp_skills.application.job_batch import BatchService
from comfyui_mcp_skills.application.jobs import JobWaitTimeout
from comfyui_mcp_skills.domain.control_plane import derive_legacy_artifact_id, derive_legacy_job_id
from comfyui_mcp_skills.domain.errors import RepairPlanNotFound
from comfyui_mcp_skills.domain.models import Job
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.repository_factory import RepositoryBundle
from comfyui_mcp_skills.infrastructure.persistence.sqlite_asset_library import (
    SQLiteAssetLibraryRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_runs import SQLiteRunRepository
from comfyui_mcp_skills.infrastructure.persistence.workflows import FileWorkflowRepository

_JOB_ID = "job_" + "a" * 32
_PLAN_ID = "repair_plan_" + "b" * 64
_REVISION_ID = "revision_" + "c" * 32
_DEPLOYMENT_ID = "deployment_" + "d" * 32


def _project(root: Path, *, workflow_count: int = 1) -> None:
    (root / "config.json").write_text(
        json.dumps(
            {
                "default_server": "local",
                "servers": [{"id": "local", "url": "http://127.0.0.1:8188"}],
            }
        ),
        encoding="utf-8",
    )
    for index in range(workflow_count):
        directory = root / "data" / "local" / f"workflow-{index:02d}"
        directory.mkdir(parents=True)
        (directory / "schema.json").write_text(
            json.dumps(
                {
                    "description": "Agent execution fixture",
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
            json.dumps({"1": {"class_type": "Text", "inputs": {"text": ""}}}),
            encoding="utf-8",
        )


class _Gateway:
    def __init__(self) -> None:
        self.queued: list[dict[str, Any]] = []
        self.queue_prompt_args: list[dict[str, Any]] = []
        self.histories: dict[str, dict[str, Any]] = {}
        self.deleted: list[list[str]] = []

    def queue_prompt(self, workflow: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.queued.append(workflow)
        self.queue_prompt_args.append(kwargs)
        return {"prompt_id": f"prompt-{len(self.queued)}", "client_id": "agent-test"}

    def get_history(
        self, prompt_id: str, timeout_seconds: float | None = None
    ) -> dict[str, Any] | None:
        return self.histories.get(prompt_id)

    def get_queue(self, timeout_seconds: float | None = None) -> dict[str, Any]:
        return {"queue_running": [], "queue_pending": []}

    def ws_events(
        self,
        client_id: str,
        prompt_id: str,
        timeout_seconds: float,
        cancel_check: Any = None,
    ) -> tuple[dict[str, Any], ...]:
        return ()

    def queue_delete(self, prompt_ids: list[str]) -> dict[str, Any]:
        self.deleted.append(prompt_ids)
        return {"success": True}

    def download_output(
        self,
        filename: str,
        subfolder: str = "",
        output_type: str = "output",
        *,
        max_bytes: int,
    ) -> bytes:
        return b"\x89PNG\r\n\x1a\nagent-result"


class _RegenerationService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def plan(
        self,
        source_job_id: str,
        owner_id: str,
        changes: dict[str, Any] | None = None,
        *,
        _overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((source_job_id, owner_id, changes, _overrides))
        if owner_id != "owner-a":
            raise RepairPlanNotFound("source Job was not found")
        original = {"seed": 7, "steps": 20}
        resulting = {**original, **(changes or {})}
        if _overrides is not None:
            resulting["_overrides"] = _overrides
        return {
            "repair_plan_id": _PLAN_ID,
            "plan_digest": "e" * 64,
            "resource_uri": f"comfyui://plans/{_PLAN_ID}",
            "original_job_id": source_job_id,
            "workflow_id": "portrait",
            "server_id": "local",
            "pinned_plan_id": "plan_" + "1" * 32,
            "pinned_revision_id": _REVISION_ID,
            "pinned_deployment_id": _DEPLOYMENT_ID,
            "pinned_content_digest": "4" * 64,
            "normalized_changes": {
                **(changes or {}),
                **({"_overrides": _overrides} if _overrides else {}),
            },
            "diff": [{"path": "/arguments/steps", "before": 20, "after": resulting["steps"]}],
            "original_arguments_digest": "2" * 64,
            "resulting_arguments_digest": "3" * 64,
            "status": "planned",
            "created_at": "2026-10-31T00:00:00+00:00",
            "expires_at": "2026-10-31T00:10:00+00:00",
            "operation": "regenerate",
            "source_job_id": source_job_id,
            "revision_id": _REVISION_ID,
            "deployment_id": _DEPLOYMENT_ID,
            "content_digest": "4" * 64,
            "original_arguments": original,
            "resulting_arguments": resulting,
            "overrides": _overrides or {},
        }


class _BatchExecution:
    def submit(
        self, server_id: str, workflow_id: str, arguments: dict[str, Any], **options: Any
    ) -> Job:
        if arguments.get("fail"):
            raise ValueError("isolated submission failure")
        prompt_id = f"prompt-{workflow_id}"
        return Job(
            prompt_id=prompt_id,
            server_id=server_id,
            workflow_id=workflow_id,
            status="submitted",
            owner_id=str(options["owner_id"]),
            job_id=derive_legacy_job_id(server_id, prompt_id),
        )


class _BatchJobs:
    def wait(
        self, server_id: str, prompt_id: str, *, timeout_seconds: float, owner_id: str = ""
    ) -> Job:
        job = Job(
            prompt_id=prompt_id,
            server_id=server_id,
            workflow_id=prompt_id.removeprefix("prompt-"),
            status="running" if prompt_id.endswith("timeout") else "completed",
            owner_id=owner_id,
            job_id=derive_legacy_job_id(server_id, prompt_id),
        )
        if job.status == "running":
            raise JobWaitTimeout(prompt_id, timeout_seconds, job)
        return job

    def cancel(self, server_id: str, prompt_id: str, *, owner_id: str = "") -> Job:
        return Job(
            prompt_id=prompt_id,
            server_id=server_id,
            workflow_id=prompt_id.removeprefix("prompt-"),
            status="cancelled",
            owner_id=owner_id,
            job_id=derive_legacy_job_id(server_id, prompt_id),
        )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_generic_run_reaches_workflows_beyond_the_dynamic_budget(tmp_path: Path) -> None:
    _project(tmp_path, workflow_count=ToolInventory.DYNAMIC_LIMIT + 1)
    gateway = _Gateway()
    server = create_server(tmp_path, gateway_factory=lambda _config: gateway)

    async with Client(server) as client:
        dynamic = [
            tool.name
            for tool in (await client.list_tools()).tools
            if tool.name.startswith("comfyui.run.")
        ]
        assert len(dynamic) == ToolInventory.DYNAMIC_LIMIT
        assert not any(name.endswith("workflow-08") for name in dynamic)
        result = await client.call_tool(
            "comfyui.workflow.run",
            {
                "server_id": "local",
                "workflow_id": "workflow-08",
                "arguments": {"prompt": "generic fallback"},
                "_overrides": {"1.text": "one run only"},
                "_execution": {
                    "idempotency_key": "generic-fallback-1",
                    "priority": -2,
                    "partial_execution_targets": ["1"],
                    "allow_risky_nodes": False,
                },
            },
        )

    assert result.is_error is False
    assert result.structured_content["workflow_id"] == "workflow-08"
    assert gateway.queued[0]["1"]["inputs"]["text"] == "one run only"
    assert gateway.queue_prompt_args[0]["priority"] == -2.0
    assert gateway.queue_prompt_args[0]["targets"] == ["1"]


@pytest.mark.anyio
async def test_generic_run_wait_timeout_is_a_valid_structured_result(tmp_path: Path) -> None:
    _project(tmp_path)
    gateway = _Gateway()
    server = create_server(tmp_path, gateway_factory=lambda _config: gateway)

    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.workflow.run",
            {
                "server_id": "local",
                "workflow_id": "workflow-00",
                "arguments": {"prompt": "still running"},
                "_execution": {"wait": True, "wait_timeout_seconds": 0},
            },
        )

    assert result.is_error is False
    assert result.structured_content["timed_out"] is True
    assert result.structured_content["last_status"] == "submitted"
    assert result.structured_content["prompt_id"] == "prompt-1"


@pytest.mark.anyio
async def test_generic_run_wait_returns_terminal_job(tmp_path: Path) -> None:
    _project(tmp_path)
    gateway = _Gateway()
    gateway.histories["prompt-1"] = {
        "status": {"completed": True, "status_str": "success"},
        "outputs": {},
    }
    server = create_server(tmp_path, gateway_factory=lambda _config: gateway)

    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.workflow.run",
            {
                "server_id": "local",
                "workflow_id": "workflow-00",
                "arguments": {"prompt": "finished"},
                "_execution": {"wait": True, "wait_timeout_seconds": 1},
            },
        )

    assert result.is_error is False
    assert result.structured_content["status"] == "completed"
    assert "timed_out" not in result.structured_content


@pytest.mark.anyio
async def test_regenerate_mcp_forwards_lineage_changes_and_strict_output(tmp_path: Path) -> None:
    _project(tmp_path)
    regeneration = _RegenerationService()
    server = create_server(
        tmp_path,
        gateway_factory=lambda _config: _Gateway(),
        regeneration_service=regeneration,
        authorization=AuthorizationContext(
            "owner-a", frozenset({Scope.EXECUTE}), Toolset.EXECUTION
        ),
    )

    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.job.regenerate",
            {
                "job_id": _JOB_ID,
                "changes": {"steps": 30},
                "_overrides": {"7.seed": 99},
            },
        )
        foreign_server = create_server(
            tmp_path,
            gateway_factory=lambda _config: _Gateway(),
            regeneration_service=regeneration,
            authorization=AuthorizationContext(
                "owner-b", frozenset({Scope.EXECUTE}), Toolset.EXECUTION
            ),
        )

    async with Client(foreign_server) as client:
        foreign = await client.call_tool("comfyui.job.regenerate", {"job_id": _JOB_ID})

    assert result.is_error is False
    assert result.structured_content["operation"] == "regenerate"
    assert result.structured_content["original_arguments"] == {"seed": 7, "steps": 20}
    assert result.structured_content["resulting_arguments"]["steps"] == 30
    assert result.structured_content["overrides"] == {"7.seed": 99}
    assert regeneration.calls == [
        (_JOB_ID, "owner-a", {"steps": 30}, {"7.seed": 99}),
        (_JOB_ID, "owner-b", {}, None),
    ]
    assert foreign.is_error is True


@pytest.mark.anyio
async def test_batch_mcp_submit_wait_timeout_and_cancel_are_isolated(tmp_path: Path) -> None:
    _project(tmp_path)
    batch = BatchService(_BatchExecution(), _BatchJobs())
    server = create_server(
        tmp_path,
        gateway_factory=lambda _config: _Gateway(),
        batch_service=batch,
        authorization=AuthorizationContext(
            "owner-a", frozenset({Scope.EXECUTE}), Toolset.EXECUTION
        ),
    )

    async with Client(server) as client:
        submitted = await client.call_tool(
            "comfyui.job.batch",
            {
                "action": "submit",
                "jobs": [
                    {"server_id": "local", "workflow_id": "one", "arguments": {}},
                    {"server_id": "local", "workflow_id": "bad", "arguments": {"fail": True}},
                    {"server_id": "local", "workflow_id": "timeout", "arguments": {}},
                ],
            },
        )
        batch_id = submitted.structured_content["batch_id"]
        waited = await client.call_tool(
            "comfyui.job.batch", {"action": "wait", "batch_id": batch_id, "timeout_seconds": 0}
        )
        cancelled = await client.call_tool(
            "comfyui.job.batch", {"action": "cancel", "batch_id": batch_id}
        )

    assert submitted.structured_content["counts"] == {
        "completed": 0,
        "failed": 1,
        "timed_out": 0,
    }
    assert [item["status"] for item in waited.structured_content["items"]] == [
        "completed",
        "failed",
        "timed_out",
    ]
    assert waited.structured_content["counts"]["timed_out"] == 1
    assert [item["status"] for item in cancelled.structured_content["items"]] == [
        "cancelled",
        "failed",
        "cancelled",
    ]


def _completed_job(store: SQLiteControlPlaneStore, *, owner_id: str = "local-stdio") -> Job:
    prompt_id = "prompt-image"
    job_id = derive_legacy_job_id("local", prompt_id)
    artifact_id = derive_legacy_artifact_id(job_id, "9", "images", 0, "out.png", "", "output")
    output = {
        "artifact_id": artifact_id,
        "upstream_node_id": "9",
        "output_key": "images",
        "upstream_output_index": 0,
        "legacy_index": 0,
        "filename": "out.png",
        "subfolder": "",
        "type": "output",
        "storage_type": "output",
        "media_type": "image",
        "mime_type": "image/png",
        "resource_uri": f"comfyui://artifacts/{artifact_id}",
        "legacy_uri": f"comfyui://outputs/local/{prompt_id}/0",
    }
    job = Job(
        prompt_id=prompt_id,
        server_id="local",
        workflow_id="portrait",
        status="completed",
        outputs=(output,),
        owner_id=owner_id,
        job_id=job_id,
    )
    runs = SQLiteRunRepository(store)
    runs.save(replace(job, status="submitted", outputs=()))
    SQLiteAssetLibraryRepository(store).terminalize(job, job.outputs)
    return job


@pytest.mark.anyio
async def test_execution_output_image_verifies_job_artifact_ownership(tmp_path: Path) -> None:
    _project(tmp_path)
    store = SQLiteControlPlaneStore(tmp_path / "data" / "control-plane.sqlite3")
    store.initialize()
    job = _completed_job(store)
    assets = SQLiteAssetLibraryRepository(store)
    repositories = RepositoryBundle(
        workflows=FileWorkflowRepository(tmp_path),
        runs=SQLiteRunRepository(store),
        assets=assets,
        workflow_store="file",
        run_store="sqlite",
        asset_store="sqlite",
        store=store,
    )
    gateway = _Gateway()
    server = create_server(
        tmp_path, repositories=repositories, gateway_factory=lambda _config: gateway
    )

    async with Client(server) as client:
        owned = await client.call_tool(
            "comfyui.output.image",
            {"server_id": "local", "filename": "out.png", "job_id": job.job_id},
        )
        with pytest.raises(MCPError, match="not owned"):
            await client.call_tool(
                "comfyui.output.image",
                {
                    "server_id": "local",
                    "filename": "other.png",
                    "job_id": job.job_id,
                },
            )

    assert owned.is_error is False
    assert owned.structured_content["ownership"]["verified"] is True
    assert base64.b64decode(owned.content[1].data).startswith(b"\x89PNG")
