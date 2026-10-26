"""End-to-end contracts for one-run overrides and risky nodes on the execute surface."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from comfyui_mcp_skills.application.catalog import WorkflowCatalog
from comfyui_mcp_skills.application.execution import (
    ExecutionService,
    RiskyWorkflowNodesError,
)
from comfyui_mcp_skills.application.planning import ExecutionPlanningService
from comfyui_mcp_skills.application.retries import RetryService
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import WorkflowArgumentsError
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.sqlite_assets import SQLiteAssetRepository
from comfyui_mcp_skills.infrastructure.persistence.sqlite_runs import SQLiteRunRepository
from comfyui_mcp_skills.infrastructure.persistence.sqlite_workflows import (
    SQLiteWorkflowRepository,
    _revision_digest,
)

_NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


class _RecordingGateway:
    def __init__(self) -> None:
        self.queued: list[dict[str, Any]] = []

    def queue_prompt(self, workflow: dict[str, Any], **kwargs: Any) -> dict[str, str]:
        del kwargs
        self.queued.append(json.loads(json.dumps(workflow)))
        return {"prompt_id": f"prompt-{len(self.queued)}"}


class _MemoryRetryRepository:
    """Minimal RetryRepository double that mirrors the SQLite snapshot semantics."""

    def __init__(self, context: dict[str, Any]) -> None:
        self.context = context
        self.plans: dict[str, dict[str, Any]] = {}
        self.reserved: set[str] = set()

    def get_retry_context(self, job_id: str, owner_id: str) -> dict[str, Any] | None:
        if owner_id != self.context["owner_id"]:
            return None
        return copy.deepcopy(self.context)

    def save_repair_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        self.plans.setdefault(plan["repair_plan_id"], copy.deepcopy(plan))
        return copy.deepcopy(self.plans[plan["repair_plan_id"]])

    def get_repair_plan(self, repair_plan_id: str, owner_id: str) -> dict[str, Any] | None:
        plan = self.plans.get(repair_plan_id)
        if plan is None or plan["owner_id"] != owner_id:
            return None
        return copy.deepcopy(plan)

    def reserve_repair_plan_commit(
        self,
        repair_plan_id: str,
        plan_digest: str,
        owner_id: str,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        plan = self.plans[repair_plan_id]
        if plan["plan_digest"] != plan_digest or plan["owner_id"] != owner_id:
            raise ValueError("plan conflict")
        if repair_plan_id not in self.reserved:
            if datetime.fromisoformat(plan["expires_at"]) <= now:
                raise ValueError("plan expired")
            self.reserved.add(repair_plan_id)
        return copy.deepcopy(plan)

    def mark_repair_plan_committed(
        self,
        repair_plan_id: str,
        plan_digest: str,
        owner_id: str,
        retry_job_id: str,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        plan = self.plans[repair_plan_id]
        if plan["plan_digest"] != plan_digest or plan["owner_id"] != owner_id:
            raise ValueError("plan conflict")
        plan.update(
            status="committed",
            result_job_id=retry_job_id,
            result_job_uri=f"comfyui://jobs/{retry_job_id}",
            retry_of=plan["original_job_id"],
            committed_at=now.isoformat(),
        )
        return copy.deepcopy(plan)


def _service(
    tmp_path: Path,
    gateway: object,
    audit: Any = None,
) -> tuple[ExecutionService, SQLiteWorkflowRepository]:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    store = SQLiteControlPlaneStore(tmp_path / "data" / "control-plane.sqlite3")
    store.initialize()
    (tmp_path / "config.json").write_text(
        json.dumps(
            {"servers": [{"id": "local", "name": "Local", "url": "http://127.0.0.1:8188"}]}
        ),
        encoding="utf-8",
    )
    workflows = SQLiteWorkflowRepository(store)
    service = ExecutionService(
        WorkflowCatalog(workflows),
        ServerRegistry(tmp_path),
        SQLiteRunRepository(store),
        SQLiteAssetRepository(store),
        lambda _config: gateway,  # type: ignore[arg-type,return-value]
        planning=ExecutionPlanningService(store, workflows),
        risky_node_audit=audit,
    )
    return service, workflows


def _publish(
    workflows: SQLiteWorkflowRepository,
    workflow_id: str,
    graph: dict[str, Any],
    parameters: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    schema: dict[str, Any] = {
        "description": workflow_id,
        "enabled": True,
        "parameters": parameters,
    }
    digest = _revision_digest(graph, schema, {})
    created = workflows.create_revision(
        workflow_id=workflow_id,
        server_id="local",
        graph=graph,
        parameter_schema=schema,
        dependency_contract={},
        content_digest=digest,
    )
    workflows.publish(str(created["deployment_id"]))
    return created, digest


def test_retry_replays_persisted_overrides_into_the_same_graph(tmp_path: Path) -> None:
    gateway = _RecordingGateway()
    service, workflows = _service(tmp_path, gateway)
    graph = {"7": {"class_type": "KSampler", "inputs": {"steps": 20, "seed": 1}}}
    created, digest = _publish(
        workflows, "flow", graph, {"steps": {"node_id": "7", "field": "steps"}}
    )

    original = service.submit("local", "flow", {}, owner_id="owner-a", overrides={"7.steps": 33})

    repository = _MemoryRetryRepository(
        {
            "job_id": original.job_id,
            "owner_id": "owner-a",
            "workflow_id": "flow",
            "server_id": "local",
            "status": "error",
            "plan_id": original.plan_id,
            "revision_id": str(created["revision_id"]),
            "deployment_id": str(created["deployment_id"]),
            "content_digest": digest,
            "raw_arguments": {},
            "legacy_migrated": False,
        }
    )
    retries = RetryService(repository, service, clock=lambda: _NOW)

    plan = retries.plan(original.job_id, "owner-a", {"_overrides": {"7.steps": 33}})

    assert repository.plans[plan["repair_plan_id"]]["normalized_changes"] == {
        "_overrides": {"7.steps": 33}
    }

    committed = retries.commit(plan["repair_plan_id"], plan["plan_digest"], "owner-a")

    assert committed["result_job_id"]
    assert gateway.queued[0]["7"]["inputs"]["steps"] == 33
    # The retry replays exactly the graph the original overridden submit built.
    assert gateway.queued[1] == gateway.queued[0]


def test_media_override_is_rejected_before_any_plan_or_submission(tmp_path: Path) -> None:
    gateway = _RecordingGateway()
    service, workflows = _service(tmp_path, gateway)
    graph = {"5": {"class_type": "LoadImage", "inputs": {"image": "input/base.png"}}}
    _publish(
        workflows,
        "media",
        graph,
        {"image": {"node_id": "5", "field": "image", "type": "image"}},
    )

    with pytest.raises(WorkflowArgumentsError) as excinfo:
        service.submit(
            "local",
            "media",
            {},
            owner_id="owner-a",
            overrides={"5.image": "asset_" + "a" * 32},
        )

    message = str(excinfo.value)
    assert "5.image" in message
    assert "comfyui.asset.upload" in message
    assert excinfo.value.code == "INVALID_WORKFLOW_ARGUMENTS"
    assert gateway.queued == []


def test_risky_nodes_are_blocked_unless_explicitly_allowed_and_audited(tmp_path: Path) -> None:
    gateway = _RecordingGateway()
    events: list[dict[str, Any]] = []
    service, workflows = _service(tmp_path, gateway, audit=events.append)
    graph = {
        "1": {"class_type": "PythonScript", "inputs": {"code": "print(1)"}},
        "2": {"class_type": "HttpRequest", "inputs": {"url": "https://example.invalid"}},
        "3": {"class_type": "KSampler", "inputs": {"steps": 20}},
    }
    _publish(workflows, "risky", graph, {})

    with pytest.raises(RiskyWorkflowNodesError) as excinfo:
        service.submit("local", "risky", {}, owner_id="owner-a")

    findings = excinfo.value.details["risky_nodes"]
    assert [(item["node_id"], item["category"]) for item in findings] == [
        ("1", "code_execution"),
        ("2", "network_request"),
    ]
    assert excinfo.value.code == "RISKY_WORKFLOW_NODES"
    assert excinfo.value.details["allow_risky_nodes"] is True
    assert gateway.queued == []

    job = service.submit("local", "risky", {}, owner_id="owner-a", allow_risky_nodes=True)

    assert job.prompt_id == "prompt-1"
    assert len(gateway.queued) == 1
    assert events and events[0]["event"] == "risky_nodes_allowed"
    assert events[0]["owner_id"] == "owner-a"
    assert [item["node_id"] for item in events[0]["risky_nodes"]] == ["1", "2"]
