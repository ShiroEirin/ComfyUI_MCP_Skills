"""Production G1 file-to-SQLite migration contracts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

import pytest

from comfyui_mcp_skills.domain.control_plane import (
    derive_legacy_attempt_id,
    derive_legacy_job_id,
    derive_legacy_unknown_job_id,
)
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.file_migration import (
    FileMigrationRehearsal,
    ManifestDriftError,
    MigrationManifest,
    RehearsalFailure,
)


@pytest.fixture
def private_evidence_dir() -> Path:
    path = Path.home() / f".comfyui-mcp-g1-test-{uuid.uuid4().hex}"
    path.mkdir(mode=0o700)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _asset(asset_id: str = "asset_" + "a" * 32) -> dict[str, object]:
    return {
        "asset_id": asset_id,
        "server_id": "local",
        "comfyui_ref": "agent/assets/cat.png",
        "name": "cat.png",
        "subfolder": "agent/assets",
        "media_type": "image",
        "mime_type": "image/png",
        "size_bytes": 8,
        "sha256": "b" * 64,
        "owner_id": "owner-a",
        "created_at": "2026-07-30T00:00:00+00:00",
    }


def _mcp_job(
    *,
    prompt_id: str = "prompt-1",
    status: str = "completed",
    idempotency_key: str = "idem-1",
    outputs: list[object] | None = None,
) -> dict[str, object]:
    return {
        "prompt_id": prompt_id,
        "server_id": "local",
        "workflow_id": "portrait",
        "status": status,
        "outputs": outputs or [],
        "error": "",
        "idempotency_key": idempotency_key,
        "client_id": "client-a",
        "request_digest": "c" * 64,
        "owner_id": "owner-a",
    }


def _mcp_path(source: Path, collection: str, identity: str, *, owner: str = "") -> Path:
    server_hash = hashlib.sha256(b"local").hexdigest()
    if collection == "prompts":
        leaf = hashlib.sha256(identity.encode()).hexdigest()
    else:
        leaf = hashlib.sha256(f"{owner}\0{identity}".encode()).hexdigest()
    return source / "data" / "runs" / server_hash / collection / f"{leaf}.json"


def _frozen_plan(source: Path, private_evidence_dir: Path, aggregate: str):
    rehearsal = FileMigrationRehearsal(source)
    manifest = rehearsal.create_manifest()
    evidence = rehearsal.backup(manifest, private_evidence_dir)
    return rehearsal, rehearsal.build_g1_plan(Path(evidence.destination), aggregate)


def _store(tmp_path: Path) -> SQLiteControlPlaneStore:
    store = SQLiteControlPlaneStore((tmp_path / "control-plane.sqlite3").resolve())
    store.initialize()
    return store


def test_manifest_strict_load_rejects_duplicate_unknown_types_budgets_and_digest(
    tmp_path: Path,
) -> None:
    valid = MigrationManifest.create((), captured_at_ns=1).to_dict()
    path = tmp_path / "manifest.json"

    path.write_text('{"version":1,"version":1}', encoding="utf-8")
    with pytest.raises(ManifestDriftError, match="duplicate"):
        MigrationManifest.load(path)

    for mutation, message in (
        ({**valid, "unknown": True}, "unknown"),
        ({**valid, "version": True}, "version"),
        ({**valid, "entries": "not-a-list"}, "entries"),
        ({**valid, "digest": "0" * 64}, "digest"),
    ):
        _write_json(path, mutation)
        with pytest.raises(ManifestDriftError, match=message):
            MigrationManifest.load(path)

    oversized = {**valid, "entries": [{}] * 5_001}
    _write_json(path, oversized)
    with pytest.raises(ManifestDriftError, match="count"):
        MigrationManifest.load(path)

    _write_json(path, valid)
    assert MigrationManifest.load(path).to_dict() == valid


def test_g1_asset_cutover_imports_alias_and_switches_atomically(
    tmp_path: Path, private_evidence_dir: Path
) -> None:
    source = tmp_path / "source"
    record = _asset()
    _write_json(source / "data" / "assets" / f"{record['asset_id']}.json", record)
    rehearsal, plan = _frozen_plan(source, private_evidence_dir, "asset")
    store = _store(tmp_path)

    result = rehearsal.cutover_g1(plan, store)

    assert result.outcome == "switched"
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            """
            SELECT asset_id, owner_id, server_id, source_type, expires_at
            FROM assets
            """
        ).fetchall() == [(record["asset_id"], "owner-a", "local", "legacy_upload", None)]
        assert connection.execute(
            """
            SELECT alias_uri, canonical_uri, object_kind, asset_id
            FROM legacy_resource_aliases
            """
        ).fetchall() == [
            (
                f"comfyui://assets/local/{record['asset_id']}",
                f"comfyui://assets/{record['asset_id']}",
                "asset",
                record["asset_id"],
            )
        ]
        assert connection.execute(
            "SELECT aggregate_kind, version, status, checksum FROM store_migrations"
        ).fetchall() == [("asset", 1, "switched", plan.checksum)]


def test_g1_job_cutover_merges_prompt_and_idempotency_with_mtime_fallback(
    tmp_path: Path, private_evidence_dir: Path
) -> None:
    source = tmp_path / "source"
    record = _mcp_job()
    prompt_path = _mcp_path(source, "prompts", "prompt-1")
    idempotency_path = _mcp_path(source, "idempotency", "idem-1", owner="owner-a")
    _write_json(prompt_path, record)
    _write_json(idempotency_path, record)
    fallback_ns = 1_700_000_000_123_456_789
    prompt_path.touch()
    idempotency_path.touch()
    prompt_path.chmod(0o600)
    idempotency_path.chmod(0o600)
    # The fallback is evidence, not wall-clock cutover time.
    os.utime(prompt_path, ns=(fallback_ns, fallback_ns))
    os.utime(idempotency_path, ns=(fallback_ns + 1_000_000_000, fallback_ns + 1_000_000_000))
    rehearsal, plan = _frozen_plan(source, private_evidence_dir, "job")
    store = _store(tmp_path)
    job_id = derive_legacy_job_id("local", "prompt-1")

    result = rehearsal.cutover_g1(plan, store)

    assert result.outcome == "switched"
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            """
            SELECT job_id, workflow_id, owner_id, status, error,
                   created_at_source, legacy_migrated, execution_origin
            FROM jobs
            """
        ).fetchall() == [
            (
                job_id,
                "portrait",
                "owner-a",
                "completed",
                "",
                "legacy_file_mtime",
                1,
                "legacy_migrated",
            )
        ]
        assert connection.execute(
            """
            SELECT attempt_id, job_id, attempt, server_id, upstream_prompt_id,
                   client_id, submission_state
            FROM execution_attempts
            """
        ).fetchall() == [
            (
                derive_legacy_attempt_id(job_id, "local", 1),
                job_id,
                1,
                "local",
                "prompt-1",
                "client-a",
                "submitted",
            )
        ]
        assert connection.execute(
            """
            SELECT owner_id, scope, key, request_digest, state, job_id, lease_token
            FROM idempotency_records
            """
        ).fetchall() == [
            ("owner-a", "legacy-execute:local", "idem-1", "c" * 64, "resolved", job_id, None)
        ]
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone() == (0,)
        assert connection.execute(
            "SELECT aggregate_kind, status, checksum FROM store_migrations ORDER BY aggregate_kind"
        ).fetchall() == [
            (kind, "switched", plan.checksum)
            for kind in ("artifact", "execution_attempt", "idempotency_record", "job")
        ]


def test_g1_job_plan_blocks_active_and_persists_expired_reservation(
    tmp_path: Path, private_evidence_dir: Path
) -> None:
    source = tmp_path / "active"
    active = {
        "server_id": "local",
        "workflow_id": "portrait",
        "idempotency_key": "active-key",
        "owner_id": "",
        "prompt_id": "",
        "status": "reserved",
        "request_digest": "d" * 64,
        "claimed_at": time.time(),
        "client_id": "client-active",
        "lease_token": uuid.uuid4().hex,
    }
    _write_json(_mcp_path(source, "idempotency", "active-key"), active)
    rehearsal = FileMigrationRehearsal(source)
    manifest = rehearsal.create_manifest()
    evidence = rehearsal.backup(manifest, private_evidence_dir)
    with pytest.raises(RehearsalFailure, match="active reservation"):
        rehearsal.build_g1_plan(Path(evidence.destination), "job")

    expired_source = tmp_path / "expired"
    expired = {**active, "idempotency_key": "expired-key", "claimed_at": 1.0}
    _write_json(_mcp_path(expired_source, "idempotency", "expired-key"), expired)
    expired_rehearsal, plan = _frozen_plan(expired_source, private_evidence_dir, "job")
    store = _store(tmp_path)

    expired_rehearsal.cutover_g1(plan, store)

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT state, job_id, expires_at, lease_token FROM idempotency_records"
        ).fetchall() == [("expired", None, None, None)]
        assert connection.execute("SELECT count(*) FROM jobs").fetchone() == (0,)


def test_g1_cli_unknown_preserves_non_resubmittable_facts(
    tmp_path: Path, private_evidence_dir: Path
) -> None:
    source = tmp_path / "source"
    args = {"seed": 7}
    digest = hashlib.sha256(
        json.dumps(args, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    record = {
        "run_id": "cli-key",
        "job_id": "cli-key",
        "prompt_id": "",
        "server_id": "local",
        "workflow_id": "portrait",
        "status": "submission_unknown",
        "timestamp": "2026-07-30T01:02:03+00:00",
        "duration_ms": 0,
        "args": args,
        "request_digest": digest,
        "lease_token": uuid.uuid4().hex,
        "client_id": "cli-client",
    }
    leaf = hashlib.sha256(b"cli-key").hexdigest()
    _write_json(source / "data" / "local" / "portrait" / "history" / f"job-{leaf}.json", record)
    rehearsal, plan = _frozen_plan(source, private_evidence_dir, "job")
    store = _store(tmp_path)
    job_id = derive_legacy_unknown_job_id("", "local", "cli-key", digest)

    rehearsal.cutover_g1(plan, store)

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT job_id, status, created_at_source FROM jobs"
        ).fetchall() == [(job_id, "submission_unknown", "legacy_timestamp")]
        assert connection.execute(
            "SELECT state, job_id, lease_token FROM idempotency_records"
        ).fetchall() == [("submission_unknown", job_id, None)]
        assert connection.execute(
            "SELECT submission_state, upstream_prompt_id, upstream_job_id FROM execution_attempts"
        ).fetchall() == [("submission_unknown", None, None)]


def test_g1_cutover_retries_same_evidence_but_rejects_database_tampering(
    tmp_path: Path, private_evidence_dir: Path
) -> None:
    source = tmp_path / "source"
    record = _asset()
    _write_json(source / "data" / "assets" / f"{record['asset_id']}.json", record)
    rehearsal, plan = _frozen_plan(source, private_evidence_dir, "asset")
    store = _store(tmp_path)

    first = rehearsal.cutover_g1(plan, store)
    second = rehearsal.cutover_g1(plan, store)
    assert first.outcome == "switched"
    assert second.outcome == "already_switched"

    with sqlite3.connect(store.path) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' "
            "AND name='tr_assets_identity_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER tr_assets_identity_update")
        connection.execute("UPDATE assets SET name = 'tampered.png'")
        connection.execute(trigger_sql)
    with pytest.raises(RehearsalFailure, match="projection"):
        rehearsal.cutover_g1(plan, store)


def test_g1_retry_rejects_extra_related_alias(tmp_path: Path, private_evidence_dir: Path) -> None:
    source = tmp_path / "source"
    record = _asset()
    asset_id = str(record["asset_id"])
    _write_json(source / "data" / "assets" / f"{asset_id}.json", record)
    rehearsal, plan = _frozen_plan(source, private_evidence_dir, "asset")
    store = _store(tmp_path)
    rehearsal.cutover_g1(plan, store)

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            INSERT INTO legacy_resource_aliases(
                alias_uri, canonical_uri, object_kind, workflow_id,
                asset_id, job_id, artifact_id, created_at
            ) VALUES (?, ?, 'asset', NULL, ?, NULL, NULL, ?)
            """,
            (
                f"comfyui://assets/other/{asset_id}",
                f"comfyui://assets/{asset_id}",
                asset_id,
                record["created_at"],
            ),
        )

    with pytest.raises(RehearsalFailure, match="ID set"):
        rehearsal.cutover_g1(plan, store)


