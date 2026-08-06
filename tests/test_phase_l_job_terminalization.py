"""Phase L atomic Job terminalization persistence contracts."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from comfyui_mcp_skills.domain.control_plane import (
    canonical_resource_uri,
    derive_legacy_artifact_id,
    derive_legacy_job_id,
)
from comfyui_mcp_skills.domain.models import Job
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.sqlite_asset_library import (
    SQLiteAssetLibraryRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_runs import SQLiteRunRepository


def _submitted_job(store: SQLiteControlPlaneStore) -> Job:
    runs = SQLiteRunRepository(store)
    job = Job(
        prompt_id="prompt-terminal",
        server_id="local",
        workflow_id="producer",
        status="submitted",
        owner_id="owner-a",
        client_id="client-terminal",
        job_id=derive_legacy_job_id("local", "prompt-terminal"),
    )
    runs.save(job)
    return job


def _completed_job(job: Job) -> Job:
    artifact_id = derive_legacy_artifact_id(
        job.job_id,
        "9",
        "images",
        0,
        "render.png",
        "renders",
        "output",
    )
    output = {
        "artifact_id": artifact_id,
        "upstream_node_id": "9",
        "output_key": "images",
        "upstream_output_index": 0,
        "legacy_index": 0,
        "filename": "render.png",
        "subfolder": "renders",
        "type": "output",
        "storage_type": "output",
        "media_type": "image",
        "mime_type": "image/png",
        "resource_uri": canonical_resource_uri("artifact", artifact_id),
        "legacy_uri": "comfyui://outputs/local/prompt-terminal/0",
    }
    return replace(job, status="completed", outputs=(output,))


def test_terminalization_rolls_back_job_artifacts_aliases_and_completeness_together(
    tmp_path: Path,
) -> None:
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()
    repository = SQLiteAssetLibraryRepository(store)
    completed = _completed_job(_submitted_job(store))

    with pytest.raises(RuntimeError, match="persisted execution facts"):
        repository.terminalize(replace(completed, server_id="other"), completed.outputs)

    phases: list[str] = []

    def fail_after_artifacts(phase: str) -> None:
        phases.append(phase)
        if phase == "after_artifacts":
            raise RuntimeError("injected terminalization failure")

    with pytest.raises(RuntimeError, match="injected terminalization failure"):
        repository.terminalize(
            completed,
            completed.outputs,
            failure_injector=fail_after_artifacts,
        )

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT status,outputs_json FROM jobs WHERE job_id=?", (completed.job_id,)
        ).fetchone() == ("submitted", "[]")
        assert connection.execute(
            "SELECT count(*) FROM artifacts WHERE job_id=?", (completed.job_id,)
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM legacy_resource_aliases WHERE object_kind='output'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT status,artifact_count,output_snapshot_digest "
            "FROM job_artifact_collections WHERE job_id=?",
            (completed.job_id,),
        ).fetchone() == ("complete", 0, None)
    assert "after_job" in phases
    assert "after_artifacts" in phases

    artifacts = repository.terminalize(completed, completed.outputs)
    assert len(artifacts) == 1
    persisted = SQLiteRunRepository(store).get("local", "prompt-terminal")
    assert persisted is not None
    assert persisted.status == "completed"
    assert persisted.outputs == completed.outputs

    with sqlite3.connect(store.path) as connection:
        stored_outputs = connection.execute(
            "SELECT outputs_json FROM jobs WHERE job_id=?", (completed.job_id,)
        ).fetchone()[0]
        assert connection.execute(
            "SELECT submission_state,server_id,upstream_prompt_id FROM execution_attempts "
            "WHERE job_id=? AND attempt=1",
            (completed.job_id,),
        ).fetchone() == ("submitted", "local", "prompt-terminal")
        assert connection.execute(
            "SELECT status,artifact_count,output_snapshot_digest "
            "FROM job_artifact_collections WHERE job_id=?",
            (completed.job_id,),
        ).fetchone() == (
            "complete",
            1,
            hashlib.sha256(str(stored_outputs).encode("utf-8")).hexdigest(),
        )
        assert connection.execute(
            "SELECT count(*) FROM artifacts WHERE job_id=?", (completed.job_id,)
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM media_locations WHERE artifact_id=?",
            (artifacts[0].artifact_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT artifact_id FROM legacy_resource_aliases WHERE alias_uri=?",
            ("comfyui://outputs/local/prompt-terminal/0",),
        ).fetchone() == (artifacts[0].artifact_id,)

    assert repository.terminalize(completed, completed.outputs) == artifacts


def test_terminalization_backfills_completed_job_with_empty_snapshot(
    tmp_path: Path,
) -> None:
    """A reconciler-marked completed job (empty snapshot) can be collected once."""
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()
    repository = SQLiteAssetLibraryRepository(store)
    runs = SQLiteRunRepository(store)
    job = _submitted_job(store)
    # JobReconciler marks the job completed without persisting outputs.
    runs.save(replace(job, status="completed"))
    completed = _completed_job(job)

    artifacts = repository.terminalize(completed, completed.outputs)
    assert len(artifacts) == 1

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT status FROM jobs WHERE job_id=?", (job.job_id,)
        ).fetchone() == ("completed",)
        assert connection.execute(
            "SELECT count(*) FROM artifacts WHERE job_id=?", (job.job_id,)
        ).fetchone() == (1,)

    # Same snapshot again: idempotent (no duplicate artifacts).
    again = repository.terminalize(completed, completed.outputs)
    assert len(again) == 1
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM artifacts WHERE job_id=?", (job.job_id,)
        ).fetchone() == (1,)
    # A drifted snapshot after collection is still rejected.
    with pytest.raises(RuntimeError, match="conflicts with persisted output snapshot"):
        repository.terminalize(replace(completed, error="drifted"), completed.outputs)
