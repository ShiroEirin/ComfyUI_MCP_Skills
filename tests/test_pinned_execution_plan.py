"""execution.plan accepts an explicitly pinned (possibly unpublished) Revision."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from comfyui_mcp_skills.adapters.mcp.server import create_server
from comfyui_mcp_skills.application.authorization import AuthorizationContext, Scope, Toolset
from comfyui_mcp_skills.application.workflow_change import WorkflowChangeService
from comfyui_mcp_skills.application.workflow_graph import (
    WorkflowGraphService,
    WorkflowValidationService,
)
from comfyui_mcp_skills.application.workflow_import import WorkflowImportService
from comfyui_mcp_skills.domain.workflow_semantics import (
    DependencyExtractorRegistry,
    ParameterRoleRegistry,
)
from comfyui_mcp_skills.infrastructure.persistence.assets import FileAssetRepository
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.repository_factory import RepositoryBundle
from comfyui_mcp_skills.infrastructure.persistence.sqlite_diagnostics import (
    SQLiteDiagnosticRetryRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_runs import SQLiteRunRepository
from comfyui_mcp_skills.infrastructure.persistence.sqlite_workflows import SQLiteWorkflowRepository
from comfyui_mcp_skills.infrastructure.persistence.workflow_changes import (
    SQLiteWorkflowChangeRepository,
)

OBJECT_INFO: dict[str, Any] = {
    "Text": {
        "input": {"required": {"text": ["STRING"]}},
        "input_order": {"required": ["text"]},
        "output": ["STRING"],
    },
    "Image": {"input": {"required": {}}, "output": ["IMAGE"]},
    "SaveImage": {
        "input": {
            "required": {
                "images": ["IMAGE"],
                "filename_prefix": ["STRING"],
            }
        },
        "input_order": {"required": ["images", "filename_prefix"]},
        "output": [],
        "output_node": True,
    },
}

GRAPH: dict[str, Any] = {
    "1": {"class_type": "Text", "inputs": {"text": "before"}},
    "2": {"class_type": "Image", "inputs": {}},
    "3": {
        "class_type": "SaveImage",
        "inputs": {"images": ["2", 0], "filename_prefix": "result"},
    },
}


class _Gateway:
    def __init__(self) -> None:
        self.histories: dict[str, dict[str, Any]] = {}
        self.submissions = 0

    def get_object_info(self) -> dict[str, Any]:
        return OBJECT_INFO

    def get_models(self, folder: str) -> list[str]:
        return []

    def get_queue(self, timeout_seconds: float | None = None) -> dict[str, Any]:
        return {"queue_running": [], "queue_pending": []}

    def get_system_stats(self) -> dict[str, Any]:
        return {"system": {"os": "test"}, "devices": [{"name": "GPU"}]}

    def get_history(
        self, prompt_id: str, timeout_seconds: float | None = None
    ) -> dict[str, Any] | None:
        return self.histories.get(prompt_id)

    def queue_prompt(self, workflow: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.submissions += 1
        prompt_id = f"prompt-pinned-{self.submissions}"
        return {"prompt_id": prompt_id, "client_id": "client-pinned"}


def _setup(
    tmp_path: Path, gateway: _Gateway | None = None
) -> tuple[SQLiteControlPlaneStore, SQLiteWorkflowRepository, Any]:
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "default_server": "local",
                "servers": [{"id": "local", "url": "http://127.0.0.1:8188"}],
            }
        ),
        encoding="utf-8",
    )
    store = SQLiteControlPlaneStore((tmp_path / "data" / "control-plane.sqlite3").resolve())
    store.initialize()
    workflows = SQLiteWorkflowRepository(store)
    graphs = WorkflowGraphService(
        ParameterRoleRegistry.default(), DependencyExtractorRegistry.default()
    )
    validation = WorkflowValidationService()
    importer = WorkflowImportService(graphs, validation, workflows)
    for workflow_id in ("portrait", "other"):
        created = importer.commit(
            importer.preview(
                GRAPH,
                workflow_id=workflow_id,
                server_id="local",
                object_info=OBJECT_INFO,
            )
        )
        workflows.publish(created["deployment_id"])
    repositories = RepositoryBundle(
        workflows=workflows,
        runs=SQLiteRunRepository(store),
        assets=FileAssetRepository(tmp_path),
        workflow_store="sqlite",
        run_store="sqlite",
        asset_store="file",
        store=store,
        retries=SQLiteDiagnosticRetryRepository(store),
    )
    server = create_server(
        tmp_path,
        repositories=repositories,
        gateway_factory=lambda _config: gateway or _Gateway(),
    )
    return store, workflows, server


def _unpublished_revision(
    store: SQLiteControlPlaneStore,
    *,
    workflow_id: str = "portrait",
    value: str = "pinned",
) -> dict[str, Any]:
    graphs = WorkflowGraphService(
        ParameterRoleRegistry.default(), DependencyExtractorRegistry.default()
    )
    changes = WorkflowChangeService(
        SQLiteWorkflowChangeRepository(store), graphs, WorkflowValidationService(), actor="pin-test"
    )
    plan = changes.plan(
        workflow_id,
        "local",
        [{"op": "set_input", "node_id": "1", "field": "text", "value": value}],
        object_info=OBJECT_INFO,
    )
    return changes.commit(plan["plan_id"], plan["plan_digest"])


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_plan_uses_a_pinned_unpublished_revision(tmp_path: Path) -> None:
    store, workflows, server = _setup(tmp_path)
    operations_server = create_server(
        tmp_path,
        repositories=RepositoryBundle(
            workflows=workflows,
            runs=SQLiteRunRepository(store),
            assets=FileAssetRepository(tmp_path),
            workflow_store="sqlite",
            run_store="sqlite",
            asset_store="file",
            store=store,
        ),
        gateway_factory=lambda _config: _Gateway(),
        authorization=AuthorizationContext(
            "local-stdio",
            frozenset({Scope.OBSERVE, Scope.OPERATE}),
            Toolset.OPERATIONS,
        ),
    )
    pinned = _unpublished_revision(store)
    published = workflows.describe("portrait", "local")
    assert pinned["published"] is False
    assert pinned["revision_id"] != published["revision_id"]

    async with Client(server) as client:
        planned = await client.call_tool(
            "comfyui.execution.plan",
            {
                "workflow_id": "portrait",
                "arguments": {"prompt": "a blue cat"},
                "server_id": "local",
                "revision_id": pinned["revision_id"],
                "deployment_id": pinned["deployment_id"],
                "content_digest": pinned["content_digest"],
            },
        )
        plan = planned.structured_content
        committed = await client.call_tool(
            "comfyui.execution.commit",
            {
                "plan_id": plan["plan_id"],
                "plan_digest": plan["plan_digest"],
                "idempotency_key": "pinned-trial-1",
            },
        )

    async with Client(operations_server) as client:
        explained = await client.call_tool("comfyui.route.explain", {"plan_id": plan["plan_id"]})

    assert plan["revision_id"] == pinned["revision_id"]
    assert plan["deployment_id"] == pinned["deployment_id"]
    assert plan["content_digest"] == pinned["content_digest"]
    assert plan["selected_server_id"] == "local"
    assert plan["status"] == "planned"
    assert len(plan["candidates"]) == 1
    assert plan["candidates"][0]["revision_id"] == pinned["revision_id"]
    assert plan["candidates"][0]["eligible"] is True
    assert explained.structured_content["plan_digest"] == plan["plan_digest"]
    # The pinned Revision is a real trial run: the plan commits to a Job.
    assert committed.structured_content["status"] == "committed"
    assert committed.structured_content["job_id"].startswith("job_")
    assert committed.structured_content["revision_id"] == pinned["revision_id"]


@pytest.mark.anyio
async def test_pinned_plan_defaults_to_the_revision_deployment(tmp_path: Path) -> None:
    store, _workflows, server = _setup(tmp_path)
    pinned = _unpublished_revision(store, value="pinned-default")

    async with Client(server) as client:
        planned = await client.call_tool(
            "comfyui.execution.plan",
            {
                "workflow_id": "portrait",
                "arguments": {"prompt": "a blue cat"},
                "server_id": "local",
                "revision_id": pinned["revision_id"],
            },
        )

    plan = planned.structured_content
    assert plan["deployment_id"] == pinned["deployment_id"]
    assert plan["content_digest"] == pinned["content_digest"]


@pytest.mark.anyio
async def test_unknown_pinned_revision_is_rejected(tmp_path: Path) -> None:
    _store_handle, _workflows, server = _setup(tmp_path)
    missing = "revision_" + "0" * 64

    async with Client(server) as client:
        rejected = await client.call_tool(
            "comfyui.execution.plan",
            {
                "workflow_id": "portrait",
                "arguments": {"prompt": "a blue cat"},
                "server_id": "local",
                "revision_id": missing,
            },
        )

    payload = json.loads(rejected.content[0].text)
    assert payload["code"] == "WORKFLOW_CHANGE_NOT_FOUND"
    assert missing in payload["message"]
    assert payload["details"]["revision_id"] == missing


@pytest.mark.anyio
async def test_pinned_revision_of_another_workflow_is_rejected(tmp_path: Path) -> None:
    store, _workflows, server = _setup(tmp_path)
    foreign = _unpublished_revision(store, workflow_id="other", value="foreign")

    async with Client(server) as client:
        rejected = await client.call_tool(
            "comfyui.execution.plan",
            {
                "workflow_id": "portrait",
                "arguments": {"prompt": "a blue cat"},
                "server_id": "local",
                "revision_id": foreign["revision_id"],
            },
        )

    payload = json.loads(rejected.content[0].text)
    assert payload["code"] == "WORKFLOW_CHANGE_NOT_FOUND"
    assert payload["details"]["revision_workflow_id"] == "other"


@pytest.mark.anyio
async def test_mismatched_pin_digest_is_rejected(tmp_path: Path) -> None:
    store, _workflows, server = _setup(tmp_path)
    pinned = _unpublished_revision(store, value="digest-check")

    async with Client(server) as client:
        rejected = await client.call_tool(
            "comfyui.execution.plan",
            {
                "workflow_id": "portrait",
                "arguments": {"prompt": "a blue cat"},
                "server_id": "local",
                "revision_id": pinned["revision_id"],
                "content_digest": "f" * 64,
            },
        )

    payload = json.loads(rejected.content[0].text)
    assert payload["code"] == "WORKFLOW_CHANGE_CONFLICT"
    assert payload["details"]["content_digest"] == "f" * 64


@pytest.mark.anyio
async def test_pinned_revision_requires_a_server(tmp_path: Path) -> None:
    store, _workflows, server = _setup(tmp_path)
    pinned = _unpublished_revision(store, value="no-server")

    async with Client(server) as client:
        rejected = await client.call_tool(
            "comfyui.execution.plan",
            {
                "workflow_id": "portrait",
                "arguments": {"prompt": "a blue cat"},
                "revision_id": pinned["revision_id"],
            },
        )

    payload = json.loads(rejected.content[0].text)
    assert payload["code"] == "WORKFLOW_CHANGE_CONFLICT"
    assert payload["details"]["field"] == "server_id"


@pytest.mark.anyio
async def test_deployment_pin_without_revision_is_rejected(tmp_path: Path) -> None:
    _store_handle, _workflows, server = _setup(tmp_path)

    async with Client(server) as client:
        rejected = await client.call_tool(
            "comfyui.execution.plan",
            {
                "workflow_id": "portrait",
                "arguments": {"prompt": "a blue cat"},
                "server_id": "local",
                "deployment_id": "deployment_" + "1" * 64,
            },
        )

    payload = json.loads(rejected.content[0].text)
    assert payload["code"] == "INVALID_ARGUMENTS"
    assert "revision_id" in payload["details"]["reason"]


@pytest.mark.anyio
async def test_plan_without_pin_keeps_the_published_revision(tmp_path: Path) -> None:
    store, workflows, server = _setup(tmp_path)
    _unpublished_revision(store, value="ignored")
    published = workflows.describe("portrait", "local")

    async with Client(server) as client:
        planned = await client.call_tool(
            "comfyui.execution.plan",
            {"workflow_id": "portrait", "arguments": {"prompt": "a blue cat"}},
        )

    plan = planned.structured_content
    assert plan["revision_id"] == published["revision_id"]
    assert plan["deployment_id"] == published["deployment_id"]


@pytest.mark.anyio
async def test_generic_run_uses_a_pinned_revision(tmp_path: Path) -> None:
    store, _workflows, server = _setup(tmp_path)
    pinned = _unpublished_revision(store, value="generic-run-pin")

    async with Client(server) as client:
        result = await client.call_tool(
            "comfyui.workflow.run",
            {
                "server_id": "local",
                "workflow_id": "portrait",
                "arguments": {"prompt": "a blue cat"},
                "revision_id": pinned["revision_id"],
                "deployment_id": pinned["deployment_id"],
                "content_digest": pinned["content_digest"],
            },
        )

    assert result.is_error is False
    assert result.structured_content["prompt_id"] == "prompt-pinned-1"
    assert result.structured_content["revision_id"] == pinned["revision_id"]
    assert result.structured_content["deployment_id"] == pinned["deployment_id"]


@pytest.mark.anyio
async def test_successful_job_regenerates_through_real_sqlite_and_mcp(tmp_path: Path) -> None:
    gateway = _Gateway()
    _store, _workflows, server = _setup(tmp_path, gateway)
    gateway.histories["prompt-pinned-1"] = {
        "status": {"completed": True, "status_str": "success"},
        "outputs": {
            "3": {"images": [{"filename": "out.png", "subfolder": "", "type": "output"}]}
        },
    }

    async with Client(server) as client:
        original = await client.call_tool(
            "comfyui.workflow.run",
            {
                "server_id": "local",
                "workflow_id": "portrait",
                "arguments": {"prompt": "original"},
                "_overrides": {"1.text": "recorded override"},
                "_execution": {"idempotency_key": "successful-original"},
            },
        )
        fetched = await client.call_tool(
            "comfyui.job.get",
            {"server_id": "local", "prompt_id": "prompt-pinned-1"},
        )
        planned = await client.call_tool(
            "comfyui.job.regenerate",
            {
                "job_id": original.structured_content["job_id"],
                "changes": {"prompt": "regenerated"},
            },
        )
        plan = planned.structured_content
        committed = await client.call_tool(
            "comfyui.job.retry.commit",
            {"repair_plan_id": plan["repair_plan_id"], "plan_digest": plan["plan_digest"]},
        )
        repeated = await client.call_tool(
            "comfyui.job.retry.commit",
            {"repair_plan_id": plan["repair_plan_id"], "plan_digest": plan["plan_digest"]},
        )

    assert original.is_error is False
    assert fetched.structured_content["status"] == "completed"
    assert planned.is_error is False, planned.content
    assert plan["operation"] == "regenerate"
    assert plan["original_arguments"] == {"prompt": "original"}
    assert plan["resulting_arguments"] == {
        "prompt": "regenerated",
        "_overrides": {"1.text": "recorded override"},
    }
    assert plan["overrides"] == {"1.text": "recorded override"}
    assert committed.is_error is False, committed.content
    assert committed.structured_content["status"] == "committed"
    assert committed.structured_content["retry_of"] == original.structured_content["job_id"]
    assert committed.structured_content["result_job_id"] != original.structured_content["job_id"]
    assert repeated.structured_content == committed.structured_content
    assert gateway.submissions == 2