def test_g1_cutover_rejects_different_checksum_and_partial_job_switch(
    tmp_path: Path, private_evidence_dir: Path
) -> None:
    source = tmp_path / "source"
    record = _asset()
    path = source / "data" / "assets" / f"{record['asset_id']}.json"
    _write_json(path, record)
    rehearsal, first_plan = _frozen_plan(source, private_evidence_dir, "asset")
    store = _store(tmp_path)
    rehearsal.cutover_g1(first_plan, store)

    record["name"] = "dog.png"
    record["comfyui_ref"] = "agent/assets/dog.png"
    _write_json(path, record)
    second_rehearsal, second_plan = _frozen_plan(source, private_evidence_dir, "asset")
    with pytest.raises(RehearsalFailure, match="checksum"):
        second_rehearsal.cutover_g1(second_plan, store)

    job_source = tmp_path / "jobs"
    _write_json(_mcp_path(job_source, "prompts", "prompt-1"), _mcp_job(idempotency_key=""))
    job_rehearsal, job_plan = _frozen_plan(job_source, private_evidence_dir, "job")
    job_store = SQLiteControlPlaneStore((tmp_path / "jobs.sqlite3").resolve())
    job_store.initialize()
    with sqlite3.connect(job_store.path) as connection:
        connection.execute(
            """
            INSERT INTO store_migrations(
                aggregate_kind, version, status, checksum, switched_at
            ) VALUES ('job', 1, 'switched', ?, '2026-07-30T00:00:00+00:00')
            """,
            (job_plan.checksum,),
        )
    with pytest.raises(RehearsalFailure, match="partial"):
        job_rehearsal.cutover_g1(job_plan, job_store)


