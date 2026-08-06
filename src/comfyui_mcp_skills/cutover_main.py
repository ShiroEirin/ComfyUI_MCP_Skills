"""Explicit production cutover from legacy files to SQLite aggregates."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict
from pathlib import Path

from comfyui_mcp_skills.infrastructure.persistence.control_plane import (
    SQLiteControlPlaneStore,
)
from comfyui_mcp_skills.infrastructure.persistence.file_migration import (
    FileMigrationRehearsal,
    ManifestDriftError,
    MigrationManifest,
    RehearsalFailure,
)
from comfyui_mcp_skills.infrastructure.persistence.g3_migration import (
    build_g3_import_plan,
    cutover_g3_import_plan,
)

CONFIRMATION = "SWITCH FILE STORES TO SQLITE"

_GROUP_AGGREGATES: dict[str, frozenset[str]] = {
    "asset": frozenset({"asset"}),
    "job": frozenset({"job", "execution_attempt", "idempotency_record", "artifact"}),
    "workflow": frozenset({"workflow", "revision", "deployment"}),
}


def _group_status(database: Path) -> dict[str, str]:
    if not database.is_file():
        return {name: "not_switched" for name in _GROUP_AGGREGATES}
    try:
        with sqlite3.connect(database) as connection:
            rows = connection.execute(
                "SELECT aggregate_kind FROM store_migrations WHERE status='switched'"
            ).fetchall()
    except sqlite3.Error:
        return {name: "unknown" for name in _GROUP_AGGREGATES}
    switched = {str(row[0]) for row in rows}
    result: dict[str, str] = {}
    for name, required in _GROUP_AGGREGATES.items():
        count = len(switched & required)
        result[name] = (
            "switched"
            if count == len(required)
            else ("partial" if count else "not_switched")
        )
    return result


def _failure(
    code: str,
    exc: BaseException,
    *,
    database: Path,
    backup: dict[str, object] | None,
) -> dict[str, object]:
    groups = _group_status(database)
    writes = any(value in {"switched", "partial"} for value in groups.values())
    payload: dict[str, object] = {
        "ok": False,
        "writes_performed": writes,
        "groups": groups,
        "backup": backup,
        "error": {
            "code": code,
            "type": type(exc).__name__,
            "message": str(exc),
        },
    }
    if writes and backup is not None:
        payload["recovery"] = {
            "evidence": backup.get("destination"),
            "action": "rerun with COMFYUI_MCP_MIGRATION_EVIDENCE set to this path",
        }
    return payload


def main() -> int:
    """Verify frozen evidence, import all routed aggregates, and print JSON evidence."""
    database = Path(
        os.environ.get("COMFYUI_MCP_DIR", os.getcwd())
    ).resolve() / "data" / "control-plane.sqlite3"
    backup_payload: dict[str, object] | None = None
    try:
        if os.environ.get("COMFYUI_MCP_MIGRATION_CONFIRM", "") != CONFIRMATION:
            raise PermissionError(
                f"COMFYUI_MCP_MIGRATION_CONFIRM must equal {CONFIRMATION!r}"
            )

        source_root = Path(os.environ.get("COMFYUI_MCP_DIR", os.getcwd()))
        rehearsal = FileMigrationRehearsal(source_root)
        evidence_value = os.environ.get("COMFYUI_MCP_MIGRATION_EVIDENCE", "").strip()
        if evidence_value:
            evidence_root = Path(evidence_value).absolute()
            manifest = MigrationManifest.load(evidence_root / "migration-manifest.json")
            backup_payload = {
                "manifest_digest": manifest.digest,
                "copied_files": len(manifest.entries),
                "verified": True,
                "destination": str(evidence_root),
            }
        else:
            backup_parent = os.environ.get("COMFYUI_MCP_MIGRATION_BACKUP", "").strip()
            if not backup_parent:
                raise ValueError("COMFYUI_MCP_MIGRATION_BACKUP is required")
            report = rehearsal.dry_run()
            if not report.ok:
                raise RehearsalFailure(
                    f"migration dry-run found {len(report.conflicts)} conflict(s)"
                )
            evidence = rehearsal.backup(report.manifest, Path(backup_parent))
            evidence_root = Path(evidence.destination)
            manifest = report.manifest
            backup_payload = asdict(evidence)

        asset_plan = rehearsal.build_g1_plan(evidence_root, "asset")
        job_plan = rehearsal.build_g1_plan(evidence_root, "job")
        workflow_plan = build_g3_import_plan(evidence_root)
        store = SQLiteControlPlaneStore(database)
        store.initialize()

        asset = rehearsal.cutover_g1(asset_plan, store)
        job = rehearsal.cutover_g1(job_plan, store)
        conflicts = rehearsal.verify_manifest(manifest)
        if conflicts:
            first = conflicts[0]
            raise ManifestDriftError(
                f"source drift: {first.code}: {first.relative_path}: {first.message}"
            )
        workflow = cutover_g3_import_plan(workflow_plan, store)
        payload = {
            "ok": True,
            "writes_performed": True,
            "manifest_digest": manifest.digest,
            "backup": backup_payload,
            "groups": {
                "asset": asset.outcome,
                "job": job.outcome,
                "workflow": workflow.outcome,
            },
            "results": {
                "asset": asdict(asset),
                "job": asdict(job),
                "workflow": asdict(workflow),
            },
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except PermissionError as exc:
        failure = _failure(
            "migration_confirmation_required", exc, database=database, backup=backup_payload
        )
    except Exception as exc:
        failure = _failure(
            "migration_cutover_failed", exc, database=database, backup=backup_payload
        )
    print(json.dumps(failure, ensure_ascii=False, sort_keys=True))
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
