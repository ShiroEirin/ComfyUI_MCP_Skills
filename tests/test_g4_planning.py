"""G4 execution planning and canonical Job identity contracts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from comfyui_mcp_skills.adapters.mcp.tooling import job_dict
from comfyui_mcp_skills.application.catalog import WorkflowCatalog
from comfyui_mcp_skills.application.execution import ExecutionService
from comfyui_mcp_skills.application.planning import ExecutionPlanningService
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.g3_migration import (
    build_g3_import_plan,
    cutover_g3_import_plan,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_assets import SQLiteAssetRepository
from comfyui_mcp_skills.infrastructure.persistence.sqlite_runs import SQLiteRunRepository
from comfyui_mcp_skills.infrastructure.persistence.sqlite_workflows import SQLiteWorkflowRepository


def _project(root: Path) -> SQLiteControlPlaneStore:
    directory = root / "data" / "local" / "portrait"
    directory.mkdir(parents=True)
    (directory / "schema.json").write_text(
        '{"description":"Portrait","enabled":true,"parameters":{}}', encoding="utf-8"
    )
    (directory / "workflow.json").write_text("{}", encoding="utf-8")
    store = SQLiteControlPlaneStore((root / "data" / "control-plane.sqlite3").resolve())
    store.initialize()
    (root / "config.json").write_text(
        '{"servers":[{"id":"local","name":"Local","url":"http://127.0.0.1:8188"}]}',
        encoding="utf-8",
    )
    cutover_g3_import_plan(build_g3_import_plan(root), store)
    return store


def test_planning_materializes_immutable_plan_and_canonical_job_atomically(tmp_path: Path) -> None:
    store = _project(tmp_path)
    service = ExecutionPlanningService(store, SQLiteWorkflowRepository(store))

    identity = service.materialize(
        server_id="local",
        workflow_id="portrait",
        owner_id="principal",
        arguments={"seed": 7},
        client_id="client-1",
    )

    assert identity.job_id.startswith("job_")
    assert identity.plan_id.startswith("plan_")
    assert identity.revision_id.startswith("revision_")
    assert identity.deployment_id.startswith("deployment_")
    assert identity.plan_digest
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT plan_id, revision_id, deployment_id, legacy_migrated, execution_origin "
            "FROM jobs WHERE job_id = ?",
            (identity.job_id,),
        ).fetchone() == (
            identity.plan_id,
            identity.revision_id,
            identity.deployment_id,
            0,
            "planned",
        )
        assert connection.execute(
            "SELECT upstream_prompt_id, upstream_job_id, client_id, submission_state "
            "FROM execution_attempts WHERE job_id = ?",
            (identity.job_id,),
        ).fetchone() == (None, None, "client-1", "submission_unknown")

    service.finalize_submission(identity, upstream_prompt_id="prompt-1")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE jobs SET status = 'completed' WHERE job_id = ?", (identity.job_id,)
        )
        connection.commit()
    service.finalize_submission(identity, upstream_prompt_id="prompt-1")
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (identity.job_id,)
        ).fetchone() == ("completed",)

    other_owner = service.materialize(
        server_id="local",
        workflow_id="portrait",
        owner_id="other-principal",
        arguments={"seed": 7},
        resolved_inputs={"seed": 7},
        client_id="client-2",
    )
    assert other_owner.plan_id != identity.plan_id
    resolved_plan = service.materialize(
        server_id="local",
        workflow_id="portrait",
        owner_id="principal",
        arguments={"seed": 7},
        resolved_inputs={"seed": 7, "image": "input/resolved.png"},
        client_id="client-3",
    )
    assert resolved_plan.plan_id != identity.plan_id
    with sqlite3.connect(store.path) as connection:
        snapshot = json.loads(
            connection.execute(
                "SELECT resolved_inputs_json FROM execution_plans WHERE plan_id = ?",
                (resolved_plan.plan_id,),
            ).fetchone()[0]
        )
    assert snapshot == {
        "arguments": {"seed": 7},
        "resolved_inputs": {"image": "input/resolved.png", "seed": 7},
    }


def test_planning_reuses_digest_and_rolls_back_plan_with_job_on_failure(tmp_path: Path) -> None:
    store = _project(tmp_path)
    service = ExecutionPlanningService(store, SQLiteWorkflowRepository(store))
    first = service.materialize(
        server_id="local",
        workflow_id="portrait",
        owner_id="principal",
        arguments={"seed": 7},
        client_id="client-1",
    )
    repeated = service.materialize(
        server_id="local",
        workflow_id="portrait",
        owner_id="principal",
        arguments={"seed": 7},
        client_id="client-1",
    )
    assert repeated == first

    try:
        service.materialize(
            server_id="local",
            workflow_id="portrait",
            owner_id="other",
            arguments={"seed": 8},
            client_id="client-2",
            failure_injector=lambda phase: (
                (_ for _ in ()).throw(RuntimeError("injected")) if phase == "after_plan" else None
            ),
        )
    except RuntimeError as exc:
        assert str(exc) == "injected"
    else:
        raise AssertionError("expected injected failure")

    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM execution_plans").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM jobs").fetchone() == (1,)


class _Gateway:
    def queue_prompt(self, workflow: dict[str, object], **kwargs: object) -> dict[str, str]:
        return {"prompt_id": "prompt-g4", "job_id": "upstream-job-g4"}


class _EmptyGateway:
    def queue_prompt(self, workflow: dict[str, object], **kwargs: object) -> dict[str, str]:
        return {}


class _JobOnlyGateway:
    def queue_prompt(self, workflow: dict[str, object], **kwargs: object) -> dict[str, str]:
        return {"job_id": "upstream-only"}


def _execution_service(
    tmp_path: Path, gateway: object
) -> tuple[ExecutionService, SQLiteRunRepository, SQLiteControlPlaneStore]:
    store = _project(tmp_path)
    workflows = SQLiteWorkflowRepository(store)
    runs = SQLiteRunRepository(store)
    service = ExecutionService(
        WorkflowCatalog(workflows),
        ServerRegistry(tmp_path),
        runs,
        SQLiteAssetRepository(store),
        lambda _config: gateway,  # type: ignore[arg-type,return-value]
        planning=ExecutionPlanningService(store, workflows),
    )
    return service, runs, store


def test_empty_upstream_identity_marks_canonical_job_unknown(tmp_path: Path) -> None:
    service, _runs, store = _execution_service(tmp_path, _EmptyGateway())

    with pytest.raises(Exception, match="submission outcome is unknown"):
        service.submit("local", "portrait", {}, owner_id="principal")

    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT status FROM jobs").fetchone() == ("submission_unknown",)


def test_upstream_job_identity_and_public_job_contract_are_independent(tmp_path: Path) -> None:
    service, runs, store = _execution_service(tmp_path, _JobOnlyGateway())

    job = service.submit("local", "portrait", {}, owner_id="principal")

    assert job.prompt_id == ""
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT upstream_prompt_id, upstream_job_id FROM execution_attempts"
        ).fetchone() == (None, "upstream-only")
    public = job_dict(job)
    assert public["plan_digest"] == job.plan_digest
    assert public["job_uri"] == f"comfyui://jobs/{job.job_id}"
    assert "client_id" not in public and "idempotency_key" not in public

    runs.save(replace(job, status="completed"))
    runs.save(replace(job, status="error", error="late"))
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone() == ("completed",)


def test_dynamic_submit_returns_non_null_g4_identity(tmp_path: Path) -> None:
    store = _project(tmp_path)
    workflows = SQLiteWorkflowRepository(store)
    runs = SQLiteRunRepository(store)
    service = ExecutionService(
        WorkflowCatalog(workflows),
        ServerRegistry(tmp_path),
        runs,
        SQLiteAssetRepository(store),
        lambda _config: _Gateway(),  # type: ignore[arg-type,return-value]
        planning=ExecutionPlanningService(store, workflows),
    )

    job = service.submit("local", "portrait", {}, owner_id="principal")

    assert job.prompt_id == "prompt-g4"
    assert job.job_id.startswith("job_")
    assert job.plan_id.startswith("plan_")
    assert job.revision_id.startswith("revision_")
    assert job.deployment_id.startswith("deployment_")
    persisted = runs.get("local", "prompt-g4")
    assert persisted is not None
    assert job.plan_digest
    assert (persisted.job_id, persisted.plan_id) == (job.job_id, job.plan_id)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT upstream_prompt_id, upstream_job_id, submission_state "
            "FROM execution_attempts WHERE job_id = ?",
            (job.job_id,),
        ).fetchone() == ("prompt-g4", "upstream-job-g4", "submitted")


def test_planning_rejects_oversized_input_snapshot(tmp_path: Path) -> None:
    store = _project(tmp_path)
    service = ExecutionPlanningService(store, SQLiteWorkflowRepository(store))

    with pytest.raises(Exception, match="input snapshot exceeds"):
        service.materialize(
            server_id="local",
            workflow_id="portrait",
            owner_id="principal",
            arguments={"prompt": "x" * (1024 * 1024)},
            client_id="client-large",
        )
