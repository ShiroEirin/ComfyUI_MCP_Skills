"""Layered assembly: fresh entry points stay lightweight, existing DBs fail closed."""

from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import anyio
import pytest
from mcp.shared.subscriptions import ResourceUpdated

from comfyui_mcp_skills.__main__ import _run_stdio
from comfyui_mcp_skills.adapters.mcp import server as mcp_server_adapter
from comfyui_mcp_skills.adapters.mcp.orchestration import OrchestrationRuntime
from comfyui_mcp_skills.adapters.mcp.server import create_server
from comfyui_mcp_skills.application.orchestration import OperationOrchestrator
from comfyui_mcp_skills.application.provisioning import (
    DependencyProvisioningService,
    ProvisioningWorkHandler,
)
from comfyui_mcp_skills.application.server_control import ServerControlService
from comfyui_mcp_skills.domain.orchestration import PROVISIONING_WORK_TYPE
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.orchestration import (
    SQLiteOrchestrationRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.repository_factory import (
    create_repository_bundle,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_provisioning import (
    SQLiteProvisioningRepository,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
_OWNER = "owner-a"
_DATABASE = Path("data/control-plane.sqlite3")
_INSTALL_CONFIRMATION = "INSTALL APPROVED DEPENDENCIES"


def _database(base_dir: Path) -> Path:
    return (base_dir / _DATABASE).resolve()


def _initialized_store(base_dir: Path) -> SQLiteControlPlaneStore:
    store = SQLiteControlPlaneStore(_database(base_dir))
    store.initialize()
    return store


def _switch(
    store: SQLiteControlPlaneStore,
    kinds: tuple[str, ...],
    *,
    version: int = 1,
    checksums: tuple[str, ...] | None = None,
) -> None:
    values = checksums or ("a" * 64,) * len(kinds)
    with sqlite3.connect(store.path) as connection:
        connection.executemany(
            """
            INSERT INTO store_migrations(
                aggregate_kind, version, status, checksum, switched_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (kind, version, "switched", checksum, "2026-07-30T00:00:00+00:00")
                for kind, checksum in zip(kinds, values, strict=True)
            ],
        )


class FakeGateway:
    """Minimal ComfyUI gateway double for server assembly."""

    def get_object_info(self) -> dict[str, object]:
        return {}

    def get_system_stats(self) -> dict[str, object]:
        return {"system": {"os": "test"}}


class FakeManager:
    """Manager double implementing the full gateway surface the handler touches."""

    def __init__(self, *, enqueue_state: str = "queued", observe_state: str = "completed") -> None:
        self._enqueue_state = enqueue_state
        self._observe_state = observe_state
        self.inspect_calls = 0
        self.preflight_calls = 0
        self.enqueue_calls = 0
        self.observe_calls = 0

    def inspect(self, server: dict[str, object]) -> dict[str, object]:
        self.inspect_calls += 1
        return {"available": True}

    def preflight_install(self, server: dict[str, object]) -> dict[str, object]:
        self.preflight_calls += 1
        return {"state": "available"}

    def enqueue_install(
        self, server: dict[str, object], item: dict[str, object], *, queue_id: str
    ) -> dict[str, object]:
        self.enqueue_calls += 1
        return {"state": self._enqueue_state}

    def observe_install(
        self, server: dict[str, object], queue_id: str, *, item: dict[str, object]
    ) -> dict[str, object]:
        self.observe_calls += 1
        return {"state": self._observe_state}


def _catalog() -> dict[str, dict[str, object]]:
    return {
        "node:KnownNode": {
            "kind": "node",
            "source_type": "git",
            "source_url": "https://example.com/known-node.git",
            "version": "0123456789abcdef0123456789abcdef01234567",
            "checksum": "a" * 64,
            "license": "Apache-2.0",
            "size_bytes": 1024,
            "target_dir": "custom_nodes",
            "restart_required": True,
            "install_state": "missing",
        }
    }


def _seed_provisioning_work(store: SQLiteControlPlaneStore) -> dict[str, str]:
    repository = SQLiteProvisioningRepository(store)
    server_service = ServerControlService(repository, clock=lambda: _NOW)
    server_plan = server_service.plan(
        "upsert", "local", _OWNER, {"url": "http://127.0.0.1:8188", "expected_revision": 0}
    )
    server_service.commit(server_plan["plan_id"], server_plan["plan_digest"], _OWNER)
    service = DependencyProvisioningService(repository, catalog=_catalog(), clock=lambda: _NOW)
    plan = service.plan("local", _OWNER, [{"kind": "node", "name": "KnownNode"}])
    approval_plan = service.plan_approval(plan["approval_id"], "approved", _OWNER)
    service.commit_approval(approval_plan["approval_plan_id"], approval_plan["plan_digest"], _OWNER)
    result = service.commit(
        plan["plan_id"],
        plan["plan_digest"],
        plan["approval_id"],
        _OWNER,
        "install-request-1",
        _INSTALL_CONFIRMATION,
    )
    return {"job_id": str(result["job_id"])}


def _job_id() -> str:
    return "job_" + "a" * 32


def _workflow_id() -> str:
    return "workflow_" + "b" * 32


def _seed_job_row(store: SQLiteControlPlaneStore) -> None:
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, workflow_id, owner_id, status, created_at, created_at_source,
                legacy_migrated, execution_origin
            ) VALUES (?, ?, ?, 'completed', ?, 'test', 1, 'legacy_migrated')
            """,
            (_job_id(), _workflow_id(), _OWNER, _NOW.isoformat()),
        )


def _seed_g5_outbox(store: SQLiteControlPlaneStore) -> str:
    with sqlite3.connect(store.path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        SQLiteOrchestrationRepository._append_event_and_outbox(
            connection,
            event_type="test.resource.updated",
            subject_uri=f"comfyui://jobs/{_job_id()}",
            correlation_id="test-correlation",
            data={"status": "completed"},
            occurred_at=_NOW.isoformat(),
            principal_id=_OWNER,
        )
        connection.commit()
        return str(
            connection.execute("SELECT outbox_id FROM outbox WHERE status='pending'").fetchone()[0]
        )


# ---------------------------------------------------------------------------
# Entry point laziness: fresh directories must not create the control plane
# ---------------------------------------------------------------------------


class _FakeServer:
    def create_initialization_options(self) -> object:
        return {}

    async def run(self, read_stream: object, write_stream: object, options: object) -> None:
        return None


@asynccontextmanager
async def _noop_stdio_server():
    yield (object(), object())


def test_stdio_fresh_dir_does_not_create_control_plane(tmp_path: Path, monkeypatch) -> None:
    base_dir = tmp_path / "proj"
    base_dir.mkdir()
    monkeypatch.setenv("COMFYUI_MCP_DIR", str(base_dir))
    captured: dict[str, object] = {}

    def fake_create_server(base: Path, **kwargs: object) -> _FakeServer:
        captured["base_dir"] = base
        return _FakeServer()

    monkeypatch.setattr("comfyui_mcp_skills.__main__.stdio_server", _noop_stdio_server)
    monkeypatch.setattr("comfyui_mcp_skills.__main__.create_server", fake_create_server)

    anyio.run(_run_stdio, base_dir)

    assert captured["base_dir"] == base_dir
    assert not _database(base_dir).exists()
    data_dir = base_dir / "data"
    assert not data_dir.exists() or not (data_dir / "control-plane.sqlite3").exists()


def test_http_main_fresh_dir_does_not_create_control_plane(tmp_path: Path, monkeypatch) -> None:
    base_dir = tmp_path / "proj"
    base_dir.mkdir()
    monkeypatch.setenv("COMFYUI_MCP_DIR", str(base_dir))
    monkeypatch.setenv(
        "COMFYUI_MCP_TOKENS",
        json.dumps({"t": {"principal_id": "p", "scopes": ["comfyui:execute"]}}),
    )
    monkeypatch.setenv("COMFYUI_MCP_LIMIT_MODE", "process")
    launched: dict[str, object] = {}

    def fake_uvicorn_run(app: object, **kwargs: object) -> None:
        launched["app"] = app

    monkeypatch.setattr("comfyui_mcp_skills.http_main.uvicorn.run", fake_uvicorn_run)

    from comfyui_mcp_skills import http_main

    http_main.main()

    assert launched
    assert not _database(base_dir).exists()


def test_http_app_factory_fresh_dir_does_not_create_control_plane(
    tmp_path: Path, monkeypatch
) -> None:
    base_dir = tmp_path / "proj"
    base_dir.mkdir()
    monkeypatch.setenv("COMFYUI_MCP_DIR", str(base_dir))
    monkeypatch.setenv(
        "COMFYUI_MCP_TOKENS",
        json.dumps({"t": {"principal_id": "p", "scopes": ["comfyui:execute"]}}),
    )

    from comfyui_mcp_skills import http_main

    http_main.create_app()

    assert not _database(base_dir).exists()


def test_http_external_limits_creates_shared_limits_but_not_control_plane(
    tmp_path: Path, monkeypatch
) -> None:
    base_dir = tmp_path / "proj"
    base_dir.mkdir()
    monkeypatch.setenv("COMFYUI_MCP_DIR", str(base_dir))
    monkeypatch.setenv(
        "COMFYUI_MCP_TOKENS",
        json.dumps({"t": {"principal_id": "p", "scopes": ["comfyui:execute"]}}),
    )
    monkeypatch.setenv("COMFYUI_MCP_LIMIT_MODE", "external")

    from comfyui_mcp_skills import http_main

    http_main.create_app()

    shared = base_dir / "data" / "shared-limits.sqlite3"
    assert shared.exists()
    assert not _database(base_dir).exists()


# ---------------------------------------------------------------------------
# Existing databases fail closed: orchestration assembly stays wired
# ---------------------------------------------------------------------------


def test_all_file_database_assembles_provisioning_handler_without_job_reconcile(
    tmp_path: Path,
) -> None:
    _initialized_store(tmp_path)
    captured: dict[str, object] = {}

    def fake_runtime(*args: object, **kwargs: object) -> object:
        captured["orchestrator"] = kwargs.get("orchestrator") or args[0]
        captured["repository"] = kwargs.get("repository") or args[1]
        captured["bus"] = kwargs.get("bus") or args[2]
        return object()

    with patch.object(mcp_server_adapter, "OrchestrationRuntime", side_effect=fake_runtime):
        create_server(
            tmp_path,
            gateway_factory=lambda _config: FakeGateway(),
            manager_gateway=FakeManager(),
        )

    orchestrator = captured["orchestrator"]
    assert orchestrator is not None
    handlers = orchestrator._handlers  # type: ignore[attr-defined]
    assert PROVISIONING_WORK_TYPE in handlers
    assert "job.reconcile" not in handlers


def test_job_cutover_database_assembles_job_reconcile_handler(tmp_path: Path) -> None:
    store = _initialized_store(tmp_path)
    _switch(store, ("job", "execution_attempt", "idempotency_record", "artifact"))
    captured: dict[str, object] = {}

    def fake_runtime(*args: object, **kwargs: object) -> object:
        captured["orchestrator"] = kwargs.get("orchestrator") or args[0]
        return object()

    with patch.object(mcp_server_adapter, "OrchestrationRuntime", side_effect=fake_runtime):
        create_server(tmp_path, gateway_factory=lambda _config: FakeGateway())

    orchestrator = captured["orchestrator"]
    assert orchestrator is not None
    handlers = orchestrator._handlers  # type: ignore[attr-defined]
    assert "job.reconcile" in handlers


def test_fresh_dir_does_not_assemble_orchestration_runtime(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_runtime(*args: object, **kwargs: object) -> object:
        captured["called"] = True
        return object()

    with patch.object(mcp_server_adapter, "OrchestrationRuntime", side_effect=fake_runtime):
        create_server(
            tmp_path,
            gateway_factory=lambda _config: FakeGateway(),
            manager_gateway=FakeManager(),
        )

    assert not captured
    assert not _database(tmp_path).exists()


# ---------------------------------------------------------------------------
# Real provisioning work advances through the assembled runtime
# ---------------------------------------------------------------------------


def test_real_provisioning_work_advances_through_orchestrator(tmp_path: Path) -> None:
    store = _initialized_store(tmp_path)
    job_id = _seed_provisioning_work(store)["job_id"]

    def audit_counts() -> tuple[int, int]:
        with sqlite3.connect(store.path) as connection:
            audits = int(
                connection.execute(
                    "SELECT count(*) FROM phase_o_audit_events WHERE owner_id=?", (_OWNER,)
                ).fetchone()[0]
            )
            outbox = int(
                connection.execute(
                    "SELECT count(*) FROM phase_o_outbox WHERE owner_id=?", (_OWNER,)
                ).fetchone()[0]
            )
            return audits, outbox

    baseline = audit_counts()
    assert baseline[0] >= 1  # PROVISIONING_COMMITTED from commit

    orchestration_repository = SQLiteOrchestrationRepository(store)
    provisioning_repository = SQLiteProvisioningRepository(store)
    manager = FakeManager(enqueue_state="queued", observe_state="completed")
    orchestrator = OperationOrchestrator(
        orchestration_repository,
        {
            PROVISIONING_WORK_TYPE: ProvisioningWorkHandler(
                provisioning_repository, manager, retry_delay_seconds=1
            )
        },
    )

    def work_item_state() -> tuple[str, str]:
        with sqlite3.connect(store.path) as connection:
            row = connection.execute(
                "SELECT status, checkpoint_json FROM operation_work_items "
                "WHERE work_type='provisioning.execute'"
            ).fetchone()
            return str(row[0]), str(row[1])

    def job_state() -> str:
        with sqlite3.connect(store.path) as connection:
            row = connection.execute(
                "SELECT status FROM provisioning_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            return str(row[0])

    def item_state() -> str:
        with sqlite3.connect(store.path) as connection:
            row = connection.execute(
                "SELECT status FROM provisioning_install_items WHERE job_id=?", (job_id,)
            ).fetchone()
            return str(row[0])

    def item_checkpoint_history() -> list[str]:
        with sqlite3.connect(store.path) as connection:
            rows = connection.execute(
                "SELECT checkpoint_json FROM provisioning_item_checkpoints WHERE job_id=?",
                (job_id,),
            ).fetchall()
            return [str(row[0]) for row in rows]

    # Step 1: enqueue -> queued; job transitions to running inside claim (no event).
    assert orchestrator.run_once("worker-1", now=_NOW) is True
    assert job_state() == "running"
    assert item_state() == "queued"
    _, checkpoint = work_item_state()
    assert '"enqueue_started":true' in checkpoint
    assert any('"enqueue_started":true' in value for value in item_checkpoint_history())
    assert audit_counts() == baseline  # +0: claim and save_item_checkpoint write no events

    # Step 2: observe -> completed; item terminalizes (ITEM_UPDATED +1).
    assert orchestrator.run_once("worker-1", now=_NOW + timedelta(seconds=1)) is True
    assert item_state() == "completed"
    assert job_state() == "running"
    audits, outbox = audit_counts()
    assert (audits, outbox) == (baseline[0] + 1, baseline[1] + 1)

    # Step 3: no active item -> job/work terminalize (UPDATED +1).
    assert orchestrator.run_once("worker-1", now=_NOW + timedelta(seconds=2)) is True
    assert job_state() == "completed"
    assert work_item_state()[0] == "completed"
    audits, outbox = audit_counts()
    assert (audits, outbox) == (baseline[0] + 2, baseline[1] + 2)
    assert manager.enqueue_calls == 1
    assert manager.observe_calls == 1


# ---------------------------------------------------------------------------
# g5 outbox dispatches real messages with owner resolution
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_g5_outbox_dispatches_owned_message(tmp_path: Path) -> None:
    store = _initialized_store(tmp_path)
    _seed_job_row(store)
    outbox_id = _seed_g5_outbox(store)

    class RecordingBus:
        def __init__(self) -> None:
            self.events: list[object] = []

        async def publish(self, event: object) -> None:
            self.events.append(event)

    orchestration_repository = SQLiteOrchestrationRepository(store)
    bus = RecordingBus()
    runtime = OrchestrationRuntime(
        OperationOrchestrator(orchestration_repository, {}),
        orchestration_repository,
        bus,
        worker_id="test-worker",
        owner_for_uri=orchestration_repository.job_owner_for_uri,
    )

    dispatched = await runtime.dispatch_outbox_once()

    assert dispatched == 1
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT status FROM outbox WHERE outbox_id=?", (outbox_id,)
        ).fetchone()
        assert str(row[0]) == "delivered"
    assert len(bus.events) == 1
    assert isinstance(bus.events[0], ResourceUpdated)
    assert bus.events[0].uri == f"comfyui://jobs/{_job_id()}"


def test_create_repository_bundle_fresh_keeps_lightweight_bundle(tmp_path: Path) -> None:
    repositories = create_repository_bundle(tmp_path)
    assert repositories.store is None
    assert not _database(tmp_path).exists()
