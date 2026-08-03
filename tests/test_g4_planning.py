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
from comfyui_mcp_skills.domain.control_plane import derive_legacy_job_id
from comfyui_mcp_skills.domain.models import Asset, Job
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.g3_migration import (
    build_g3_import_plan,
    cutover_g3_import_plan,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_asset_library import (
    SQLiteAssetLibraryRepository,
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


def _lineage_project(root: Path, *, consumer_class: str = "LoadImage") -> SQLiteControlPlaneStore:
    directory = root / "data" / "local" / "lineage"
    directory.mkdir(parents=True)
    graph = {
        "7": {
            "class_type": consumer_class,
            "inputs": {"image": "placeholder.png"},
        }
    }
    schema = {
        "description": "Lineage input",
        "enabled": True,
        "parameters": {
            "source": {
                "type": "image",
                "required": True,
                "node_id": "7",
                "field": "image",
            }
        },
    }
    (directory / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    (directory / "workflow.json").write_text(json.dumps(graph), encoding="utf-8")
    store = SQLiteControlPlaneStore((root / "data" / "control-plane.sqlite3").resolve())
    store.initialize()
    (root / "config.json").write_text(
        '{"servers":[{"id":"local","name":"Local","url":"http://127.0.0.1:8188"}]}',
        encoding="utf-8",
    )
    cutover_g3_import_plan(build_g3_import_plan(root), store)
    return store


def test_planning_persists_owner_bound_graph_derived_asset_input_atomically(
    tmp_path: Path,
) -> None:
    store = _lineage_project(tmp_path)
    assets = SQLiteAssetRepository(store)
    source = Asset(
        asset_id="asset_" + "a" * 64,
        server_id="local",
        comfyui_ref="inputs/source.png",
        name="source.png",
        subfolder="inputs",
        media_type="image",
        mime_type="image/png",
        size_bytes=3,
        sha256="b" * 64,
        owner_id="owner-a",
    )
    assets.save(source)
    service = ExecutionPlanningService(store, SQLiteWorkflowRepository(store))

    identity = service.materialize(
        server_id="local",
        workflow_id="lineage",
        owner_id="owner-a",
        arguments={"source": source.asset_id},
        resolved_inputs={"source": source.comfyui_ref},
        client_id="lineage-client",
    )

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            """SELECT owner_id,revision_id,deployment_id,parameter_name,
                      consumer_node_id,consumer_input_name,consumer_class,source_kind,
                      asset_id,artifact_id,source_job_id,reuse_strategy,source_digest
               FROM execution_plan_inputs WHERE plan_id=?""",
            (identity.plan_id,),
        ).fetchone() == (
            "owner-a",
            identity.revision_id,
            identity.deployment_id,
            "source",
            "7",
            "image",
            "LoadImage",
            "asset",
            source.asset_id,
            None,
            None,
            "direct",
            source.sha256,
        )
        assert connection.execute(
            "SELECT plan_id FROM execution_plan_inputs WHERE owner_id=? AND asset_id=?",
            ("owner-a", source.asset_id),
        ).fetchall() == [(identity.plan_id,)]

    with pytest.raises(LookupError, match="owned Asset input was not found"):
        service.materialize(
            server_id="local",
            workflow_id="lineage",
            owner_id="owner-b",
            arguments={"source": source.asset_id},
            resolved_inputs={"source": source.comfyui_ref},
            client_id="wrong-owner-client",
        )

    second = replace(source, asset_id="asset_" + "c" * 64, sha256="d" * 64)
    assets.save(second)
    with pytest.raises(RuntimeError, match="injected"):
        service.materialize(
            server_id="local",
            workflow_id="lineage",
            owner_id="owner-a",
            arguments={"source": second.asset_id},
            resolved_inputs={"source": second.comfyui_ref},
            client_id="rollback-lineage-client",
            failure_injector=lambda phase: (
                (_ for _ in ()).throw(RuntimeError("injected"))
                if phase == "after_plan_inputs"
                else None
            ),
        )
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM execution_plan_inputs").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM jobs").fetchone() == (1,)


@pytest.mark.parametrize("reference_kind", ["canonical", "legacy"])
def test_planning_resolves_owned_artifact_references_to_graph_target(
    tmp_path: Path, reference_kind: str
) -> None:
    store = _lineage_project(tmp_path, consumer_class="LoadImageOutput")
    runs = SQLiteRunRepository(store)
    source_job = Job(
        prompt_id="source-prompt",
        server_id="local",
        workflow_id="producer",
        status="submitted",
        owner_id="owner-a",
        client_id="source-client",
        job_id=derive_legacy_job_id("local", "source-prompt"),
    )
    runs.save(source_job)
    legacy_uri = "comfyui://outputs/local/source-prompt/0"
    observation = {
        "upstream_node_id": "9",
        "output_key": "images",
        "upstream_output_index": 0,
        "legacy_index": 0,
        "filename": "source.png",
        "subfolder": "renders",
        "type": "output",
        "storage_type": "output",
        "media_type": "image",
        "mime_type": "image/png",
        "legacy_uri": legacy_uri,
    }
    completed_source = replace(source_job, status="completed", outputs=(observation,))
    artifacts = SQLiteAssetLibraryRepository(store).terminalize(
        completed_source, completed_source.outputs
    )
    artifact = artifacts[0]
    reference = artifact.resource_uri if reference_kind == "canonical" else legacy_uri

    service = ExecutionPlanningService(store, SQLiteWorkflowRepository(store))
    identity = service.materialize(
        server_id="local",
        workflow_id="lineage",
        owner_id="owner-a",
        arguments={"source": reference},
        resolved_inputs={"source": "renders/source.png [output]"},
        client_id=f"artifact-{reference_kind}",
    )
    with pytest.raises(LookupError, match="owned Artifact input was not found"):
        service.materialize(
            server_id="local",
            workflow_id="lineage",
            owner_id="owner-b",
            arguments={"source": reference},
            resolved_inputs={"source": "renders/source.png [output]"},
            client_id=f"wrong-owner-{reference_kind}",
        )

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            """SELECT owner_id,revision_id,deployment_id,parameter_name,
                      consumer_node_id,consumer_input_name,consumer_class,source_kind,
                      asset_id,artifact_id,source_job_id,reuse_strategy,source_digest
               FROM execution_plan_inputs WHERE plan_id=?""",
            (identity.plan_id,),
        ).fetchone() == (
            "owner-a",
            identity.revision_id,
            identity.deployment_id,
            "source",
            "7",
            "image",
            "LoadImageOutput",
            "artifact",
            None,
            artifact.artifact_id,
            source_job.job_id,
            "direct",
            artifact.digest,
        )
        assert connection.execute(
            "SELECT plan_id FROM execution_plan_inputs WHERE owner_id=? AND artifact_id=?",
            ("owner-a", artifact.artifact_id),
        ).fetchall() == [(identity.plan_id,)]
