"""Phase G2 runtime restart execution: plan/approve/commit, drain/fence,
admission lifecycle, crash recovery, and backend gating."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from comfyui_mcp_skills.adapters.mcp.server import create_server
from comfyui_mcp_skills.application.authorization import AuthorizationContext, Scope, Toolset
from comfyui_mcp_skills.application.catalog import WorkflowCatalog
from comfyui_mcp_skills.application.execution import ExecutionService
from comfyui_mcp_skills.application.runtime_restart import RuntimeRestartService
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import (
    RestartApprovalInvalid,
    RestartExecutionFailed,
    RestartFenced,
    RestartPlanConflict,
    RestartPlanNotFound,
)
from comfyui_mcp_skills.infrastructure.persistence.assets import FileAssetRepository
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.runtime_restart import (
    SQLiteRuntimeRestartRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_runs import SQLiteRunRepository
from comfyui_mcp_skills.infrastructure.persistence.workflows import FileWorkflowRepository

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeController:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def restart(self, server_id: str) -> dict[str, Any]:
        self.calls.append(server_id)
        if self.fail:
            raise RuntimeError("controller restart failed")
        return {"server_id": server_id, "adapter": "test", "completed": True}


def _project(tmp_path: Path) -> Path:
    base = tmp_path / "proj"
    base.mkdir(parents=True)
    (base / "config.json").write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "id": "local",
                        "url": "http://127.0.0.1:8188",
                        "runtime": {"adapter": "test"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return base


def _store(base: Path) -> SQLiteControlPlaneStore:
    store = SQLiteControlPlaneStore((base / "data" / "control-plane.sqlite3").resolve())
    store.initialize()
    return store


def _seed_job(
    store: SQLiteControlPlaneStore,
    job_id: str,
    *,
    owner_id: str = "owner-a",
    status: str = "queued",
    server_id: str = "local",
) -> None:
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, workflow_id, owner_id, status, created_at, created_at_source,
                legacy_migrated, execution_origin, server_id
            ) VALUES (?, 'freeze_workflow', ?, ?, ?, 'test', 1, 'legacy_migrated', ?)
            """,
            (job_id, owner_id, status, _NOW.isoformat(), server_id),
        )


def _repository(store: SQLiteControlPlaneStore) -> SQLiteRuntimeRestartRepository:
    return SQLiteRuntimeRestartRepository(store, clock=lambda: _NOW)


def _service(
    base: Path,
    store: SQLiteControlPlaneStore,
    controller: FakeController | None = None,
    *,
    drain_wait: float = 0.1,
    drain_poll: float = 0.01,
) -> RuntimeRestartService:
    return RuntimeRestartService(
        ServerRegistry(base),
        _repository(store),
        controller_provider=(lambda _sid: controller) if controller is not None else None,
        clock=lambda: _NOW,
        drain_wait_seconds=drain_wait,
        drain_poll_seconds=drain_poll,
    )


def _job_id(seed: str) -> str:
    return "job_" + seed * 32


def _switch(store: SQLiteControlPlaneStore, kinds: tuple[str, ...]) -> None:
    with sqlite3.connect(store.path) as connection:
        connection.executemany(
            """
            INSERT INTO store_migrations(
                aggregate_kind, version, status, checksum, switched_at
            ) VALUES (?, 1, 'switched', ?, '2026-07-30T00:00:00+00:00')
            """,
            [(kind, "a" * 64) for kind in kinds],
        )


def _plan(service: RuntimeRestartService) -> dict[str, Any]:
    return service.plan("local", "owner-a")


def _approved(service: RuntimeRestartService, plan: dict[str, Any]) -> dict[str, Any]:
    return service.approve(plan["plan_id"], "approved", "owner-a", "go")


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def test_plan_persists_server_wide_active_snapshot(tmp_path: Path) -> None:
    base = _project(tmp_path)
    store = _store(base)
    _seed_job(store, _job_id("a"), owner_id="owner-a")
    _seed_job(store, _job_id("b"), owner_id="owner-b")
    _seed_job(store, _job_id("c"), owner_id="owner-c", status="completed")
    service = _service(base, store, FakeController())

    plan = _plan(service)

    assert plan["status"] == "planned"
    assert plan["approved_impact_summary"] == {"job_count": 2}
    assert plan["controller_available"] is True
    assert plan["approval_id"].startswith("runtime_approval_")
    impact = service.get(plan["plan_id"], "owner-a")["impact_jobs"]
    assert {row["owner_id"] for row in impact} == {"owner-a", "owner-b"}


