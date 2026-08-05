"""Durable Phase O migration and provisioning repository contracts."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from comfyui_mcp_skills.adapters.mcp.admin_control import AdminOutboxRuntime
from comfyui_mcp_skills.application.config_bundles import ConfigBundleService
from comfyui_mcp_skills.application.provisioning import (
    INSTALL_CONFIRMATION,
    DependencyProvisioningService,
)
from comfyui_mcp_skills.application.server_control import ServerControlService
from comfyui_mcp_skills.domain.errors import WorkflowChangeNotFound
from comfyui_mcp_skills.infrastructure.persistence import control_plane as control_plane_module
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.sqlite_provisioning import (
    SQLiteProvisioningRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_workflows import (
    SQLiteWorkflowRepository,
    _revision_digest,
)
from comfyui_mcp_skills.infrastructure.persistence.workflow_changes import (
    SQLiteWorkflowChangeRepository,
)

_NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
_OWNER = "owner-a"


def _repository(path: Path) -> SQLiteProvisioningRepository:
    store = SQLiteControlPlaneStore(path.resolve())
    store.initialize()
    return SQLiteProvisioningRepository(store)


def _add_server(repository: SQLiteProvisioningRepository) -> None:
    service = ServerControlService(repository, clock=lambda: _NOW)
    plan = service.plan(
        "upsert",
        "local",
        _OWNER,
        {"url": "http://127.0.0.1:8188", "expected_revision": 0},
    )
    assert service.commit(plan["plan_id"], plan["plan_digest"], _OWNER)["server_id"] == "local"


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


def test_phase_o_migration_upgrades_v8_without_rewriting_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteControlPlaneStore((tmp_path / "control-plane.sqlite3").resolve())
    migrations = control_plane_module._MIGRATIONS
    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", migrations[:8])
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        prior = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()

    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", migrations[:9])
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone() == (9,)
        assert (
            connection.execute(
                "SELECT version, name, checksum FROM schema_migrations "
                "WHERE version < 9 ORDER BY version"
            ).fetchall()
            == prior
        )
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
    assert {
        "managed_servers",
        "config_bundles",
        "dependency_plans",
        "approvals",
        "provisioning_jobs",
        "provisioning_install_items",
        "provisioning_item_checkpoints",
        "phase_o_audit_events",
        "phase_o_outbox",
    } <= tables


def test_server_plans_are_owner_digest_bound_and_immutable(tmp_path: Path) -> None:
    database = (tmp_path / "control-plane.sqlite3").resolve()
    repository = _repository(database)
    service = ServerControlService(repository, clock=lambda: _NOW)
    plan = service.plan(
        "upsert",
        "local",
        _OWNER,
        {"url": "http://127.0.0.1:8188", "expected_revision": 0},
    )

    with pytest.raises(ValueError, match="owner|digest|revision"):
        service.commit(plan["plan_id"], plan["plan_digest"], "owner-b")
    with (
        sqlite3.connect(database) as connection,
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
    ):
        connection.execute(
            "UPDATE server_change_plans SET server_id='other' WHERE plan_id=?",
            (plan["plan_id"],),
        )

    first = service.commit(plan["plan_id"], plan["plan_digest"], _OWNER)
    repeated = service.commit(plan["plan_id"], plan["plan_digest"], _OWNER)
    assert repeated == first


def test_install_commit_is_idempotent_and_creates_durable_work(tmp_path: Path) -> None:
    database = (tmp_path / "control-plane.sqlite3").resolve()
    repository = _repository(database)
    _add_server(repository)
    service = DependencyProvisioningService(
        repository,
        catalog=_catalog(),
        clock=lambda: _NOW,
    )
    plan = service.plan(
        "local",
        _OWNER,
        [{"kind": "node", "name": "KnownNode"}],
    )
    approval_plan = service.plan_approval(plan["approval_id"], "approved", _OWNER)
    service.commit_approval(approval_plan["approval_plan_id"], approval_plan["plan_digest"], _OWNER)

    first = service.commit(
        plan["plan_id"],
        plan["plan_digest"],
        plan["approval_id"],
        _OWNER,
        "install-request-1",
        INSTALL_CONFIRMATION,
    )
    repeated = service.commit(
        plan["plan_id"],
        plan["plan_digest"],
        plan["approval_id"],
        _OWNER,
        "install-request-1",
        INSTALL_CONFIRMATION,
    )
    assert repeated == first
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM provisioning_jobs").fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM operation_work_items WHERE work_type='provisioning.execute'"
        ).fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM approval_uses").fetchone() == (1,)


def test_config_bundle_import_applies_and_round_trips_public_content(tmp_path: Path) -> None:
    repository = _repository((tmp_path / "control-plane.sqlite3").resolve())
    service = ConfigBundleService(repository, clock=lambda: _NOW)
    bundle = {
        "format_version": 1,
        "revision": "0",
        "servers": [
            {
                "server_id": "local",
                "endpoint_url": "http://127.0.0.1:8188",
                "enabled": True,
                "is_default": True,
                "secret_refs": {"api_key": "COMFY_API_KEY"},
            }
        ],
        "workflows": [],
        "default_server": "local",
        "bundle_digest": "",
        "resource_uri": "comfyui://config/bundles/0",
        "created_at": _NOW.isoformat(),
    }
    bundle.pop("bundle_digest")
    plan = service.plan_import(bundle, "0", _OWNER)

    committed = service.commit_import(plan["plan_id"], plan["plan_digest"], _OWNER)

    assert committed["revision"] == "1"
    assert committed["servers"][0]["secret_refs"] == {"api_key": "COMFY_API_KEY"}
    assert repository.get_server("local", _OWNER)["revision"] == 1
    assert service.export(_OWNER) == committed


def test_cancel_commit_preserves_cancelled_aggregate_status(tmp_path: Path) -> None:
    repository = _repository((tmp_path / "control-plane.sqlite3").resolve())
    _add_server(repository)
    service = DependencyProvisioningService(repository, catalog=_catalog(), clock=lambda: _NOW)
    plan = service.plan("local", _OWNER, [{"kind": "node", "name": "KnownNode"}])
    approval_plan = service.plan_approval(plan["approval_id"], "approved", _OWNER)
    service.commit_approval(approval_plan["approval_plan_id"], approval_plan["plan_digest"], _OWNER)
    job = service.commit(
        plan["plan_id"],
        plan["plan_digest"],
        plan["approval_id"],
        _OWNER,
        "install-request-cancel",
        INSTALL_CONFIRMATION,
    )
    cancel = service.plan_cancel(job["job_id"], _OWNER)

    first = service.commit_cancel(cancel["cancel_plan_id"], cancel["plan_digest"], _OWNER)
    repeated = service.commit_cancel(cancel["cancel_plan_id"], cancel["plan_digest"], _OWNER)

    assert first["status"] == "cancelled"
    assert repeated["status"] == "cancelled"


class _FailingBus:
    async def publish(self, _event: object) -> None:
        raise RuntimeError("subscription unavailable")


@pytest.mark.anyio
async def test_failed_phase_o_publish_remains_pending_for_retry(tmp_path: Path) -> None:
    database = (tmp_path / "control-plane.sqlite3").resolve()
    repository = _repository(database)
    _add_server(repository)
    service = DependencyProvisioningService(repository, catalog=_catalog(), clock=lambda: _NOW)
    plan = service.plan("local", _OWNER, [{"kind": "node", "name": "KnownNode"}])
    runtime = AdminOutboxRuntime(
        repository,
        _FailingBus(),
        owner_id=_OWNER,
    )

    assert await runtime.dispatch_once() == 0
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status FROM phase_o_outbox WHERE payload_json LIKE ?",
            (f"%{plan['approval_id']}%",),
        ).fetchone() == ("pending",)


def test_owner_workflow_bindings_survive_another_owner_publish(tmp_path: Path) -> None:
    store = SQLiteControlPlaneStore((tmp_path / "control-plane.sqlite3").resolve())
    store.initialize()
    provisioning = SQLiteProvisioningRepository(store)
    control = ServerControlService(provisioning, clock=lambda: _NOW)
    for owner in ("owner-a", "owner-b"):
        plan = control.plan(
            "upsert",
            "local",
            owner,
            {"url": "http://127.0.0.1:8188", "expected_revision": 0},
        )
        control.commit(plan["plan_id"], plan["plan_digest"], owner)

    deployments: dict[str, str] = {}
    revisions: dict[str, str] = {}
    for owner, text in (("owner-a", "alpha"), ("owner-b", "beta")):
        workflows = SQLiteWorkflowRepository(store, owner_id=owner)
        graph = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": text}}}
        schema = {
            "type": "object",
            "properties": {"prompt": {"type": "string", "x-node-id": "1", "x-input": "text"}},
            "required": ["prompt"],
        }
        dependencies: dict[str, object] = {}
        created = workflows.create_revision(
            workflow_id="portrait",
            server_id="local",
            graph=graph,
            parameter_schema=schema,
            dependency_contract=dependencies,
            content_digest=_revision_digest(graph, schema, dependencies),
        )
        workflows.publish(created["deployment_id"])
        deployments[owner] = created["deployment_id"]
        revisions[owner] = created["revision_id"]

    owner_a = SQLiteWorkflowRepository(store, owner_id="owner-a")
    owner_b = SQLiteWorkflowRepository(store, owner_id="owner-b")
    assert owner_a.describe("portrait", "local")["deployment_id"] == deployments["owner-a"]
    assert owner_b.describe("portrait", "local")["deployment_id"] == deployments["owner-b"]
    assert owner_a.get("local", "portrait").graph["1"]["inputs"]["text"] == "alpha"
    assert owner_b.get("local", "portrait").graph["1"]["inputs"]["text"] == "beta"
    assert [item["revision_id"] for item in owner_a.list_revisions("portrait")] == [
        revisions["owner-a"]
    ]
    with pytest.raises(LookupError, match="not found"):
        owner_a.get_revision(revisions["owner-b"])
    with pytest.raises(WorkflowChangeNotFound, match="not found"):
        SQLiteWorkflowChangeRepository(store).rollback(
            "portrait",
            "local",
            revisions["owner-b"],
            "owner-a-foreign-rollback",
            "owner-a",
        )
    request = {"owner": "owner-a"}
    dynamic = SQLiteWorkflowRepository(store, owner_provider=lambda: request["owner"])
    assert dynamic.get("local", "portrait").graph["1"]["inputs"]["text"] == "alpha"
    request["owner"] = "owner-b"
    assert dynamic.get("local", "portrait").graph["1"]["inputs"]["text"] == "beta"
