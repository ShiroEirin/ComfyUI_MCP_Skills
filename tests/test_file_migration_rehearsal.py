"""Executable file-manifest and isolated migration rehearsal contracts."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest

from comfyui_mcp_skills.infrastructure.persistence.file_migration import (
    FileMigrationRehearsal,
    ManifestDriftError,
    ManifestEntry,
    MigrationDryRunReport,
    MigrationManifest,
    RehearsalFailure,
)
from comfyui_mcp_skills.migration_main import main as migration_main


@pytest.fixture
def private_evidence_dir() -> Path:
    path = Path.home() / f".comfyui-mcp-evidence-test-{uuid.uuid4().hex}"
    path.mkdir(mode=0o700)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _valid_asset(asset_id: str) -> dict[str, object]:
    return {
        "asset_id": asset_id,
        "server_id": "local",
        "comfyui_ref": "agent/assets/cat.png",
        "name": "cat.png",
        "subfolder": "agent/assets",
        "media_type": "image",
        "mime_type": "image/png",
        "size_bytes": 8,
        "sha256": "a" * 64,
        "owner_id": "owner",
        "created_at": "2026-07-30T00:00:00+00:00",
    }


def test_manifest_is_deterministic_and_verifies_exact_source_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workflow = source / "data" / "local" / "portrait"
    _write_json(workflow / "workflow.json", {"1": {"inputs": {"text": "cat"}}})
    _write_json(workflow / "schema.json", {"parameters": {}, "enabled": True})
    asset_id = "asset_" + "a" * 32
    _write_json(source / "data" / "assets" / f"{asset_id}.json", _valid_asset(asset_id))

    rehearsal = FileMigrationRehearsal(source)
    first = rehearsal.create_manifest()
    second = rehearsal.create_manifest()

    assert first.entries == second.entries
    first.validate_integrity()
    second.validate_integrity()
    assert [entry.relative_path for entry in first.entries] == [
        f"data/assets/{asset_id}.json",
        "data/local/portrait/schema.json",
        "data/local/portrait/workflow.json",
    ]
    for entry in first.entries:
        raw = (source / Path(entry.relative_path)).read_bytes()
        assert entry.sha256 == hashlib.sha256(raw).hexdigest()
        assert entry.size_bytes == len(raw)
        assert entry.mtime_ns > 0
    assert rehearsal.verify_manifest(first) == ()


def test_manifest_verification_and_backup_refuse_source_drift(tmp_path: Path) -> None:
    source = tmp_path / "source"
    asset_id = "asset_" + "b" * 32
    asset_path = source / "data" / "assets" / f"{asset_id}.json"
    _write_json(asset_path, _valid_asset(asset_id))
    rehearsal = FileMigrationRehearsal(source)
    manifest = rehearsal.create_manifest()
    asset_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ManifestDriftError, match="changed"):
        rehearsal.backup(manifest, tmp_path / "backup")


def test_backup_preserves_manifest_bytes_and_metadata(
    tmp_path: Path, private_evidence_dir: Path
) -> None:
    source = tmp_path / "source"
    asset_id = "asset_" + "c" * 32
    _write_json(source / "data" / "assets" / f"{asset_id}.json", _valid_asset(asset_id))
    rehearsal = FileMigrationRehearsal(source)
    manifest = rehearsal.create_manifest()

    evidence = rehearsal.backup(manifest, private_evidence_dir / "backups")

    assert evidence.manifest_digest == manifest.digest
    assert evidence.verified is True
    destination = Path(evidence.destination)
    copied = destination / "data" / "assets" / f"{asset_id}.json"
    assert copied.read_bytes() == (source / "data" / "assets" / f"{asset_id}.json").read_bytes()
    stored_manifest = json.loads((destination / "migration-manifest.json").read_text("utf-8"))
    assert stored_manifest["digest"] == manifest.digest


def test_dry_run_reports_invalid_json_identity_and_workflow_conflicts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workflow = source / "data" / "local" / "portrait"
    _write_json(workflow / "workflow.json", {"1": {"inputs": {"text": "cat"}}})
    _write_json(
        workflow / "schema.json",
        {"parameters": {"prompt": {"node_id": "missing", "field": "text"}}},
    )
    invalid_asset = source / "data" / "assets" / "asset_old.json"
    invalid_asset.parent.mkdir(parents=True, exist_ok=True)
    invalid_asset.write_text('{"asset_id":"asset_old","asset_id":"other"}', encoding="utf-8")

    report = FileMigrationRehearsal(source).dry_run()

    assert report.ok is False
    assert {conflict.code for conflict in report.conflicts} >= {
        "duplicate_json_key",
        "workflow_invalid",
    }
    assert report.manifest.entries
    assert report.writes_performed is False


def test_manifest_rejects_symlinked_sources(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    link = source / "data" / "assets" / f"asset_{'a' * 32}.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    report = FileMigrationRehearsal(source).dry_run()

    assert any(conflict.code == "unsafe_source_path" for conflict in report.conflicts)


def test_isolated_cutover_rehearsal_is_idempotent_and_atomic(
    tmp_path: Path, private_evidence_dir: Path
) -> None:
    source = tmp_path / "source"
    asset_id = "asset_" + "d" * 32
    _write_json(source / "data" / "assets" / f"{asset_id}.json", _valid_asset(asset_id))
    rehearsal = FileMigrationRehearsal(source)
    report = rehearsal.dry_run()
    database = rehearsal.create_isolated_database(private_evidence_dir / "control-plane.sqlite3")

    first = rehearsal.rehearse_isolated_cutover(report, database, rehearsal_name="asset_manifest")
    second = rehearsal.rehearse_isolated_cutover(report, database, rehearsal_name="asset_manifest")

    assert first.imported == 1
    assert second.imported == 0
    assert second.reused == 1
    with sqlite3.connect(database.path) as connection:
        assert connection.execute("SELECT count(*) FROM test_aggregates").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM store_migrations").fetchone() == (0,)
        assert connection.execute(
            "SELECT status FROM test_migration_switches WHERE rehearsal_name = 'asset_manifest'"
        ).fetchone() == ("switched",)


def test_isolated_cutover_failure_rolls_back_without_switching(
    tmp_path: Path, private_evidence_dir: Path
) -> None:
    source = tmp_path / "source"
    asset_id = "asset_" + "e" * 32
    _write_json(source / "data" / "assets" / f"{asset_id}.json", _valid_asset(asset_id))
    rehearsal = FileMigrationRehearsal(source)
    report = rehearsal.dry_run()
    database = rehearsal.create_isolated_database(private_evidence_dir / "control-plane.sqlite3")

    with pytest.raises(RehearsalFailure, match="injected"):
        rehearsal.rehearse_isolated_cutover(
            report,
            database,
            rehearsal_name="asset_manifest",
            fail_after_import=True,
        )

    assert (source / "data" / "assets" / f"{asset_id}.json").exists()
    with sqlite3.connect(database.path) as connection:
        assert connection.execute("SELECT count(*) FROM test_aggregates").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM test_migration_switches").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM store_migrations").fetchone() == (0,)


def test_migration_dry_run_entrypoint_emits_structured_report(
    tmp_path: Path,
    private_evidence_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source"
    asset_id = "asset_" + "f" * 32
    _write_json(source / "data" / "assets" / f"{asset_id}.json", _valid_asset(asset_id))
    monkeypatch.setenv("COMFYUI_MCP_DIR", str(source))
    monkeypatch.setenv("COMFYUI_MCP_MIGRATION_BACKUP", str(private_evidence_dir / "backup"))

    assert migration_main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["manifest_digest"]
    assert payload["backup"]["verified"] is True


def test_migration_dry_run_entrypoint_returns_conflict_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source"
    path = source / "data" / "assets" / "asset_old.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("COMFYUI_MCP_DIR", str(source))
    monkeypatch.delenv("COMFYUI_MCP_MIGRATION_BACKUP", raising=False)

    assert migration_main() == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["conflicts"]


def test_manifest_integrity_rejects_forged_entries(tmp_path: Path) -> None:
    source = tmp_path / "source"
    asset_id = "asset_" + "1" * 32
    _write_json(source / "data" / "assets" / f"{asset_id}.json", _valid_asset(asset_id))
    rehearsal = FileMigrationRehearsal(source)
    manifest = rehearsal.create_manifest()
    forged = MigrationManifest(
        version=manifest.version,
        captured_at_ns=manifest.captured_at_ns,
        entries=(ManifestEntry("../forged.json", "0" * 64, 1, 1),),
        digest=manifest.digest,
    )

    with pytest.raises(ManifestDriftError):
        rehearsal.verify_manifest(forged)
    with pytest.raises(ManifestDriftError):
        rehearsal.backup(forged, tmp_path / "backups")


def test_dry_run_rejects_invalid_asset_types_and_active_reservation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    asset_id = "asset_" + "2" * 32
    invalid_asset = _valid_asset(asset_id)
    invalid_asset["media_type"] = "exe"
    invalid_asset["size_bytes"] = "eight"
    _write_json(source / "data" / "assets" / f"{asset_id}.json", invalid_asset)
    server_id = "local"
    server_hash = hashlib.sha256(server_id.encode()).hexdigest()
    owner = "owner"
    key = "key"
    key_hash = hashlib.sha256(f"{owner}\0{key}".encode()).hexdigest()
    _write_json(
        source / "data" / "runs" / server_hash / "idempotency" / f"{key_hash}.json",
        {
            "server_id": server_id,
            "workflow_id": "portrait",
            "idempotency_key": key,
            "owner_id": owner,
            "prompt_id": "",
            "status": "reserved",
            "request_digest": "a" * 64,
            "claimed_at": __import__("time").time(),
            "client_id": "client",
            "lease_token": "lease",
        },
    )

    report = FileMigrationRehearsal(source).dry_run()

    messages = {conflict.message for conflict in report.conflicts}
    assert any("media_type" in message or "size_bytes" in message for message in messages)
    assert any("active reservation" in message for message in messages)


def test_backup_excludes_secret_config(tmp_path: Path, private_evidence_dir: Path) -> None:
    source = tmp_path / "source"
    _write_json(source / "config.json", {"servers": [{"auth": "secret"}]})
    asset_id = "asset_" + "3" * 32
    _write_json(source / "data" / "assets" / f"{asset_id}.json", _valid_asset(asset_id))
    rehearsal = FileMigrationRehearsal(source)
    manifest = rehearsal.create_manifest()

    evidence = rehearsal.backup(manifest, private_evidence_dir / "backups")

    assert all(entry.relative_path != "config.json" for entry in manifest.entries)
    assert not (Path(evidence.destination) / "config.json").exists()


def test_migration_entrypoint_returns_evidence_failure_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source"
    asset_id = "asset_" + "4" * 32
    _write_json(source / "data" / "assets" / f"{asset_id}.json", _valid_asset(asset_id))
    monkeypatch.setenv("COMFYUI_MCP_DIR", str(source))
    monkeypatch.setenv("COMFYUI_MCP_MIGRATION_BACKUP", str(source / "backups"))

    assert migration_main() == 3

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "migration_evidence_failed"


def test_rehearsal_recomputes_dry_run_instead_of_trusting_report(
    tmp_path: Path, private_evidence_dir: Path
) -> None:
    source = tmp_path / "source"
    path = source / "data" / "assets" / f"asset_{'5' * 32}.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"asset_id":"a","asset_id":"b"}', encoding="utf-8")
    rehearsal = FileMigrationRehearsal(source)
    manifest = rehearsal.create_manifest()
    forged_report = MigrationDryRunReport(manifest, (), 1)
    database = rehearsal.create_isolated_database(private_evidence_dir / "scratch")

    with pytest.raises(RehearsalFailure, match="recomputed"):
        rehearsal.rehearse_isolated_cutover(forged_report, database, rehearsal_name="forged_report")


def test_migration_entrypoint_rejects_missing_source_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COMFYUI_MCP_DIR", str(tmp_path / "missing"))
    monkeypatch.delenv("COMFYUI_MCP_MIGRATION_BACKUP", raising=False)

    assert migration_main() == 3

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "migration_evidence_failed"