def test_plan_reuses_pending_same_digest_but_not_terminal(tmp_path: Path) -> None:
    base = _project(tmp_path)
    store = _store(base)
    _seed_job(store, _job_id("a"))
    service = _service(base, store, FakeController())

    first = _plan(service)
    second = _plan(service)
    assert second["plan_id"] == first["plan_id"]  # pending reuse

    _approved(service, first)
    service.commit(
        first["plan_id"],
        first["plan_digest"],
        first["approval_id"],
        "owner-a",
        "request-1",
    )
    third = _plan(service)
    assert third["plan_id"] != first["plan_id"]  # terminal never reused


def test_plan_allows_exactly_ten_thousand_active_jobs(tmp_path: Path) -> None:
    base = _project(tmp_path)
    store = _store(base)
    rows = [
        (
            f"job_{i:032x}",
            "freeze_workflow",
            "owner-a",
            "queued",
            _NOW.isoformat(),
            "test",
            1,
            "legacy_migrated",
            "local",
        )
        for i in range(10000)
    ]
    with sqlite3.connect(store.path) as connection:
        connection.executemany(
            """
            INSERT INTO jobs(
                job_id, workflow_id, owner_id, status, created_at, created_at_source,
                legacy_migrated, execution_origin, server_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    service = _service(base, store, FakeController())

    plan = _plan(service)

    assert plan["status"] == "planned"
    assert plan["approved_impact_summary"] == {"job_count": 10000}


def test_recovery_receipt_matches_terminal_shape(tmp_path: Path) -> None:
    base = _project(tmp_path)
    store = _store(base)
    _seed_job(store, _job_id("a"))
    service = _service(base, store)
    plan = _plan(service)
    _approved(service, plan)
    service._repository.begin_drain(plan["plan_id"], "request-1", _NOW)

    service.recover()

    session = service._repository.get_plan(plan["plan_id"], "owner-a")
    receipt = session["commit_result"]
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "RESTART_INTERRUPTED_UNKNOWN"
    assert receipt["retryable"] is True
    assert receipt["execution_impact_summary"] == session["execution_impact_summary"]
    assert receipt["execution_impact_digest"] == session["execution_impact_digest"]
    assert receipt["committed_at"] == session["committed_at"]


def test_commit_failure_with_custom_details_replays_identically(tmp_path: Path) -> None:
    base = _project(tmp_path)
    store = _store(base)

    class CustomFailure(RestartExecutionFailed):
        def __init__(self) -> None:
            super().__init__(
                "custom controller error",
                details={"error_code": "CUSTOM_CTL", "retryable": False, "hint": "x"},
            )

    class FailingController:
        def restart(self, server_id: str) -> dict[str, Any]:
            raise CustomFailure()

    service = _service(base, store, FailingController())
    plan = _plan(service)
    _approved(service, plan)

    with pytest.raises(RestartExecutionFailed) as first:
        service.commit(
            plan["plan_id"], plan["plan_digest"], plan["approval_id"], "owner-a", "request-1"
        )
    with pytest.raises(RestartExecutionFailed) as replay:
        service.commit(
            plan["plan_id"], plan["plan_digest"], plan["approval_id"], "owner-a", "request-1"
        )
    assert first.value.as_dict()["code"] == "RESTART_EXECUTION_FAILED"
    assert first.value.as_dict()["details"]["error_code"] == "CUSTOM_CTL"
    assert first.value.as_dict()["details"]["hint"] == "x"
    assert replay.value.as_dict() == first.value.as_dict()


def test_commit_receipt_committed_at_matches_terminal_column(tmp_path: Path) -> None:
    base = _project(tmp_path)
    store = _store(base)
    _seed_job(store, _job_id("a"))

    class TickingController:
        def __init__(self) -> None:
            self.calls = 0

        def restart(self, server_id: str) -> dict[str, Any]:
            return {"server_id": server_id, "adapter": "test", "completed": True}

    controller = TickingController()
    times = iter(
        [
            _NOW,
            _NOW + timedelta(seconds=1),
            _NOW + timedelta(seconds=2),
            _NOW + timedelta(seconds=3),
        ]
    )
    service = RuntimeRestartService(
        ServerRegistry(base),
        _repository(store),
        controller_provider=lambda _sid: controller,
        clock=lambda: next(times),
        drain_wait_seconds=0.1,
        drain_poll_seconds=0.01,
    )
    plan = _plan(service)
    _approved(service, plan)
    result = service.commit(
        plan["plan_id"], plan["plan_digest"], plan["approval_id"], "owner-a", "request-1"
    )
    session = service._repository.get_plan(plan["plan_id"], "owner-a")

    assert result["commit_result"]["committed_at"] == session["committed_at"]


def test_plan_fails_closed_when_active_impact_exceeds_bound(tmp_path: Path) -> None:
    base = _project(tmp_path)
    store = _store(base)
    service = _service(base, store, FakeController())
    rows = [
        (
            f"job_{i:032x}",
            "freeze_workflow",
            "owner-a",
            "queued",
            _NOW.isoformat(),
            "test",
            1,
            "legacy_migrated",
            "local",
        )
        for i in range(10001)
    ]
    with sqlite3.connect(store.path) as connection:
        connection.executemany(
            """
            INSERT INTO jobs(
                job_id, workflow_id, owner_id, status, created_at, created_at_source,
                legacy_migrated, execution_origin, server_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    with pytest.raises(ValueError, match="bound"):
        _plan(service)


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


def test_approve_accepts_and_rejects_once(tmp_path: Path) -> None:
    base = _project(tmp_path)
    store = _store(base)
    service = _service(base, store)
    plan = _plan(service)

    approved = _approved(service, plan)
    assert approved["status"] == "approved"
    assert approved["approval_actor"] == "owner-a"
    with pytest.raises(RestartApprovalInvalid):
        service.approve(plan["plan_id"], "approved", "owner-a", "again")


def test_approve_rejects_wrong_owner_or_unknown_plan(tmp_path: Path) -> None:
    base = _project(tmp_path)
    store = _store(base)
    service = _service(base, store)
    plan = _plan(service)

    with pytest.raises(RestartPlanNotFound):
        service.approve("runtime_plan_" + "0" * 32, "approved", "owner-a", "")
    with pytest.raises(RestartPlanNotFound):
        service.approve(plan["plan_id"], "approved", "owner-other", "")


def test_approve_expired_plan_is_rejected(tmp_path: Path) -> None:
    base = _project(tmp_path)
    store = _store(base)
    service = _service(base, store)
    plan = _plan(service)
    # Clock past the approval window.
    service._clock = lambda: _NOW + timedelta(hours=2)

    with pytest.raises(RestartApprovalInvalid):
        service.approve(plan["plan_id"], "approved", "owner-a", "")


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------


def test_commit_executes_after_drain_and_releases_fence(tmp_path: Path) -> None:
    base = _project(tmp_path)
    store = _store(base)
    _seed_job(store, _job_id("a"))
    controller = FakeController()
    service = _service(base, store, controller)
    plan = _plan(service)
    _approved(service, plan)

    result = service.commit(
        plan["plan_id"], plan["plan_digest"], plan["approval_id"], "owner-a", "request-1"
    )

    assert result["status"] == "completed"
    assert controller.calls == ["local"]
    assert result["commit_result"]["status"] == "completed"
    # fence released
    assert service._repository.active_restart("local") is None
    # approved snapshot untouched (dual snapshot)
    assert result["approved_impact_summary"] == {"job_count": 1}
    assert result["commit_result"]["execution_impact_summary"]["job_count"] == 1


def test_commit_replays_successful_receipt_without_second_restart(tmp_path: Path) -> None:
    base = _project(tmp_path)
    store = _store(base)
    controller = FakeController()
    service = _service(base, store, controller)
    plan = _plan(service)
    _approved(service, plan)

    first = service.commit(
        plan["plan_id"], plan["plan_digest"], plan["approval_id"], "owner-a", "request-1"
    )
    second = service.commit(
        plan["plan_id"], plan["plan_digest"], plan["approval_id"], "owner-a", "request-1"
    )

    assert second["status"] == "completed"
    assert second == first  # replay returns the identical public view
    assert "approval_uri" in second
    assert "commit_request_id" not in second
    assert controller.calls == ["local"]


def test_get_paginates_impact_jobs_without_gaps(tmp_path: Path) -> None:
    base = _project(tmp_path)
    store = _store(base)
    for i in range(5):
        _seed_job(store, f"job_{i:032x}", owner_id="owner-a")
    service = _service(base, store, FakeController())
    plan = _plan(service)

    page1 = service.get(plan["plan_id"], "owner-a", limit=2, cursor=0)
    assert [row["ordinal"] for row in page1["impact_jobs"]] == [0, 1]
    assert page1["next_cursor"] == 2
    assert page1["impact_total"] == 5
    page2 = service.get(plan["plan_id"], "owner-a", limit=2, cursor=page1["next_cursor"])
    assert [row["ordinal"] for row in page2["impact_jobs"]] == [2, 3]
    assert page2["next_cursor"] == 4
    page3 = service.get(plan["plan_id"], "owner-a", limit=2, cursor=page2["next_cursor"])
    assert [row["ordinal"] for row in page3["impact_jobs"]] == [4]
    assert page3["next_cursor"] is None
    assert [r["job_id"] for p in (page1, page2, page3) for r in p["impact_jobs"]] == [
        f"job_{i:032x}" for i in range(5)
    ]


def test_commit_failure_writes_receipt_and_replay_reraises(tmp_path: Path) -> None:
    base = _project(tmp_path)
    store = _store(base)
    controller = FakeController(fail=True)
    service = _service(base, store, controller)
    plan = _plan(service)
    _approved(service, plan)

    with pytest.raises(RestartExecutionFailed) as first:
        service.commit(
            plan["plan_id"], plan["plan_digest"], plan["approval_id"], "owner-a", "request-1"
        )
    assert service._repository.active_restart("local") is None  # fence released
    assert service._repository.get_plan(plan["plan_id"], "owner-a")["status"] == "failed"
    with pytest.raises(RestartExecutionFailed) as replay:
        service.commit(
            plan["plan_id"], plan["plan_digest"], plan["approval_id"], "owner-a", "request-1"
        )
    assert replay.value.as_dict() == first.value.as_dict()  # identical error replay
    assert controller.calls == ["local"]


def test_commit_rejects_unapproved_digest_mismatch_or_wrong_approval(tmp_path: Path) -> None:
    base = _project(tmp_path)
    store = _store(base)
    service = _service(base, store, FakeController())
    plan = _plan(service)

    with pytest.raises(RestartPlanConflict, match="digest"):
        service.commit(
            plan["plan_id"], "0" * 64, plan["approval_id"], "owner-a", "request-1"
        )
    with pytest.raises(RestartApprovalInvalid):
        service.commit(
            plan["plan_id"],
            plan["plan_digest"],
            "runtime_approval_" + "1" * 32,
            "owner-a",
            "request-1",
        )
    _approved(service, plan)
    # A different request on an approved plan executes normally.
    first = service.commit(
        plan["plan_id"], plan["plan_digest"], plan["approval_id"], "owner-a", "request-2"
    )
    assert first["status"] == "completed"
    # After completion, further commits conflict (plan no longer approved).
    with pytest.raises(RestartPlanConflict):
        service.commit(
            plan["plan_id"],
            plan["plan_digest"],
            plan["approval_id"],
            "owner-a",
            "request-3",
        )


def test_second_commit_conflicts_while_restart_active(tmp_path: Path) -> None:
    base = _project(tmp_path)
    store = _store(base)
    controller = FakeController()
    service = _service(base, store, controller)
    first = _plan(service)
    _approved(service, first)
    # Hold the fence open: simulate an in-flight restart.
    service._repository.begin_drain(first["plan_id"], "request-1", _NOW)

    second = _plan(service)
    assert second["plan_id"] != first["plan_id"]
    _approved(service, second)
    with pytest.raises(RestartPlanConflict, match="already draining"):
        service.commit(
            second["plan_id"],
            second["plan_digest"],
            second["approval_id"],
            "owner-a",
            "request-2",
        )
    assert controller.calls == []


def test_drain_timeout_fails_without_restart(tmp_path: Path, monkeypatch) -> None:
    base = _project(tmp_path)
    store = _store(base)
    controller = FakeController()
    service = _service(base, store, controller, drain_wait=0.05, drain_poll=0.01)
    plan = _plan(service)
    _approved(service, plan)

    def stuck(server_id: str) -> int:
        return 1

    monkeypatch.setattr(service._repository, "pending_admissions", stuck)
    with pytest.raises(RestartExecutionFailed, match="drain"):
        service.commit(
            plan["plan_id"], plan["plan_digest"], plan["approval_id"], "owner-a", "request-1"
        )
    assert controller.calls == []
    assert service._repository.get_plan(plan["plan_id"], "owner-a")["status"] == "failed"
    assert service._repository.active_restart("local") is None


# ---------------------------------------------------------------------------
# fence and admission lifecycle
# ---------------------------------------------------------------------------


def test_fenced_submissions_are_rejected_before_claim(tmp_path: Path) -> None:
    base = _project(tmp_path)
    store = _store(base)
    _seed_job(store, _job_id("a"))
    service = _service(base, store)
    plan = _plan(service)
    _approved(service, plan)
    # Manually open the fence (draining) without executing the restart.
    service._repository.begin_drain(plan["plan_id"], "request-1", _NOW)

    runs = service._repository  # SQLiteRuntimeRestartRepository exposes admit/release
    with pytest.raises(RestartFenced) as excinfo:
        runs.admit("local", "admission_" + "a" * 32, _NOW)
    assert excinfo.value.code == "HOST_RESTART_IN_PROGRESS"
    assert excinfo.value.retryable is True
    # fence cleared -> admit succeeds
    service._repository.fail(
        plan["plan_id"],
        {"status": "failed", "error_code": "X", "retryable": False},
        error="test",
        now=_NOW,
    )
    token = runs.admit("local", "admission_" + "b" * 32, _NOW)
    assert token == "admission_" + "b" * 32
    runs.release_admission(token)


def test_admission_lifecycle_clears_on_release(tmp_path: Path) -> None:
    base = _project(tmp_path)
    store = _store(base)
    repository = _repository(store)
    repository.admit("local", "admission_" + "a" * 32, _NOW)
    assert repository.pending_admissions("local") == 1
    repository.release_admission("admission_" + "a" * 32)
    assert repository.pending_admissions("local") == 0
    repository.release_admission("admission_" + "a" * 32)  # idempotent


def test_recovery_clears_admissions_and_fails_orphaned_sessions(tmp_path: Path) -> None:
    base = _project(tmp_path)
    store = _store(base)
    service = _service(base, store)
    # Admission first (no fence yet), then open the fence as an orphaned drain.
    service._repository.admit("local", "admission_" + "c" * 32, _NOW)
    plan = _plan(service)
    _approved(service, plan)
    service._repository.begin_drain(plan["plan_id"], "request-1", _NOW)

    recovered = service.recover()

    assert recovered["cleared_admissions"] == 1
    assert recovered["recovered_plans"] == 1
    session = service._repository.get_plan(plan["plan_id"], "owner-a")
    assert session["status"] == "failed"
    assert session["error"] == "restart_interrupted_unknown"
    assert service._repository.active_restart("local") is None


# ---------------------------------------------------------------------------
# backend gating and MCP integration
# ---------------------------------------------------------------------------


def test_restart_execution_tools_gated_on_file_backend(tmp_path: Path) -> None:
    base = _project(tmp_path)  # fresh: no control-plane DB yet
    auth = AuthorizationContext(
        "local-operator", frozenset({Scope.OBSERVE, Scope.OPERATE}), Toolset.OPERATIONS
    )
    server = create_server(
        base,
        gateway_factory=lambda _config: _Gateway(),
        authorization=auth,
    )
    import anyio

    async def listing() -> list[str]:
        async with Client(server) as client:
            result = await client.list_tools()
            return [tool.name for tool in result.tools]

    names = anyio.run(listing)
    assert "comfyui.runtime.restart.plan" in names
    assert "comfyui.runtime.restart.approve" not in names
    assert "comfyui.runtime.restart.commit" not in names
    assert "comfyui.runtime.restart.get" not in names


class _Gateway:
    def __init__(self) -> None:
        self.queued: list[dict[str, Any]] = []

    def queue_prompt(
        self, workflow: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        self.queued.append(workflow)
        return {"prompt_id": "prompt-g2", "client_id": "client-g2"}

    def get_queue(self) -> dict[str, Any]:
        return {"queue_running": [], "queue_pending": []}

    def get_history(self, prompt_id: str, timeout_seconds: float | None = None) -> None:
        return None

    def get_object_info(self) -> dict[str, Any]:
        return {}

    def get_system_stats(self) -> dict[str, Any]:
        return {"system": {"os": "test"}}


def test_mcp_restart_flow_end_to_end(tmp_path: Path) -> None:
    import anyio

    base = _project(tmp_path)
    store = _store(base)
    _switch(store, ("job", "execution_attempt", "idempotency_record", "artifact"))
    _seed_job(store, _job_id("a"))
    controller = FakeController()
    auth = AuthorizationContext(
        "local-operator", frozenset({Scope.OBSERVE, Scope.OPERATE}), Toolset.OPERATIONS
    )
    server = create_server(
        base,
        gateway_factory=lambda _config: _Gateway(),
        authorization=auth,
        runtime_controller_provider=lambda _sid: controller,
    )

    async def flow() -> tuple[dict[str, Any], dict[str, Any]]:
        async with Client(server) as client:
            planned = await client.call_tool(
                "comfyui.runtime.restart.plan", {"server_id": "local"}
            )
            plan = _structured(planned)
            approved = await client.call_tool(
                "comfyui.runtime.restart.approve",
                {
                    "plan_id": plan["plan_id"],
                    "decision": "approved",
                    "reason": "scheduled maintenance",
                },
            )
            assert _structured(approved)["status"] == "approved"
            committed = await client.call_tool(
                "comfyui.runtime.restart.commit",
                {
                    "plan_id": plan["plan_id"],
                    "plan_digest": plan["plan_digest"],
                    "approval_id": plan["approval_id"],
                    "request_id": "mcp-request-1",
                },
            )
            result = _structured(committed)
            assert result["status"] == "completed"
            fetched = await client.call_tool(
                "comfyui.runtime.restart.get", {"plan_id": plan["plan_id"]}
            )
            return result, _structured(fetched)

    result, fetched = anyio.run(flow)
    assert controller.calls == ["local"]
    assert fetched["status"] == "completed"
    assert fetched["approved_impact_summary"] == {"job_count": 1}


def test_fenced_submission_through_execution_service(tmp_path: Path) -> None:
    base = _project(tmp_path)
    _workflow_project(base)
    store = _store(base)
    _switch(store, ("job", "execution_attempt", "idempotency_record", "artifact"))
    restart_repo = SQLiteRuntimeRestartRepository(store)
    runs = SQLiteRunRepository(store)
    catalog = WorkflowCatalog(FileWorkflowRepository(base))
    assets = FileAssetRepository(base)
    execution = ExecutionService(
        catalog, ServerRegistry(base), runs, assets, lambda _config: _Gateway()
    )
    service = RuntimeRestartService(ServerRegistry(base), restart_repo, clock=lambda: _NOW)
    plan = _plan(service)
    _approved(service, plan)
    restart_repo.begin_drain(plan["plan_id"], "request-1", _NOW)

    # Idempotency-keyed submission: rejected before the claim.
    with pytest.raises(RestartFenced):
        execution.submit(
            "local",
            "txt2img",
            {"prompt": "blocked"},
            idempotency_key="key-blocked",
            owner_id="owner-a",
        )
    # Bare submission (no idempotency key): rejected before the gateway.
    with pytest.raises(RestartFenced):
        execution.submit("local", "txt2img", {"prompt": "blocked-bare"}, owner_id="owner-a")

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM jobs WHERE owner_id='owner-a'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM idempotency_records WHERE owner_id='owner-a'"
        ).fetchone() == (0,)
    assert restart_repo.pending_admissions("local") == 0


def test_normal_submission_releases_admission(tmp_path: Path) -> None:
    base = _project(tmp_path)
    _workflow_project(base)
    store = _store(base)
    _switch(store, ("job", "execution_attempt", "idempotency_record", "artifact"))
    restart_repo = SQLiteRuntimeRestartRepository(store)
    runs = SQLiteRunRepository(store)
    catalog = WorkflowCatalog(FileWorkflowRepository(base))
    execution = ExecutionService(
        catalog, ServerRegistry(base), runs, FileAssetRepository(base), lambda _config: _Gateway()
    )

    job = execution.submit(
        "local", "txt2img", {"prompt": "hello"}, idempotency_key="key-ok", owner_id="owner-a"
    )

    assert job.status == "submitted"
    assert restart_repo.pending_admissions("local") == 0  # finally contract cleaned it


def _workflow_project(base: Path) -> None:
    directory = base / "data" / "local" / "txt2img"
    directory.mkdir(parents=True)
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


def _structured(result: Any) -> dict[str, Any]:
    for content in result.content:
        if getattr(content, "type", "") == "text":
            return json.loads(content.text)
    raise AssertionError("no structured text result")