def test_g1_cutover_rolls_back_injected_failure_and_source_drift(
    tmp_path: Path, private_evidence_dir: Path
) -> None:
    source = tmp_path / "source"
    record = _asset()
    path = source / "data" / "assets" / f"{record['asset_id']}.json"
    _write_json(path, record)
    rehearsal, plan = _frozen_plan(source, private_evidence_dir, "asset")
    store = _store(tmp_path)

    def fail(phase: str) -> None:
        if phase == "after_import":
            raise RuntimeError("injected transaction failure")

    with pytest.raises(RuntimeError, match="injected"):
        rehearsal.cutover_g1(plan, store, failure_injector=fail)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM assets").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM legacy_resource_aliases").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM store_migrations").fetchone() == (0,)

    record["name"] = "changed.png"
    record["comfyui_ref"] = "agent/assets/changed.png"
    _write_json(path, record)
    with pytest.raises(ManifestDriftError, match="source"):
        rehearsal.cutover_g1(plan, store)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM assets").fetchone() == (0,)


@pytest.mark.parametrize("failed_phase", ["after_projection", "after_switch", "before_commit"])
def test_g1_cutover_rolls_back_late_transaction_failures(
    tmp_path: Path, private_evidence_dir: Path, failed_phase: str
) -> None:
    source = tmp_path / "source"
    record = _asset()
    _write_json(source / "data" / "assets" / f"{record['asset_id']}.json", record)
    rehearsal, plan = _frozen_plan(source, private_evidence_dir, "asset")
    store = SQLiteControlPlaneStore((tmp_path / f"{failed_phase}.sqlite3").resolve())
    store.initialize()

    def fail(phase: str) -> None:
        if phase == failed_phase:
            raise RuntimeError(f"injected {phase} failure")

    with pytest.raises(RuntimeError, match=failed_phase):
        rehearsal.cutover_g1(plan, store, failure_injector=fail)

    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM assets").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM legacy_resource_aliases").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM store_migrations").fetchone() == (0,)


def test_g1_job_plan_rejects_nonempty_legacy_outputs(
    tmp_path: Path, private_evidence_dir: Path
) -> None:
    source = tmp_path / "source"
    output = {"filename": "x.png", "subfolder": "", "type": "output"}
    _write_json(
        _mcp_path(source, "prompts", "prompt-output"),
        _mcp_job(prompt_id="prompt-output", outputs=[output], idempotency_key=""),
    )
    rehearsal = FileMigrationRehearsal(source)
    evidence = rehearsal.backup(rehearsal.create_manifest(), private_evidence_dir)

    with pytest.raises(RehearsalFailure, match="Artifact"):
        rehearsal.build_g1_plan(Path(evidence.destination), "job")
