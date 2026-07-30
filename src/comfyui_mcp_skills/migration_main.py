"""Executable read-only legacy file migration dry-run."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict
from pathlib import Path

from comfyui_mcp_skills.infrastructure.persistence.file_migration import (
    FileMigrationRehearsal,
    ManifestDriftError,
    RehearsalFailure,
)


def main() -> int:
    source_root = Path(os.environ.get("COMFYUI_MCP_DIR", os.getcwd()))
    try:
        rehearsal = FileMigrationRehearsal(source_root)
        report = rehearsal.dry_run()
        backup_payload: dict[str, object] | None = None
        backup_path = os.environ.get("COMFYUI_MCP_MIGRATION_BACKUP")
        if report.ok and backup_path:
            backup_payload = asdict(rehearsal.backup(report.manifest, Path(backup_path)))
        payload = {
            "ok": report.ok,
            "manifest_version": report.manifest.version,
            "manifest_digest": report.manifest.digest,
            "source_files": len(report.manifest.entries),
            "valid_records": report.valid_records,
            "conflicts": [asdict(conflict) for conflict in report.conflicts],
            "writes_performed": report.writes_performed,
            "backup": backup_payload,
        }
        exit_code = 0 if report.ok else 2
    except (ManifestDriftError, RehearsalFailure, OSError, ValueError, sqlite3.Error) as exc:
        payload = {
            "ok": False,
            "conflicts": [],
            "writes_performed": False,
            "backup": None,
            "error": {
                "code": "migration_evidence_failed",
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
        exit_code = 3
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
