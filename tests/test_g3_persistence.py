"""G3 Workflow/Revision/Deployment persistence contracts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.g3_migration import (
    build_g3_import_plan,
    cutover_g3_import_plan,
)
from comfyui_mcp_skills.infrastructure.persistence.repository_factory import (
    StoreRoutingError,
    create_repository_bundle,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_workflows import (
    SQLiteWorkflowRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.workflows import FileWorkflowRepository

_SWITCH_GROUP = ("workflow", "revision", "deployment")


def _write_workflow(
    root: Path,
    *,
    server_id: str = "local",
    workflow_id: str = "portrait",
    description: str = "Portrait",
    prompt: str = "cat",
) -> None:
    directory = root / "data" / server_id / workflow_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "schema.json").write_text(
        json.dumps(
            {
                "description": description,
                "enabled": True,
                "parameters": {
                    "prompt": {
                        "type": "string",
                        "node_id": "1",
                        "field": "text",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (directory / "workflow.json").write_text(
        json.dumps({"1": {"class_type": "Text", "inputs": {"text": prompt}}}),
        encoding="utf-8",
    )


def _store(root: Path) -> SQLiteControlPlaneStore:
    store = SQLiteControlPlaneStore((root / "data" / "control-plane.sqlite3").resolve())
    store.initialize()
    return store


def _insert_second_deployment(store: SQLiteControlPlaneStore) -> tuple[str, str]:
    revision_id = "revision_" + "b" * 64
    deployment_id = "deployment_" + "c" * 64
    graph = {"1": {"class_type": "Text", "inputs": {"text": "dog"}}}
    schema = {
        "description": "Updated portrait",
        "enabled": True,
        "parameters": {"prompt": {"type": "string", "node_id": "1", "field": "text"}},
    }
    graph_json = json.dumps(graph, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    schema_json = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{graph_json}\n{schema_json}".encode()).hexdigest()
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO workflow_revisions(
                revision_id, workflow_id, graph_json, parameter_schema_json,
                dependency_contract_json, content_digest, created_at
            ) VALUES (?, 'portrait', ?, ?, '{}', ?, '2026-07-30T00:00:00+00:00')
            """,
            (revision_id, graph_json, schema_json, digest),
        )
        connection.execute(
            """
            INSERT INTO workflow_deployments(
                deployment_id, workflow_id, revision_id, server_id, enabled,
                validation_status, published, created_at
            ) VALUES (?, 'portrait', ?, 'local', 1, 'valid', 0,
                      '2026-07-30T00:00:00+00:00')
            """,
            (deployment_id, revision_id),
        )
    return revision_id, deployment_id


def test_g3_plan_and_cutover_are_deterministic_and_idempotent(tmp_path: Path) -> None:
    _write_workflow(tmp_path)
    _write_workflow(
        tmp_path,
        server_id="remote",
        workflow_id="upscale",
        description="Upscale",
    )
    first = build_g3_import_plan(tmp_path)
    second = build_g3_import_plan(tmp_path)
    store = _store(tmp_path)

    result = cutover_g3_import_plan(first, store)
    repeated = cutover_g3_import_plan(second, store)

    assert first == second
    assert result.outcome == "switched"
    assert repeated.outcome == "already_switched"
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM workflows").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM workflow_revisions").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM workflow_deployments").fetchone() == (2,)
        assert connection.execute(
            """
            SELECT aggregate_kind, version, status, checksum
            FROM store_migrations
            WHERE aggregate_kind IN ('workflow', 'revision', 'deployment')
            ORDER BY aggregate_kind
            """
        ).fetchall() == [
            ("deployment", 1, "switched", first.checksum),
            ("revision", 1, "switched", first.checksum),
            ("workflow", 1, "switched", first.checksum),
        ]


