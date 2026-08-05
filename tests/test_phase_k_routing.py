"""Phase K multi-server routing and immutable execution plan contracts."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from mcp import Client

from comfyui_mcp_skills.adapters.mcp.server import create_server
from comfyui_mcp_skills.application.planning import ExecutionPlanningService
from comfyui_mcp_skills.application.routing import RoutingService
from comfyui_mcp_skills.application.server_control import ServerControlService
from comfyui_mcp_skills.application.servers import OwnerAwareServerRegistry, ServerRegistry
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.g3_migration import (
    build_g3_import_plan,
    cutover_g3_import_plan,
)
from comfyui_mcp_skills.infrastructure.persistence.repository_factory import (
    create_repository_bundle,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_provisioning import (
    SQLiteProvisioningRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_routing import (
    SQLiteRoutingRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_runs import SQLiteRunRepository
from comfyui_mcp_skills.infrastructure.persistence.sqlite_workflows import (
    SQLiteWorkflowRepository,
)
from tests.test_g4_planning import _project
from tests.test_mcp_server import FakeGateway
from tests.test_mcp_server import _project as _mcp_project


class _Repository:
    def __init__(self) -> None:
        self.plans: dict[str, dict[str, Any]] = {}
        self.commit_keys: dict[str, tuple[str, str]] = {}
        self.contexts = [
            {
                "server_id": "busy",
                "revision_id": "revision_" + "1" * 64,
                "deployment_id": "deployment_" + "2" * 64,
                "content_digest": "3" * 64,
                "parameters": {"steps": {"type": "integer", "required": True, "minimum": 1}},
                "queue_depth": 4,
                "execution_slots": 1,
                "available_vram_bytes": 4_000,
                "required_vram_bytes": 2_000,
                "missing_dependencies": [],
                "reuse_mode": "copy",
            },
            {
                "server_id": "idle",
                "revision_id": "revision_" + "4" * 64,
                "deployment_id": "deployment_" + "5" * 64,
                "content_digest": "6" * 64,
                "parameters": {"steps": {"type": "integer", "required": True, "minimum": 1}},
                "queue_depth": 0,
                "execution_slots": 2,
                "available_vram_bytes": 8_000,
                "required_vram_bytes": 2_000,
                "missing_dependencies": [],
                "reuse_mode": "direct",
            },
        ]

    def list_routing_contexts(self, owner_id: str, workflow_id: str) -> list[dict[str, Any]]:
        assert (owner_id, workflow_id) == ("owner-a", "portrait")
        return [dict(item) for item in self.contexts]

    def resolve_server_connection(
        self, owner_id: str, server_id: str, revision: int, config_digest: str
    ) -> dict[str, Any] | None:
        assert owner_id == "owner-a"
        assert server_id in {"busy", "idle"}
        assert (revision, config_digest) == (0, "")
        return None

    def save_routing_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        self.plans[plan["plan_id"]] = dict(plan)
        return dict(plan)

    def get_routing_plan(self, plan_id: str, owner_id: str) -> dict[str, Any] | None:
        plan = self.plans.get(plan_id)
        return dict(plan) if plan is not None and plan["owner_id"] == owner_id else None

    def claim_routing_commit(
        self, plan_id: str, plan_digest: str, owner_id: str, idempotency_digest: str
    ) -> None:
        assert owner_id == "owner-a"
        existing = self.commit_keys.get(idempotency_digest)
        binding = (plan_id, plan_digest)
        if existing is not None and existing != binding:
            raise ValueError("Routing commit idempotency key conflict")
        self.commit_keys[idempotency_digest] = binding

    def mark_routing_plan_committed(
        self, plan_id: str, plan_digest: str, owner_id: str, job_id: str
    ) -> dict[str, Any]:
        plan = self.plans[plan_id]
        assert (plan["plan_digest"], plan["owner_id"]) == (plan_digest, owner_id)
        plan = {**plan, "status": "committed", "job_id": job_id}
        self.plans[plan_id] = plan
        return dict(plan)


class _Executor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def submit(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        return {"job_id": "job_" + "7" * 64, "status": "reserved"}


def test_routing_plan_selects_best_candidate_and_commit_preserves_digest() -> None:
    repository = _Repository()
    executor = _Executor()
    service = RoutingService(repository, executor)

    plan = service.plan(
        "owner-a",
        "portrait",
        {"steps": 20},
        policy={"max_steps": 30, "max_queue_depth": 10},
    )

    assert plan["selected_server_id"] == "idle"
    assert plan["execution_slots"] == 2
    assert plan["reuse_mode"] == "direct"
    assert [item["server_id"] for item in plan["candidates"]] == ["idle", "busy"]
    assert plan["estimate_available"] is False
    committed = service.commit(
        plan["plan_id"], plan["plan_digest"], "owner-a", idempotency_key="route-1"
    )
    assert committed["status"] == "committed"
    assert executor.calls[0]["server_id"] == "idle"
    assert executor.calls[0]["revision_id"] == "revision_" + "4" * 64
    assert executor.calls[0]["idempotency_key"].startswith("routing-")


def test_routing_respects_locked_server_and_reports_policy_violations() -> None:
    service = RoutingService(_Repository(), _Executor())

    locked = service.plan(
        "owner-a",
        "portrait",
        {"steps": 20},
        server_id="busy",
        policy={"max_steps": 30, "max_queue_depth": 10},
    )
    assert locked["selected_server_id"] == "busy"
    assert locked["selection_reason"] == "caller_locked_server"

    with pytest.raises(ValueError, match="Policy rejected execution") as rejected:
        service.plan(
            "owner-a",
            "portrait",
            {"steps": 40},
            policy={"max_steps": 30, "max_queue_depth": 10},
        )
    assert "max_steps" in str(rejected.value)


def test_routing_excludes_incompatible_candidates_and_rejects_digest_change() -> None:
    repository = _Repository()
    repository.contexts[1]["missing_dependencies"] = ["model:missing.safetensors"]
    service = RoutingService(repository, _Executor())
    plan = service.plan(
        "owner-a",
        "portrait",
        {"steps": 20},
        policy={"max_steps": 30, "max_queue_depth": 10},
    )

    assert plan["selected_server_id"] == "busy"
    assert plan["candidates"][1]["exclusion_reasons"] == ["missing_dependencies"]
    with pytest.raises(ValueError, match="digest"):
        service.commit(plan["plan_id"], "0" * 64, "owner-a", idempotency_key="route-1")


def test_routing_request_identity_and_mixed_candidate_schemas() -> None:
    repository = _Repository()
    repository.contexts[1]["parameters"] = {"prompt": {"type": "string", "required": True}}
    service = RoutingService(repository, _Executor())

    first = service.plan("owner-a", "portrait", {"steps": 20}, request_id="request-a")
    repeated = service.plan("owner-a", "portrait", {"steps": 20}, request_id="request-a")
    repository.contexts[0]["queue_depth"] = 99
    stable = service.plan("owner-a", "portrait", {"steps": 20}, request_id="request-a")
    assert stable["plan_id"] == first["plan_id"]
    with pytest.raises(ValueError, match="different inputs"):
        service.plan("owner-a", "portrait", {"steps": 21}, request_id="request-a")
    fresh = service.plan("owner-a", "portrait", {"steps": 20}, request_id="request-b")
    assert first["plan_id"] == repeated["plan_id"]
    assert fresh["plan_id"] != first["plan_id"]
    assert first["selected_server_id"] == "busy"
    assert first["candidates"][1]["exclusion_reasons"] == ["argument_schema_mismatch"]


def test_sqlite_repository_round_trips_and_commits_owner_bound_plan(tmp_path: Any) -> None:
    store = _project(tmp_path)
    repository = SQLiteRoutingRepository(store)
    service = RoutingService(repository, lambda **_kwargs: {"job_id": "job_unused"})

    plan = service.plan("owner-a", "portrait", {})
    repository.claim_routing_commit(plan["plan_id"], plan["plan_digest"], "owner-a", "a" * 64)
    with pytest.raises(ValueError, match="another idempotency key"):
        repository.claim_routing_commit(plan["plan_id"], plan["plan_digest"], "owner-a", "b" * 64)
    second_plan = service.plan("owner-a", "portrait", {}, request_id="second-request")
    with pytest.raises(ValueError, match="idempotency key conflict"):
        repository.claim_routing_commit(
            second_plan["plan_id"], second_plan["plan_digest"], "owner-a", "a" * 64
        )
    assert plan["selected_server_id"] == "local"
    assert repository.get_routing_plan(plan["plan_id"], "other-owner") is None
    assert repository.get_routing_plan(plan["plan_id"], "owner-a")["status"] == "planned"

    planner = ExecutionPlanningService(store, SQLiteWorkflowRepository(store))
    wrong_identity = planner.materialize(
        server_id="local",
        workflow_id="portrait",
        owner_id="owner-b",
        arguments={},
        client_id="route-wrong-owner",
    )
    with pytest.raises(ValueError, match="identity"):
        repository.mark_routing_plan_committed(
            plan["plan_id"], plan["plan_digest"], "owner-a", wrong_identity.job_id
        )
    identity = planner.materialize(
        server_id="local",
        workflow_id="portrait",
        owner_id="owner-a",
        arguments={},
        client_id="route-commit",
    )
    committed = repository.mark_routing_plan_committed(
        plan["plan_id"], plan["plan_digest"], "owner-a", identity.job_id
    )
    assert committed["status"] == "committed"
    assert committed["job_id"] == identity.job_id
    assert (
        repository.mark_routing_plan_committed(
            plan["plan_id"], plan["plan_digest"], "owner-a", identity.job_id
        )["job_id"]
        == identity.job_id
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_owner_server_connection_is_revision_bound(tmp_path: Any) -> None:
    store = SQLiteControlPlaneStore((tmp_path / "control-plane.sqlite3").resolve())
    store.initialize()
    provisioning = SQLiteProvisioningRepository(store)
    controls = ServerControlService(provisioning)
    created = controls.plan(
        "upsert",
        "local",
        "owner-a",
        {"url": "http://127.0.0.1:8188", "expected_revision": 0},
    )
    controls.commit(created["plan_id"], created["plan_digest"], "owner-a")
    current = provisioning.get_server("local", "owner-a")
    assert current is not None
    routing = SQLiteRoutingRepository(store)
    assert routing.current_server_connection("owner-a", "local")["url"].endswith(":8188")

    updated = controls.plan(
        "upsert",
        "local",
        "owner-a",
        {"url": "http://127.0.0.1:8288", "expected_revision": current["revision"]},
    )
    controls.commit(updated["plan_id"], updated["plan_digest"], "owner-a")
    with pytest.raises(ValueError, match="revision conflict"):
        routing.resolve_server_connection(
            "owner-a", "local", current["revision"], current["config_digest"]
        )
    assert routing.current_server_connection("owner-a", "local")["url"].endswith(":8288")


def test_owner_aware_registry_separates_same_server_id(tmp_path: Any) -> None:
    store = SQLiteControlPlaneStore((tmp_path / "control-plane.sqlite3").resolve())
    store.initialize()
    provisioning = SQLiteProvisioningRepository(store)
    controls = ServerControlService(provisioning)
    for owner_id, port in (("owner-a", 8288), ("owner-b", 8388)):
        plan = controls.plan(
            "upsert",
            "local",
            owner_id,
            {"url": f"http://127.0.0.1:{port}", "expected_revision": 0},
        )
        controls.commit(plan["plan_id"], plan["plan_digest"], owner_id)
    (tmp_path / "config.json").write_text(
        '{"servers":[{"id":"local","url":"http://127.0.0.1:9999"}]}',
        encoding="utf-8",
    )
    routing = SQLiteRoutingRepository(store)
    request = {"owner": "owner-a"}
    scoped = OwnerAwareServerRegistry(
        ServerRegistry(tmp_path),
        lambda: request["owner"],
        routing.current_server_connection,
    )
    assert scoped.connection("local")["url"].endswith(":8288")
    request["owner"] = "owner-b"
    assert scoped.connection("local")["url"].endswith(":8388")


@pytest.mark.anyio
async def test_mcp_plan_explain_policy_and_commit_route_to_pinned_job(tmp_path: Any) -> None:
    _mcp_project(tmp_path)
    database = (tmp_path / "data" / "control-plane.sqlite3").resolve()
    store = SQLiteControlPlaneStore(database)
    store.initialize()
    cutover_g3_import_plan(build_g3_import_plan(tmp_path), store)
    repositories = replace(
        create_repository_bundle(tmp_path),
        runs=SQLiteRunRepository(store),
        run_store="sqlite",
    )
    gateway = FakeGateway()
    server = create_server(
        tmp_path,
        gateway_factory=lambda _config: gateway,
        repositories=repositories,
    )

    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert {
            "comfyui.execution.plan",
            "comfyui.execution.commit",
            "comfyui.route.explain",
            "comfyui.policy.evaluate",
        } <= names
        denied = await client.call_tool(
            "comfyui.policy.evaluate",
            {"arguments": {"steps": 31}, "policy": {"max_steps": 30}},
        )
        assert denied.structured_content["allowed"] is False
        planned = await client.call_tool(
            "comfyui.execution.plan",
            {"workflow_id": "txt2img", "arguments": {"prompt": "a blue cat"}},
        )
        plan = planned.structured_content
        assert plan["selected_server_id"] == "local"
        explained = await client.call_tool("comfyui.route.explain", {"plan_id": plan["plan_id"]})
        assert explained.structured_content["plan_digest"] == plan["plan_digest"]
        committed = await client.call_tool(
            "comfyui.execution.commit",
            {
                "plan_id": plan["plan_id"],
                "plan_digest": plan["plan_digest"],
                "idempotency_key": "route-mcp-1",
            },
        )

    assert committed.structured_content["status"] == "committed"
    assert committed.structured_content["job_id"].startswith("job_")
    assert gateway.queued[0]["1"]["inputs"]["text"] == "a blue cat"
