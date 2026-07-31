"""G5 durable orchestration, lease fencing, and Job reconciliation contracts."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError

from comfyui_mcp_skills.adapters.mcp.orchestration import OrchestrationRuntime
from comfyui_mcp_skills.adapters.mcp.server import create_server
from comfyui_mcp_skills.application.auth_context import reset_authorization, set_authorization
from comfyui_mcp_skills.application.authorization import AuthorizationContext, Scope, Toolset
from comfyui_mcp_skills.application.orchestration import (
    ComfyUIReconcileProbe,
    JobReconciler,
    OperationOrchestrator,
    ReconcileObservation,
)
from comfyui_mcp_skills.application.planning import ExecutionPlanningService
from comfyui_mcp_skills.domain.errors import ServerOffline
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.g3_migration import (
    build_g3_import_plan,
    cutover_g3_import_plan,
)
from comfyui_mcp_skills.infrastructure.persistence.orchestration import (
    SQLiteOrchestrationRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.repository_factory import RepositoryBundle
from comfyui_mcp_skills.infrastructure.persistence.sqlite_assets import SQLiteAssetRepository
from comfyui_mcp_skills.infrastructure.persistence.sqlite_runs import SQLiteRunRepository
from comfyui_mcp_skills.infrastructure.persistence.sqlite_workflows import SQLiteWorkflowRepository


def _project(root: Path) -> SQLiteControlPlaneStore:
    workflow = root / "data" / "local" / "portrait"
    workflow.mkdir(parents=True)
    (workflow / "schema.json").write_text(
        '{"description":"Portrait","enabled":true,"parameters":{}}', encoding="utf-8"
    )
    (workflow / "workflow.json").write_text("{}", encoding="utf-8")
    store = SQLiteControlPlaneStore((root / "data" / "control-plane.sqlite3").resolve())
    store.initialize()
    cutover_g3_import_plan(build_g3_import_plan(root), store)
    return store


def _scheduled_job(root: Path) -> tuple[SQLiteControlPlaneStore, str, str]:
    store = _project(root)
    planning = ExecutionPlanningService(store, SQLiteWorkflowRepository(store))
    identity = planning.materialize(
        server_id="local",
        workflow_id="portrait",
        owner_id="principal",
        arguments={},
        client_id="client-1",
    )
    with sqlite3.connect(store.path) as connection:
        work_item_id = str(
            connection.execute(
                "SELECT work_item_id FROM operation_work_items WHERE subject_uri = ?",
                (f"comfyui://jobs/{identity.job_id}",),
            ).fetchone()[0]
        )
        assert connection.execute(
            "SELECT count(*) FROM domain_events WHERE subject_uri = ?",
            (f"comfyui://jobs/{identity.job_id}",),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM outbox WHERE topic = 'resources.updated'",
        ).fetchone() == (1,)
    return store, identity.job_id, work_item_id


def test_plan_job_work_event_and_outbox_are_committed_atomically(tmp_path: Path) -> None:
    store, job_id, work_item_id = _scheduled_job(tmp_path)

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT work_type, status, checkpoint_json FROM operation_work_items "
            "WHERE work_item_id = ?",
            (work_item_id,),
        ).fetchone() == ("job.reconcile", "pending", "{}")
        assert connection.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone() == ("reserved",)


def test_expired_lease_can_be_reclaimed_and_stale_worker_is_fenced(tmp_path: Path) -> None:
    store, _job_id, work_item_id = _scheduled_job(tmp_path)
    repository = SQLiteOrchestrationRepository(store)
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)

    first = repository.acquire_next("worker-1", now=now, lease_seconds=30)
    assert first is not None and first.work_item_id == work_item_id
    repository = SQLiteOrchestrationRepository(store)
    assert repository.acquire_next("worker-2", now=now, lease_seconds=30) is None

    second = repository.acquire_next("worker-2", now=now + timedelta(seconds=31), lease_seconds=30)
    assert second is not None
    assert second.fencing_token == first.fencing_token + 1
    repository.checkpoint(second, {"step": 1}, now=now + timedelta(seconds=31))

    try:
        repository.checkpoint(first, {"step": 99}, now=now + timedelta(seconds=32))
    except RuntimeError as exc:
        assert "fenced" in str(exc)
    else:
        raise AssertionError("stale lease unexpectedly updated the work item")


class _Probe:
    def __init__(self, observations: list[ReconcileObservation]) -> None:
        self._observations = iter(observations)

    def __call__(self, server_id: str, prompt_id: str, client_id: str) -> ReconcileObservation:
        assert server_id == "local"
        assert prompt_id == "prompt-1"
        assert client_id == "client-1"
        return next(self._observations)


def test_reconciler_requires_repeated_missing_and_generation_evidence_for_lost(
    tmp_path: Path,
) -> None:
    store, job_id, _work_item_id = _scheduled_job(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE execution_attempts SET upstream_prompt_id = 'prompt-1', "
            "submission_state = 'submitted' WHERE job_id = ?",
            (job_id,),
        )
        connection.execute("UPDATE jobs SET status = 'submitted' WHERE job_id = ?", (job_id,))
        connection.commit()
    repository = SQLiteOrchestrationRepository(store)
    probe = _Probe(
        [
            ReconcileObservation(online=False),
            ReconcileObservation(online=True, generation="generation-a", state="missing"),
            ReconcileObservation(online=True, generation="generation-b", state="missing"),
        ]
    )
    orchestrator = OperationOrchestrator(
        repository,
        {"job.reconcile": JobReconciler(repository, probe, missing_threshold=2)},
    )
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)

    assert orchestrator.run_once("worker", now=now)
    assert orchestrator.run_once("worker", now=now + timedelta(seconds=31))
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone() == ("submitted",)
    assert orchestrator.run_once("worker", now=now + timedelta(seconds=62))

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone() == ("lost",)
        assert connection.execute(
            "SELECT event_type FROM domain_events WHERE subject_uri = ? ORDER BY sequence DESC",
            (f"comfyui://jobs/{job_id}",),
        ).fetchone() == ("UPSTREAM_STATE_LOST",)
        assert connection.execute(
            "SELECT status FROM operation_work_items WHERE subject_uri = ?",
            (f"comfyui://jobs/{job_id}",),
        ).fetchone() == ("completed",)


class _RecoveryProbe:
    def __call__(self, server_id: str, prompt_id: str, client_id: str) -> ReconcileObservation:
        assert (server_id, prompt_id, client_id) == ("local", "", "client-1")
        return ReconcileObservation(
            True,
            "generation-a",
            "queued",
            upstream_prompt_id="prompt-recovered",
        )


def test_reconciler_recovers_unknown_submission_by_stable_client_id(tmp_path: Path) -> None:
    store, job_id, _work_item_id = _scheduled_job(tmp_path)
    repository = SQLiteOrchestrationRepository(store)
    orchestrator = OperationOrchestrator(
        repository,
        {"job.reconcile": JobReconciler(repository, _RecoveryProbe())},
    )

    assert orchestrator.run_once("worker", now=datetime(2026, 8, 1, tzinfo=timezone.utc))

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT upstream_prompt_id, submission_state FROM execution_attempts WHERE job_id = ?",
            (job_id,),
        ).fetchone() == ("prompt-recovered", "submitted")
        assert connection.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone() == ("queued",)


def test_reconciliation_schedule_rolls_back_with_plan_and_job(tmp_path: Path) -> None:
    store = _project(tmp_path)
    planning = ExecutionPlanningService(store, SQLiteWorkflowRepository(store))

    def fail(phase: str) -> None:
        if phase == "after_reconciliation_schedule":
            raise RuntimeError("injected")

    try:
        planning.materialize(
            server_id="local",
            workflow_id="portrait",
            owner_id="principal",
            arguments={},
            client_id="client-rollback",
            failure_injector=fail,
        )
    except RuntimeError as exc:
        assert str(exc) == "injected"
    else:
        raise AssertionError("failure injection did not abort planning")

    with sqlite3.connect(store.path) as connection:
        assert tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "execution_plans",
                "jobs",
                "operation_work_items",
                "domain_events",
                "outbox",
            )
        ) == (0, 0, 0, 0, 0)


@pytest.mark.anyio
async def test_outbox_projects_live_resource_update_without_protocol_replay(
    tmp_path: Path,
) -> None:
    store, job_id, _work_item_id = _scheduled_job(tmp_path)
    repository = SQLiteOrchestrationRepository(store)

    class RecordingBus:
        def __init__(self) -> None:
            self.events: list[object] = []

        async def publish(self, event: object) -> None:
            self.events.append(event)

    bus = RecordingBus()
    runtime = OrchestrationRuntime(
        OperationOrchestrator(repository, {}),
        repository,
        bus,  # type: ignore[arg-type]
        worker_id="worker",
    )

    assert await runtime.dispatch_outbox_once() == 1
    assert [event.uri for event in bus.events] == [f"comfyui://jobs/{job_id}"]  # type: ignore[attr-defined]
    assert await runtime.dispatch_outbox_once() == 0
    assert repository.job_context(job_id).status == "reserved"


def test_offline_observation_breaks_consecutive_missing_sequence(tmp_path: Path) -> None:
    store, job_id, _work_item_id = _scheduled_job(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE execution_attempts SET upstream_prompt_id = 'prompt-1', "
            "submission_state = 'submitted' WHERE job_id = ?",
            (job_id,),
        )
        connection.execute("UPDATE jobs SET status = 'submitted' WHERE job_id = ?", (job_id,))
        connection.commit()
    repository = SQLiteOrchestrationRepository(store)
    orchestrator = OperationOrchestrator(
        repository,
        {
            "job.reconcile": JobReconciler(
                repository,
                _Probe(
                    [
                        ReconcileObservation(True, "generation-a", "missing"),
                        ReconcileObservation(False),
                        ReconcileObservation(True, "generation-b", "missing"),
                    ]
                ),
                missing_threshold=2,
                grace_seconds=3600,
            )
        },
    )
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)

    for offset in (0, 31, 62):
        assert orchestrator.run_once("worker", now=now + timedelta(seconds=offset))

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone() == ("submitted",)
        checkpoint = json.loads(
            connection.execute("SELECT checkpoint_json FROM operation_work_items").fetchone()[0]
        )
    assert checkpoint["consecutive_missing"] == 1


def test_interrupted_history_completes_reconciliation_work(tmp_path: Path) -> None:
    store, job_id, _work_item_id = _scheduled_job(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE execution_attempts SET upstream_prompt_id = 'prompt-1', "
            "submission_state = 'submitted' WHERE job_id = ?",
            (job_id,),
        )
        connection.commit()
    repository = SQLiteOrchestrationRepository(store)
    orchestrator = OperationOrchestrator(
        repository,
        {
            "job.reconcile": JobReconciler(
                repository,
                _Probe([ReconcileObservation(True, "generation-a", "interrupted")]),
            )
        },
    )

    assert orchestrator.run_once("worker", now=datetime(2026, 8, 1, tzinfo=timezone.utc))
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone() == ("interrupted",)
        assert connection.execute("SELECT status FROM operation_work_items").fetchone() == (
            "completed",
        )


@pytest.mark.anyio
async def test_outbox_publish_failure_is_isolated_and_retried(tmp_path: Path) -> None:
    store, _job_id, _work_item_id = _scheduled_job(tmp_path)
    repository = SQLiteOrchestrationRepository(store)

    class FlakyBus:
        def __init__(self) -> None:
            self.fail = True
            self.events: list[object] = []

        async def publish(self, event: object) -> None:
            if self.fail:
                self.fail = False
                raise RuntimeError("injected publish failure")
            self.events.append(event)

    bus = FlakyBus()
    runtime = OrchestrationRuntime(
        OperationOrchestrator(repository, {}),
        repository,
        bus,  # type: ignore[arg-type]
        worker_id="worker",
    )

    assert await runtime.dispatch_outbox_once() == 0
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT status FROM outbox").fetchone() == ("pending",)
    assert await runtime.dispatch_outbox_once() == 1
    assert len(bus.events) == 1


def test_comfyui_probe_maps_interrupted_history_to_terminal_state(tmp_path: Path) -> None:
    class Servers:
        def connection(self, server_id: str) -> dict[str, object]:
            assert server_id == "local"
            return {}

    class Gateway:
        def get_system_stats(self) -> dict[str, object]:
            return {"generation": "generation-a"}

        def get_history(self, prompt_id: str) -> dict[str, object]:
            assert prompt_id == "prompt-1"
            return {"status": {"status_str": "interrupted"}}

    probe = ComfyUIReconcileProbe(
        Servers(),  # type: ignore[arg-type]
        lambda _config: Gateway(),  # type: ignore[arg-type,return-value]
    )

    observation = probe("local", "prompt-1", "client-1")

    assert observation.state == "interrupted"


@pytest.mark.anyio
async def test_outbox_discards_owner_mismatch_without_starving_next_message(
    tmp_path: Path,
) -> None:
    store, job_id, _work_item_id = _scheduled_job(tmp_path)
    with sqlite3.connect(store.path) as connection:
        payload = json.loads(connection.execute("SELECT payload_json FROM outbox").fetchone()[0])
        payload["owner_id"] = "other-principal"
        connection.execute(
            "UPDATE outbox SET payload_json = ?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")),),
        )
        occurred_at = "2026-08-01T00:00:00+00:00"
        subject_uri = f"comfyui://jobs/{job_id}"
        connection.execute(
            "INSERT INTO domain_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "event-valid",
                "JOB_UPDATED",
                subject_uri,
                2,
                occurred_at,
                "principal",
                "correlation-valid",
                "{}",
            ),
        )
        connection.execute(
            "INSERT INTO outbox(outbox_id, event_id, topic, payload_json, status, created_at) "
            "VALUES (?, ?, 'resources.updated', ?, 'pending', ?)",
            (
                "outbox-valid",
                "event-valid",
                json.dumps(
                    {"uri": subject_uri, "sequence": 2, "owner_id": "principal"},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                occurred_at,
            ),
        )
        connection.commit()
    repository = SQLiteOrchestrationRepository(store)

    class RecordingBus:
        def __init__(self) -> None:
            self.events: list[object] = []

        async def publish(self, event: object) -> None:
            self.events.append(event)

    bus = RecordingBus()
    runtime = OrchestrationRuntime(
        OperationOrchestrator(repository, {}),
        repository,
        bus,  # type: ignore[arg-type]
        worker_id="worker",
    )

    assert await runtime.dispatch_outbox_once() == 1
    assert [event.uri for event in bus.events] == [f"comfyui://jobs/{job_id}"]  # type: ignore[attr-defined]
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT status FROM outbox ORDER BY outbox_id").fetchall() == [
            ("delivered",),
            ("delivered",),
        ]


@pytest.mark.anyio
async def test_job_resource_subscription_rejects_different_owner(tmp_path: Path) -> None:
    store, job_id, _work_item_id = _scheduled_job(tmp_path)
    workflows = SQLiteWorkflowRepository(store)
    repositories = RepositoryBundle(
        workflows,
        SQLiteRunRepository(store),
        SQLiteAssetRepository(store),
        "sqlite",
        "sqlite",
        "sqlite",
        store,
    )
    authorization = AuthorizationContext("other-principal", frozenset(Scope), Toolset.EXECUTION)

    class OfflineGateway:
        def get_system_stats(self) -> dict[str, object]:
            raise ServerOffline("offline")

    server = create_server(
        tmp_path,
        repositories=repositories,
        authorization=authorization,
        gateway_factory=lambda _config: OfflineGateway(),  # type: ignore[arg-type,return-value]
    )
    token = set_authorization(authorization)
    try:
        async with Client(server) as client:
            with pytest.raises(MCPError, match="Resource unavailable"):
                async with client.listen(resource_subscriptions=[f"comfyui://jobs/{job_id}"]):
                    pass
    finally:
        reset_authorization(token)


def test_comfyui_probe_bounds_total_history_items(tmp_path: Path) -> None:
    class Servers:
        def connection(self, server_id: str) -> dict[str, object]:
            assert server_id == "local"
            return {}

    class Gateway:
        def get_system_stats(self) -> dict[str, object]:
            return {"generation": "generation-a"}

        def get_queue(self) -> dict[str, object]:
            return {"queue_running": [], "queue_pending": []}

        def get_history_list(self, max_items: int = 20) -> dict[str, object]:
            assert max_items == 100
            return {f"prompt-{index}": {} for index in range(101)}

    probe = ComfyUIReconcileProbe(
        Servers(),  # type: ignore[arg-type]
        lambda _config: Gateway(),  # type: ignore[arg-type,return-value]
    )

    observation = probe("local", "", "client-1")

    assert observation.online is True
    assert observation.state == "unknown"
    assert "item limit" in observation.error