def test_sqlite_repository_reads_only_published_and_publishes_atomically(
    tmp_path: Path,
) -> None:
    _write_workflow(tmp_path)
    store = _store(tmp_path)
    cutover_g3_import_plan(build_g3_import_plan(tmp_path), store)
    repository = SQLiteWorkflowRepository(store)
    first = repository.get("local", "portrait")
    assert first is not None
    assert first.description == "Portrait"
    assert first.graph["1"]["inputs"]["text"] == "cat"

    revision_id, deployment_id = _insert_second_deployment(store)
    assert len(repository.list()) == 1
    assert repository.get("local", "portrait") == first
    assert repository.list_revisions("portrait")[-1]["revision_id"] == revision_id

    repository.publish(deployment_id)

    current = repository.get("local", "portrait")
    assert current is not None
    assert current.description == "Updated portrait"
    assert current.graph["1"]["inputs"]["text"] == "dog"
    assert repository.describe("portrait", "local") == {
        "server_id": "local",
        "workflow_id": "portrait",
        "description": "Updated portrait",
        "revision_id": revision_id,
        "deployment_id": deployment_id,
        "content_digest": repository.list_revisions("portrait")[-1]["content_digest"],
        "validation_status": "valid",
        "published": True,
    }
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            """
            SELECT published FROM workflow_deployments
            WHERE workflow_id = 'portrait' AND server_id = 'local'
            ORDER BY deployment_id
            """
        ).fetchall() == [(1,), (0,)]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE workflow_revisions SET graph_json = '{}' WHERE revision_id = ?",
                (revision_id,),
            )

    with pytest.raises(LookupError):
        repository.publish("deployment_" + "f" * 64)
    assert repository.get("local", "portrait") == current


def test_g3_failure_and_conflict_leave_no_partial_switch(tmp_path: Path) -> None:
    _write_workflow(tmp_path)
    plan = build_g3_import_plan(tmp_path)
    store = _store(tmp_path)

    def fail_after_import(phase: str) -> None:
        if phase == "after_import":
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        cutover_g3_import_plan(plan, store, failure_injector=fail_after_import)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM workflows").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM store_migrations").fetchone() == (0,)
        connection.execute(
            "INSERT INTO workflows(workflow_id, created_at) VALUES ('portrait', 'conflict')"
        )

    with pytest.raises(RuntimeError, match="conflicts"):
        cutover_g3_import_plan(plan, store)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_revisions").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM workflow_deployments").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM store_migrations").fetchone() == (0,)


def test_repository_bundle_fails_closed_on_partial_g3_and_routes_complete_group(
    tmp_path: Path,
) -> None:
    _write_workflow(tmp_path)
    store = _store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            INSERT INTO store_migrations(
                aggregate_kind, version, status, checksum, switched_at
            ) VALUES ('workflow', 1, 'switched', ?, '2026-07-30T00:00:00+00:00')
            """,
            ("a" * 64,),
        )

    with pytest.raises(StoreRoutingError, match="partial workflow"):
        create_repository_bundle(tmp_path)

    other = tmp_path / "complete"
    _write_workflow(other)
    other_store = _store(other)
    cutover_g3_import_plan(build_g3_import_plan(other), other_store)
    repositories = create_repository_bundle(other)

    assert isinstance(repositories.workflows, SQLiteWorkflowRepository)
    assert repositories.workflow_store == "sqlite"
    assert repositories.workflows.get("local", "portrait") is not None


def test_repository_bundle_keeps_file_workflows_before_complete_switch(tmp_path: Path) -> None:
    _write_workflow(tmp_path)

    repositories = create_repository_bundle(tmp_path)

    assert isinstance(repositories.workflows, FileWorkflowRepository)
    assert repositories.workflow_store == "file"
    assert repositories.workflows.get("local", "portrait") is not None


def test_stale_file_workflow_repository_is_fenced_after_g3_cutover(tmp_path: Path) -> None:
    _write_workflow(tmp_path)
    stale = FileWorkflowRepository(tmp_path)
    assert stale.get("local", "portrait") is not None
    store = _store(tmp_path)
    cutover_g3_import_plan(build_g3_import_plan(tmp_path), store)

    with pytest.raises(RuntimeError, match="fenced"):
        stale.get("local", "portrait")
    with pytest.raises(RuntimeError, match="fenced"):
        stale.list()
