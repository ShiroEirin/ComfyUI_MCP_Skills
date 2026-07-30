"""Read-only file manifests and isolated control-plane migration rehearsals."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from comfyui_mcp_skills.domain.identifiers import validate_identifier
from comfyui_mcp_skills.domain.workflow_schema import (
    build_input_schema,
    normalize_parameters,
    validate_parameter_targets,
)
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.migration_lock import (
    project_migration_lock,
)

_MANIFEST_VERSION = 1
_MAX_FILES = 5_000
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_READ_CHUNK = 1024 * 1024
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_MANIFEST_PATH_LENGTH = 4_096
_MAX_TIMESTAMP_NS = 2**63 - 1
_ASSET_ID = re.compile(r"asset_[0-9a-f]{32}(?:[0-9a-f]{32})?\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_RUN_STATES = {
    "reserved",
    "submission_unknown",
    "submitted",
    "queued",
    "running",
    "completed",
    "success",
    "error",
    "interrupted",
    "cancelled",
}


class ManifestDriftError(RuntimeError):
    """The source tree no longer matches a captured manifest."""


class RehearsalFailure(RuntimeError):
    """An isolated migration rehearsal could not complete atomically."""


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    relative_path: str
    sha256: str
    size_bytes: int
    mtime_ns: int

    def to_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
        }


@dataclass(frozen=True, slots=True)
class MigrationManifest:
    version: int
    captured_at_ns: int
    entries: tuple[ManifestEntry, ...]
    digest: str

    @classmethod
    def from_dict(cls, value: object) -> MigrationManifest:
        """Strictly reconstruct a manifest without refreshing frozen evidence."""
        if not isinstance(value, dict):
            raise ManifestDriftError("manifest root must be an object")
        expected = {"version", "captured_at_ns", "entries", "digest"}
        if any(not isinstance(key, str) for key in value):
            raise ManifestDriftError("manifest field names must be strings")
        actual = set(value)
        if actual != expected:
            unknown = sorted(actual - expected)
            missing = sorted(expected - actual)
            if unknown:
                raise ManifestDriftError(f"manifest contains unknown fields: {unknown}")
            raise ManifestDriftError(f"manifest is missing fields: {missing}")
        version = value["version"]
        captured_at_ns = value["captured_at_ns"]
        digest = value["digest"]
        entries_value = value["entries"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise ManifestDriftError("manifest version must be an integer")
        if isinstance(captured_at_ns, bool) or not isinstance(captured_at_ns, int):
            raise ManifestDriftError("manifest capture time must be an integer")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ManifestDriftError("manifest digest must be a lowercase SHA-256")
        if not isinstance(entries_value, list):
            raise ManifestDriftError("manifest entries must be a list")
        if len(entries_value) > _MAX_FILES:
            raise ManifestDriftError("manifest file count exceeds the limit")
        entries: list[ManifestEntry] = []
        entry_fields = {"relative_path", "sha256", "size_bytes", "mtime_ns"}
        for index, item in enumerate(entries_value):
            if not isinstance(item, dict):
                raise ManifestDriftError(f"manifest entry {index} must be an object")
            if any(not isinstance(key, str) for key in item):
                raise ManifestDriftError(
                    f"manifest entry {index} field names must be strings"
                )
            item_fields = set(item)
            if item_fields != entry_fields:
                unknown = sorted(item_fields - entry_fields)
                missing = sorted(entry_fields - item_fields)
                if unknown:
                    raise ManifestDriftError(
                        f"manifest entry {index} contains unknown fields: {unknown}"
                    )
                raise ManifestDriftError(f"manifest entry {index} is missing fields: {missing}")
            relative_path = item["relative_path"]
            if not isinstance(relative_path, str):
                raise ManifestDriftError(f"manifest entry {index} path must be a string")
            entries.append(
                ManifestEntry(
                    relative_path=relative_path,
                    sha256=item["sha256"],
                    size_bytes=item["size_bytes"],
                    mtime_ns=item["mtime_ns"],
                )
            )
        manifest = cls(version, captured_at_ns, tuple(entries), digest)
        manifest.validate_integrity()
        return manifest

    @classmethod
    def load(cls, path: str | Path) -> MigrationManifest:
        """Load a frozen manifest with duplicate-key and resource-budget checks."""
        candidate = Path(path).absolute()
        if any(
            part.exists() and _is_link_or_reparse(part) for part in (candidate, *candidate.parents)
        ):
            raise ManifestDriftError("manifest path must not contain symbolic links")
        try:
            before = candidate.stat()
            if not stat.S_ISREG(before.st_mode):
                raise ManifestDriftError("manifest path must be a regular file")
            if before.st_size > _MAX_MANIFEST_BYTES:
                raise ManifestDriftError("manifest byte size exceeds the limit")
            raw = candidate.read_bytes()
            after = candidate.stat()
        except ManifestDriftError:
            raise
        except OSError as exc:
            raise ManifestDriftError(f"cannot read migration manifest: {exc}") from exc
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or len(raw) != after.st_size:
            raise ManifestDriftError("manifest changed while being read")
        try:
            value = _load_json_object(raw)
        except _DuplicateJsonKey as exc:
            raise ManifestDriftError(f"manifest contains a duplicate JSON key: {exc}") from exc
        except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise ManifestDriftError(f"manifest JSON is invalid: {exc}") from exc
        return cls.from_dict(value)

    @classmethod
    def create(cls, entries: Iterable[ManifestEntry], *, captured_at_ns: int) -> MigrationManifest:
        ordered = tuple(sorted(entries, key=lambda entry: entry.relative_path))
        payload = {
            "version": _MANIFEST_VERSION,
            "captured_at_ns": captured_at_ns,
            "entries": [entry.to_dict() for entry in ordered],
        }
        digest = hashlib.sha256(_canonical_json(payload)).hexdigest()
        return cls(_MANIFEST_VERSION, captured_at_ns, ordered, digest)

    def validate_integrity(self) -> None:
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version != _MANIFEST_VERSION
        ):
            raise ManifestDriftError("unsupported manifest version")
        if (
            isinstance(self.captured_at_ns, bool)
            or not isinstance(self.captured_at_ns, int)
            or not 0 < self.captured_at_ns <= _MAX_TIMESTAMP_NS
        ):
            raise ManifestDriftError(
                "manifest capture time must be a positive signed 64-bit integer"
            )
        if not isinstance(self.entries, tuple):
            raise ManifestDriftError("manifest entries must be an immutable tuple")
        if not isinstance(self.digest, str) or _SHA256.fullmatch(self.digest) is None:
            raise ManifestDriftError("manifest digest must be a lowercase SHA-256")
        if len(self.entries) > _MAX_FILES:
            raise ManifestDriftError("manifest file count exceeds the limit")
        paths: list[str] = []
        total_bytes = 0
        for entry in self.entries:
            if not isinstance(entry, ManifestEntry):
                raise ManifestDriftError("manifest entries must contain ManifestEntry values")
            path = _validate_relative_path(entry.relative_path)
            if len(path) > _MAX_MANIFEST_PATH_LENGTH:
                raise ManifestDriftError(f"manifest path exceeds the length limit: {path[:80]}")
            paths.append(path)
            if not isinstance(entry.sha256, str) or _SHA256.fullmatch(entry.sha256) is None:
                raise ManifestDriftError(f"invalid manifest SHA-256: {path}")
            if (
                isinstance(entry.size_bytes, bool)
                or not isinstance(entry.size_bytes, int)
                or entry.size_bytes < 0
            ):
                raise ManifestDriftError(f"invalid manifest size_bytes: {path}")
            if (
                isinstance(entry.mtime_ns, bool)
                or not isinstance(entry.mtime_ns, int)
                or not 0 <= entry.mtime_ns <= _MAX_TIMESTAMP_NS
            ):
                raise ManifestDriftError(f"invalid manifest mtime_ns: {path}")
            if entry.size_bytes > _MAX_FILE_BYTES:
                raise ManifestDriftError(f"manifest file exceeds the size limit: {path}")
            total_bytes += entry.size_bytes
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ManifestDriftError("manifest paths must be unique and sorted")
        if total_bytes > _MAX_TOTAL_BYTES:
            raise ManifestDriftError("manifest total size exceeds the limit")
        rebuilt = MigrationManifest.create(self.entries, captured_at_ns=self.captured_at_ns)
        if not hmac.compare_digest(rebuilt.digest, self.digest):
            raise ManifestDriftError("manifest digest does not match its entries")

    def to_dict(self) -> dict[str, object]:
        self.validate_integrity()
        return {
            "version": self.version,
            "captured_at_ns": self.captured_at_ns,
            "entries": [entry.to_dict() for entry in self.entries],
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class MigrationConflict:
    code: str
    relative_path: str
    message: str


@dataclass(frozen=True, slots=True)
class BackupEvidence:
    manifest_digest: str
    copied_files: int
    verified: bool
    destination: str


@dataclass(frozen=True, slots=True)
class MigrationDryRunReport:
    manifest: MigrationManifest
    conflicts: tuple[MigrationConflict, ...]
    valid_records: int
    writes_performed: bool = False

    @property
    def ok(self) -> bool:
        return not self.conflicts


@dataclass(frozen=True, slots=True)
class IsolatedRehearsalDatabase:
    path: Path
    nonce: str


@dataclass(frozen=True, slots=True)
class CutoverRehearsalResult:
    imported: int
    reused: int
    manifest_digest: str


@dataclass(frozen=True, slots=True)
class _CapturedFile:
    entry: ManifestEntry
    raw: bytes


class FileMigrationRehearsal:
    """Capture exact legacy files without invoking side-effecting repositories."""

    def __init__(self, source_root: str | Path) -> None:
        original = Path(source_root)
        if not original.exists() or not original.is_dir():
            raise ValueError("migration source root must be an existing directory")
        if original.exists() and _is_link_or_reparse(original):
            raise ValueError("migration source root must not be a symbolic link")
        root = original.absolute()
        if any(part.exists() and _is_link_or_reparse(part) for part in (root, *root.parents)):
            raise ValueError("migration source root must not contain symbolic links")
        self.source_root = root.resolve(strict=False)
        data_root = self.source_root / "data"
        if not data_root.exists() or not data_root.is_dir() or _is_link_or_reparse(data_root):
            raise ValueError("migration source root must contain a regular data directory")
        self._isolated_databases: dict[Path, str] = {}
        self._migration_lock = project_migration_lock(self.source_root)

    def create_manifest(self) -> MigrationManifest:
        with self._migration_lock:
            return self._create_manifest_locked()

    def verify_manifest(self, manifest: MigrationManifest) -> tuple[MigrationConflict, ...]:
        with self._migration_lock:
            return self._verify_manifest_locked(manifest)

    def backup(self, manifest: MigrationManifest, destination_parent: str | Path) -> BackupEvidence:
        with self._migration_lock:
            return self._backup_locked(manifest, destination_parent)

    def dry_run(self) -> MigrationDryRunReport:
        with self._migration_lock:
            return self._dry_run_locked()

    def build_g1_plan(self, backup_root: str | Path, aggregate: Literal["asset", "job"]) -> Any:
        """Build a production plan solely from a verified frozen backup."""
        with self._migration_lock:
            manifest, captured = self._load_g1_backup_locked(backup_root)
            conflicts = self._verify_manifest_locked(manifest)
            if conflicts:
                first = conflicts[0]
                raise ManifestDriftError(
                    f"source drift: {first.code}: {first.relative_path}: {first.message}"
                )
            from comfyui_mcp_skills.infrastructure.persistence.g1_migration import (
                FrozenLegacyFile,
                build_g1_import_plan,
            )

            return build_g1_import_plan(
                source_root=self.source_root,
                backup_root=Path(backup_root).absolute(),
                manifest=manifest,
                files=tuple(FrozenLegacyFile(item.entry, item.raw) for item in captured),
                aggregate=aggregate,
            )

    def cutover_g1(
        self,
        plan: Any,
        store: SQLiteControlPlaneStore,
        *,
        failure_injector: Callable[[str], None] | None = None,
    ) -> Any:
        """Atomically import, verify, and switch one production G1 group."""
        from comfyui_mcp_skills.infrastructure.persistence.g1_migration import (
            G1ImportPlan,
            cutover_g1_import_plan,
        )

        if not isinstance(plan, G1ImportPlan):
            raise TypeError("plan must be a G1ImportPlan")
        if plan.source_root.resolve(strict=False) != self.source_root:
            raise RehearsalFailure("G1 plan belongs to a different source root")
        with self._migration_lock:

            def verify_evidence() -> None:
                manifest, captured = self._load_g1_backup_locked(plan.backup_root)
                if manifest != plan.manifest or tuple(
                    (item.entry, item.raw) for item in captured
                ) != tuple((item.entry, item.raw) for item in plan.files):
                    raise ManifestDriftError("frozen backup changed after G1 planning")
                conflicts = self._verify_manifest_locked(plan.manifest)
                if conflicts:
                    first = conflicts[0]
                    raise ManifestDriftError(
                        f"source drift: {first.code}: {first.relative_path}: {first.message}"
                    )

            verify_evidence()
            return cutover_g1_import_plan(
                plan,
                store,
                verify_evidence=verify_evidence,
                failure_injector=failure_injector,
            )

    def _load_g1_backup_locked(
        self, backup_root: str | Path
    ) -> tuple[MigrationManifest, tuple[_CapturedFile, ...]]:
        root = Path(backup_root).absolute()
        if not root.exists() or not root.is_dir():
            raise ManifestDriftError("verified backup root must be an existing directory")
        if _is_under(root.resolve(strict=False), self.source_root):
            raise ManifestDriftError("verified backup must be outside the source root")
        if any(part.exists() and _is_link_or_reparse(part) for part in (root, *root.parents)):
            raise ManifestDriftError("verified backup path must not contain symbolic links")
        try:
            _validate_private_parent(root)
        except (OSError, ValueError) as exc:
            raise ManifestDriftError(f"verified backup permissions are invalid: {exc}") from exc
        manifest = MigrationManifest.load(root / "migration-manifest.json")
        captured: list[_CapturedFile] = []
        for entry in manifest.entries:
            try:
                item = _capture_open_file(
                    root / Path(entry.relative_path), root, entry.relative_path
                )
            except (OSError, ValueError, ManifestDriftError) as exc:
                raise ManifestDriftError(
                    f"verified backup file is invalid: {entry.relative_path}: {exc}"
                ) from exc
            if item.entry != entry:
                raise ManifestDriftError(
                    f"verified backup file differs from manifest: {entry.relative_path}"
                )
            captured.append(item)
        return manifest, tuple(captured)

    def _create_manifest_locked(self) -> MigrationManifest:
        captured, conflicts, captured_at_ns = self._capture_sources()
        if conflicts:
            first = conflicts[0]
            raise ManifestDriftError(f"{first.code}: {first.relative_path}: {first.message}")
        return MigrationManifest.create(
            (item.entry for item in captured), captured_at_ns=captured_at_ns
        )

    def _verify_manifest_locked(self, manifest: MigrationManifest) -> tuple[MigrationConflict, ...]:
        manifest.validate_integrity()
        captured, conflicts, _ = self._capture_sources()
        current_entries = tuple(item.entry for item in captured)
        if current_entries != manifest.entries:
            conflicts = (
                *conflicts,
                MigrationConflict(
                    "manifest_changed",
                    "",
                    "source files changed after the manifest was captured",
                ),
            )
        return _sort_conflicts(conflicts)

    def _backup_locked(
        self, manifest: MigrationManifest, destination_parent: str | Path
    ) -> BackupEvidence:
        manifest.validate_integrity()
        captured, conflicts, _ = self._capture_sources()
        if conflicts or tuple(item.entry for item in captured) != manifest.entries:
            raise ManifestDriftError("source files changed before backup")
        parent = Path(destination_parent).absolute()
        if _is_under(parent.resolve(strict=False), self.source_root):
            raise ValueError("backup destination must be outside the source root")
        if os.name == "nt" and str(parent).startswith("\\\\"):
            raise ValueError("network backup destinations are not allowed")
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _validate_private_parent(parent)
        if _is_link_or_reparse(parent):
            raise ValueError("backup parent must not be a symbolic link")
        stage = parent / f".migration-backup.{uuid.uuid4().hex}.tmp"
        final = parent / f"migration-backup-{manifest.digest[:16]}-{uuid.uuid4().hex}"
        try:
            stage.mkdir(mode=0o700)
            for item in captured:
                target = stage / Path(item.entry.relative_path)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _write_private_file(target, item.raw)
                os.utime(target, ns=(item.entry.mtime_ns, item.entry.mtime_ns))
                copied = _capture_open_file(target, stage, item.entry.relative_path)
                if copied.entry != item.entry or copied.raw != item.raw:
                    raise RehearsalFailure(
                        f"backup verification failed: {item.entry.relative_path}"
                    )
            _atomic_write_json(stage / "migration-manifest.json", manifest.to_dict())
            if self._verify_manifest_locked(manifest):
                raise ManifestDriftError("source files changed during backup")
            os.rename(stage, final)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        return BackupEvidence(manifest.digest, len(captured), True, str(final))

    def _dry_run_locked(self) -> MigrationDryRunReport:
        captured, scan_conflicts, captured_at_ns = self._capture_sources()
        manifest = MigrationManifest.create(
            (item.entry for item in captured), captured_at_ns=captured_at_ns
        )
        conflicts = list(scan_conflicts)
        parsed: dict[str, dict[str, Any]] = {}
        for item in captured:
            relative = item.entry.relative_path
            try:
                parsed[relative] = _load_json_object(item.raw)
            except _DuplicateJsonKey as exc:
                conflicts.append(MigrationConflict("duplicate_json_key", relative, str(exc)))
            except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
                conflicts.append(MigrationConflict("invalid_json", relative, str(exc)))
        conflicts.extend(self._validate_assets(parsed))
        conflicts.extend(self._validate_workflows(parsed))
        conflicts.extend(self._validate_run_records(parsed, manifest.captured_at_ns))
        conflicts.extend(self._validate_duplicate_run_sources(parsed))
        conflicted_paths = {
            conflict.relative_path for conflict in conflicts if conflict.relative_path
        }
        valid_records = sum(1 for path in parsed if path not in conflicted_paths)
        return MigrationDryRunReport(
            manifest=manifest,
            conflicts=_sort_conflicts(conflicts),
            valid_records=valid_records,
        )

    def create_isolated_database(self, parent: str | Path) -> IsolatedRehearsalDatabase:
        parent_path = Path(parent)
        if not parent_path.is_absolute():
            parent_path = parent_path.resolve()
        if parent_path.exists() and not parent_path.is_dir():
            raise NotADirectoryError("isolated rehearsal parent must be a directory")
        parent_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if _is_link_or_reparse(parent_path):
            raise ValueError("isolated rehearsal parent must not be a symbolic link")
        private_root = Path(tempfile.mkdtemp(prefix="comfyui-g0-rehearsal-", dir=parent_path))
        private_root.chmod(0o700)
        _validate_private_parent(parent_path)
        database_path = private_root / "control-plane.sqlite3"
        store = SQLiteControlPlaneStore(database_path)
        store.initialize()
        nonce = uuid.uuid4().hex + uuid.uuid4().hex
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                INSERT INTO test_migration_database_role(singleton, role, nonce)
                VALUES (1, 'g0_isolated_rehearsal', ?)
                """,
                (nonce,),
            )
        self._isolated_databases[database_path.resolve()] = nonce
        return IsolatedRehearsalDatabase(database_path.resolve(), nonce)

    def rehearse_isolated_cutover(
        self,
        report: MigrationDryRunReport,
        database: IsolatedRehearsalDatabase,
        *,
        rehearsal_name: str,
        fail_after_import: bool = False,
    ) -> CutoverRehearsalResult:
        with self._migration_lock:
            return self._rehearse_isolated_cutover_locked(
                report,
                database,
                rehearsal_name=rehearsal_name,
                fail_after_import=fail_after_import,
            )

    def _rehearse_isolated_cutover_locked(
        self,
        report: MigrationDryRunReport,
        database: IsolatedRehearsalDatabase,
        *,
        rehearsal_name: str,
        fail_after_import: bool = False,
    ) -> CutoverRehearsalResult:
        if not report.ok:
            raise RehearsalFailure("dry-run conflicts must be resolved before rehearsal")
        report.manifest.validate_integrity()
        registered_nonce = self._isolated_databases.get(database.path.resolve())
        if registered_nonce is None or not hmac.compare_digest(registered_nonce, database.nonce):
            raise RehearsalFailure("database token was not created by this rehearsal instance")
        fresh_report = self._dry_run_locked()
        if not fresh_report.ok or fresh_report.manifest.entries != report.manifest.entries:
            raise RehearsalFailure("dry-run must be recomputed successfully before rehearsal")
        rehearsal_name = validate_identifier(rehearsal_name, field="rehearsal_name")
        SQLiteControlPlaneStore(database.path).initialize()
        connection = sqlite3.connect(database.path, isolation_level=None, timeout=5.0)
        transaction_started = False
        imported = 0
        reused = 0
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("BEGIN IMMEDIATE")
            transaction_started = True
            marker = connection.execute(
                "SELECT role, nonce FROM test_migration_database_role WHERE singleton = 1"
            ).fetchone()
            if marker is None or tuple(marker) != ("g0_isolated_rehearsal", database.nonce):
                raise RehearsalFailure("database is not the isolated rehearsal database")
            if connection.execute("SELECT 1 FROM store_migrations LIMIT 1").fetchone():
                raise RehearsalFailure("production cutover evidence is not allowed in rehearsal")
            existing_switch = connection.execute(
                """
                SELECT checksum, status FROM test_migration_switches
                WHERE rehearsal_name = ?
                """,
                (rehearsal_name,),
            ).fetchone()
            if existing_switch is not None and tuple(existing_switch) != (
                report.manifest.digest,
                "switched",
            ):
                raise RehearsalFailure("existing rehearsal evidence conflicts with manifest")
            aggregate_ids: list[str] = []
            for entry in report.manifest.entries:
                aggregate_id = (
                    "migration-file-"
                    + hashlib.sha256(entry.relative_path.encode("utf-8")).hexdigest()
                )
                aggregate_ids.append(aggregate_id)
                payload = json.dumps(
                    entry.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                existing = connection.execute(
                    "SELECT payload_json FROM test_aggregates WHERE aggregate_id = ?",
                    (aggregate_id,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO test_aggregates(
                            aggregate_id, payload_json, revision, created_at
                        ) VALUES (?, ?, 0, ?)
                        """,
                        (aggregate_id, payload, _utc_now()),
                    )
                    imported += 1
                elif str(existing[0]) == payload:
                    reused += 1
                else:
                    raise RehearsalFailure(
                        f"imported aggregate conflicts with manifest: {entry.relative_path}"
                    )
            if fail_after_import:
                raise RehearsalFailure("injected failure after import")
            if _count_aggregates(connection, aggregate_ids) != len(aggregate_ids):
                raise RehearsalFailure("import count validation failed")
            if self._verify_manifest_locked(report.manifest):
                raise ManifestDriftError("source files changed during cutover rehearsal")
            if existing_switch is None:
                connection.execute(
                    """
                    INSERT INTO test_migration_switches(
                        rehearsal_name, version, checksum, status, switched_at
                    ) VALUES (?, 1, ?, 'switched', ?)
                    """,
                    (rehearsal_name, report.manifest.digest, _utc_now()),
                )
            connection.commit()
            transaction_started = False
        except BaseException:
            if transaction_started:
                connection.rollback()
            raise
        finally:
            connection.close()
        return CutoverRehearsalResult(imported, reused, report.manifest.digest)

    def _capture_sources(
        self,
    ) -> tuple[list[_CapturedFile], tuple[MigrationConflict, ...], int]:
        captured_at_ns = time.time_ns()
        captured: list[_CapturedFile] = []
        conflicts: list[MigrationConflict] = []
        try:
            candidates = self._candidate_paths()
        except OSError as exc:
            return [], (MigrationConflict("source_scan_failed", "data", str(exc)),), captured_at_ns
        if len(candidates) > _MAX_FILES:
            return (
                [],
                (
                    MigrationConflict(
                        "source_budget_exceeded", "", "source file count exceeds limit"
                    ),
                ),
                captured_at_ns,
            )
        total_bytes = 0
        for path in candidates:
            relative = path.relative_to(self.source_root).as_posix()
            try:
                item = _capture_open_file(path, self.source_root, relative)
                total_bytes += item.entry.size_bytes
                if total_bytes > _MAX_TOTAL_BYTES:
                    raise ValueError("source total size exceeds limit")
                captured.append(item)
            except (OSError, ValueError, ManifestDriftError) as exc:
                conflicts.append(MigrationConflict("unsafe_source_path", relative, str(exc)))
        try:
            final_candidates = self._candidate_paths()
        except OSError as exc:
            conflicts.append(MigrationConflict("source_scan_failed", "data", str(exc)))
        else:
            if final_candidates != candidates:
                conflicts.append(
                    MigrationConflict(
                        "source_set_changed", "data", "source file set changed during capture"
                    )
                )
        return captured, _sort_conflicts(conflicts), captured_at_ns

    def _candidate_paths(self) -> tuple[Path, ...]:
        candidates: set[Path] = set()
        visited_directories = 0

        def visit_directory() -> None:
            nonlocal visited_directories
            visited_directories += 1
            if visited_directories > 50_000:
                raise OSError("source directory count exceeds limit")

        def add(path: Path) -> None:
            candidates.add(path)
            if len(candidates) > _MAX_FILES:
                raise OSError("source file count exceeds limit")

        def add_all(paths: Iterable[Path]) -> None:
            for path in paths:
                add(path)

        data = self.source_root / "data"
        if _is_link_or_reparse(data):
            return (data,)
        assets = data / "assets"
        if _is_link_or_reparse(assets):
            add(assets)
        elif assets.exists():
            visit_directory()
            add_all(assets.glob("*.json"))
        runs = data / "runs"
        if _is_link_or_reparse(runs):
            add(runs)
        elif runs.exists():
            visit_directory()
            add_all(runs.glob("*/prompts/*.json"))
            add_all(runs.glob("*/idempotency/*.json"))
        for server_dir in data.iterdir():
            visit_directory()
            if server_dir.name in {"assets", "runs"}:
                continue
            if _is_link_or_reparse(server_dir):
                add(server_dir)
                continue
            if not server_dir.is_dir():
                continue
            for workflow_dir in server_dir.iterdir():
                visit_directory()
                if _is_link_or_reparse(workflow_dir):
                    add(workflow_dir)
                    continue
                if not workflow_dir.is_dir():
                    continue
                for name in ("schema.json", "workflow.json"):
                    path = workflow_dir / name
                    if path.exists() or path.is_symlink():
                        add(path)
                history = workflow_dir / "history"
                if _is_link_or_reparse(history):
                    add(history)
                elif history.exists():
                    visit_directory()
                    add_all(history.glob("*.json"))
        return tuple(sorted(candidates, key=lambda path: path.as_posix()))

    def _validate_assets(self, parsed: dict[str, dict[str, Any]]) -> list[MigrationConflict]:
        conflicts: list[MigrationConflict] = []
        expected = {
            "asset_id",
            "server_id",
            "comfyui_ref",
            "name",
            "subfolder",
            "media_type",
            "mime_type",
            "size_bytes",
            "sha256",
            "owner_id",
            "created_at",
        }
        for relative, value in parsed.items():
            parts = Path(relative).parts
            if len(parts) != 3 or parts[:2] != ("data", "assets"):
                continue
            missing = sorted(expected - value.keys())
            if missing:
                conflicts.append(
                    MigrationConflict("asset_invalid", relative, f"missing fields: {missing}")
                )
                continue
            try:
                asset_id = _nonempty_string(value["asset_id"], "asset_id")
                if _ASSET_ID.fullmatch(asset_id) is None or Path(relative).stem != asset_id:
                    raise ValueError("asset path and canonical asset_id must match")
                validate_identifier(value["server_id"], field="server_id")
                name = _nonempty_string(value["name"], "name")
                subfolder = _string(value["subfolder"], "subfolder")
                comfyui_ref = _nonempty_string(value["comfyui_ref"], "comfyui_ref")
                expected_ref = f"{subfolder}/{name}" if subfolder else name
                if comfyui_ref != expected_ref:
                    raise ValueError("comfyui_ref does not match subfolder/name")
                if value["media_type"] not in {"image", "audio", "video"}:
                    raise ValueError("invalid media_type")
                _nonempty_string(value["mime_type"], "mime_type")
                size = value["size_bytes"]
                if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                    raise ValueError("size_bytes must be a non-negative integer")
                digest = value["sha256"]
                if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                    raise ValueError("invalid asset sha256")
                _string(value["owner_id"], "owner_id")
                _parse_aware_time(value["created_at"], "created_at")
            except (TypeError, ValueError) as exc:
                conflicts.append(MigrationConflict("asset_invalid", relative, str(exc)))
        return conflicts

    def _validate_workflows(self, parsed: dict[str, dict[str, Any]]) -> list[MigrationConflict]:
        conflicts: list[MigrationConflict] = []
        directories: set[str] = set()
        for relative in parsed:
            path = Path(relative)
            if (
                len(path.parts) == 4
                and path.parts[0] == "data"
                and path.parts[1] not in {"assets", "runs"}
                and path.name in {"schema.json", "workflow.json"}
            ):
                directories.add(Path(*path.parts[:3]).as_posix())
        for directory in sorted(directories):
            schema_path = f"{directory}/schema.json"
            graph_path = f"{directory}/workflow.json"
            if schema_path not in parsed or graph_path not in parsed:
                conflicts.append(
                    MigrationConflict(
                        "workflow_incomplete",
                        directory,
                        "schema.json and workflow.json are both required",
                    )
                )
                continue
            server_id, workflow_id = Path(directory).parts[1:3]
            try:
                validate_identifier(server_id, field="server_id")
                validate_identifier(workflow_id, field="workflow_id")
                schema = parsed[schema_path]
                graph = parsed[graph_path]
                parameters = normalize_parameters(schema)
                validate_parameter_targets(parameters, graph)
                build_input_schema(parameters)
            except (TypeError, ValueError, KeyError) as exc:
                conflicts.append(MigrationConflict("workflow_invalid", directory, str(exc)))
        return conflicts

    def _validate_run_records(
        self, parsed: dict[str, dict[str, Any]], captured_at_ns: int
    ) -> list[MigrationConflict]:
        conflicts: list[MigrationConflict] = []
        snapshot_seconds = captured_at_ns / 1_000_000_000
        for relative, value in parsed.items():
            parts = Path(relative).parts
            is_mcp = len(parts) == 5 and parts[:2] == ("data", "runs")
            is_history = len(parts) == 5 and parts[0] == "data" and parts[3] == "history"
            if not (is_mcp or is_history):
                continue
            try:
                server_id = validate_identifier(value.get("server_id"), field="server_id")
                workflow_id = validate_identifier(value.get("workflow_id"), field="workflow_id")
                status = _nonempty_string(value.get("status"), "status")
                if status not in _RUN_STATES:
                    raise ValueError("unknown run status")
                digest = value.get("request_digest")
                if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                    raise ValueError("request_digest is required and must be SHA-256")
                owner = _string(value.get("owner_id", ""), "owner_id")
                if is_mcp:
                    server_hash = parts[2]
                    if server_hash != hashlib.sha256(server_id.encode()).hexdigest():
                        raise ValueError("run server directory hash mismatch")
                    collection = parts[3]
                    if collection == "prompts":
                        prompt_id = validate_identifier(value.get("prompt_id"), field="prompt_id")
                        if Path(relative).stem != hashlib.sha256(prompt_id.encode()).hexdigest():
                            raise ValueError("prompt file hash mismatch")
                    elif collection == "idempotency":
                        key = _nonempty_string(value.get("idempotency_key"), "idempotency_key")
                        expected = hashlib.sha256(f"{owner}\0{key}".encode()).hexdigest()
                        if Path(relative).stem != expected:
                            raise ValueError("idempotency file hash mismatch")
                    else:
                        raise ValueError("unknown run collection")
                else:
                    if (parts[1], parts[2]) != (server_id, workflow_id):
                        raise ValueError("history directory identity mismatch")
                    stem = Path(relative).stem
                    if stem.startswith("job-"):
                        external_id = _nonempty_string(value.get("job_id"), "job_id")
                        prefix = "job-"
                    elif stem.startswith("prompt-"):
                        external_id = validate_identifier(value.get("prompt_id"), field="prompt_id")
                        prefix = "prompt-"
                    else:
                        raise ValueError("invalid history filename")
                    if stem != prefix + hashlib.sha256(external_id.encode()).hexdigest():
                        raise ValueError("history filename hash mismatch")
                    run_id = _nonempty_string(value.get("run_id"), "run_id")
                    if run_id != external_id:
                        raise ValueError("history run_id does not match filename identity")
                    if prefix == "job-" and value.get("job_id") != external_id:
                        raise ValueError("job history identity mismatch")
                    if prefix == "prompt-" and value.get("job_id"):
                        raise ValueError("prompt history must not carry job_id")
                if status == "reserved":
                    if is_mcp:
                        claimed_at = value.get("claimed_at")
                        if isinstance(claimed_at, bool) or not isinstance(claimed_at, (int, float)):
                            raise ValueError("reserved MCP run requires claimed_at")
                        claim_seconds = float(claimed_at)
                    else:
                        claim_seconds = _parse_aware_time(
                            value.get("timestamp"), "timestamp"
                        ).timestamp()
                    age = snapshot_seconds - claim_seconds
                    if age < 0 or age <= 300:
                        raise ValueError("active reservation blocks migration")
                if not is_mcp:
                    arguments = value.get("args")
                    if not isinstance(arguments, dict):
                        raise ValueError("request_digest cannot be verified without args")
                    recalculated = hashlib.sha256(_canonical_json(arguments)).hexdigest()
                    if digest != recalculated:
                        raise ValueError("request_digest mismatch")
                if value.get("outputs"):
                    raise ValueError("legacy outputs lack deterministic Artifact mapping fields")
            except (TypeError, ValueError) as exc:
                conflicts.append(MigrationConflict("run_invalid", relative, str(exc)))
        return conflicts

    def _validate_duplicate_run_sources(
        self, parsed: dict[str, dict[str, Any]]
    ) -> list[MigrationConflict]:
        conflicts: list[MigrationConflict] = []
        identities: dict[tuple[str, str], tuple[str, bytes]] = {}
        for relative, value in parsed.items():
            parts = Path(relative).parts
            if not (
                (len(parts) == 5 and parts[:2] == ("data", "runs"))
                or (len(parts) == 5 and parts[0] == "data" and parts[3] == "history")
            ):
                continue
            server_id = value.get("server_id")
            prompt_id = value.get("prompt_id")
            if not isinstance(server_id, str) or not isinstance(prompt_id, str) or not prompt_id:
                continue
            identity = (server_id, prompt_id)
            payload = _canonical_json(value)
            previous = identities.get(identity)
            if previous is not None and previous[1] != payload:
                conflicts.append(
                    MigrationConflict(
                        "run_source_conflict",
                        relative,
                        f"conflicts with {previous[0]} for {server_id}/{prompt_id}",
                    )
                )
            else:
                identities[identity] = (relative, payload)
        return conflicts


def _capture_open_file(path: Path, root: Path, relative_path: str) -> _CapturedFile:
    relative_path = _validate_relative_path(relative_path)
    if any(part.exists() and _is_link_or_reparse(part) for part in (path, *path.parents)):
        raise ValueError("symbolic links or reparse points are not allowed")
    resolved_before = path.resolve(strict=True)
    if not _is_under(resolved_before, root):
        raise ValueError("source path escapes the source root")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(resolved_before, flags)
    try:
        before = os.fstat(descriptor)
        opened_path = _final_path_from_descriptor(descriptor)
        if not _is_under(opened_path, root):
            raise ValueError("opened source file escapes the source root")
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("source path is not a regular file")
        if before.st_nlink > 1:
            raise ValueError("hard-linked source files are not allowed")
        if before.st_size > _MAX_FILE_BYTES:
            raise ValueError("source file exceeds the size limit")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(_READ_CHUNK, remaining))
            if not chunk:
                raise ManifestDriftError("source file was truncated while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or len(raw) != after.st_size:
        raise ManifestDriftError("source changed while being read")
    resolved_after = path.resolve(strict=True)
    current = os.stat(resolved_after, follow_symlinks=False)
    current_identity = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
    if not _is_under(resolved_after, root) or current_identity != identity_after:
        raise ManifestDriftError("source path changed while being read")
    entry = ManifestEntry(
        relative_path=relative_path,
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=after.st_size,
        mtime_ns=after.st_mtime_ns,
    )
    return _CapturedFile(entry, raw)


def _final_path_from_descriptor(descriptor: int) -> Path:
    if os.name == "nt":
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(descriptor)
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if length <= 0 or length >= len(buffer):
            raise OSError("cannot resolve opened file handle")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value).resolve(strict=True)
    for fd_root in (Path("/proc/self/fd"), Path("/dev/fd")):
        if fd_root.exists():
            return (fd_root / str(descriptor)).resolve(strict=True)
    raise OSError("platform cannot resolve opened file descriptors safely")


def _load_json_object(raw: bytes) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateJsonKey(f"duplicate key: {key}")
            result[key] = value
        return result

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=object_pairs)
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    _validate_json_budget(value)
    return value


def _validate_json_budget(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > 100_000 or depth > 64:
            raise ValueError("JSON structure exceeds migration limits")
        if isinstance(current, str) and len(current) > 1_048_576:
            raise ValueError("JSON string exceeds migration limits")
        if isinstance(current, dict):
            for key, child in current.items():
                if len(key) > 4096:
                    raise ValueError("JSON key exceeds migration limits")
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)


def _validate_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ManifestDriftError("manifest path must be a POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ManifestDriftError("manifest path must not be absolute or traverse parents")
    if path.as_posix() != value:
        raise ManifestDriftError("manifest path is not canonical")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _atomic_write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        _write_private_file(temporary, payload.encode("utf-8"))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_private_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _count_aggregates(connection: sqlite3.Connection, aggregate_ids: list[str]) -> int:
    count = 0
    for start in range(0, len(aggregate_ids), 500):
        batch = aggregate_ids[start : start + 500]
        placeholders = ",".join("?" for _ in batch)
        count += int(
            connection.execute(
                f"SELECT count(*) FROM test_aggregates WHERE aggregate_id IN ({placeholders})",
                batch,
            ).fetchone()[0]
        )
    return count


def _validate_private_parent(path: Path) -> None:
    resolved = path.resolve(strict=True)
    if _is_link_or_reparse(resolved):
        raise ValueError("migration evidence parent must not be a symbolic link")
    identity_home = _identity_home().resolve(strict=True)
    if not _is_under(resolved, identity_home):
        raise ValueError("migration evidence paths must stay under the effective user profile")
    ancestors = [resolved]
    current = resolved
    while current != identity_home:
        current = current.parent
        ancestors.append(current)
    if any(_is_link_or_reparse(ancestor) for ancestor in ancestors):
        raise ValueError("migration evidence path must not contain symbolic links")
    if os.name == "nt":
        import ctypes

        drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(resolved.anchor))
        if drive_type == 4:
            raise ValueError("network migration evidence paths are not allowed")
        if any(_windows_has_broad_write_ace(ancestor) for ancestor in ancestors):
            raise ValueError("migration evidence path grants broad write access")
        return
    effective_uid = int(getattr(os, "geteuid")())
    for ancestor in ancestors:
        info = ancestor.stat()
        if info.st_uid != effective_uid:
            raise ValueError("migration evidence path must be owned by the effective user")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError("migration evidence path must not be group/world writable")


def _identity_home() -> Path:
    if os.name != "nt":
        import pwd

        return Path(getattr(pwd, "getpwuid")(int(getattr(os, "geteuid")())).pw_dir)
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    userenv.GetUserProfileDirectoryW.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    userenv.GetUserProfileDirectoryW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
    try:
        size = wintypes.DWORD(0)
        userenv.GetUserProfileDirectoryW(token, None, ctypes.byref(size))
        buffer = ctypes.create_unicode_buffer(size.value)
        if not userenv.GetUserProfileDirectoryW(token, buffer, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "GetUserProfileDirectoryW failed")
        return Path(buffer.value)
    finally:
        kernel32.CloseHandle(token)


def _windows_current_sid() -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    )
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "GetTokenInformation failed")
        sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID)).contents
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
        try:
            return str(sid_text.value)
        finally:
            kernel32.LocalFree(sid_text)
    finally:
        kernel32.CloseHandle(token)


def _windows_has_broad_write_ace(path: Path) -> bool:
    import ctypes
    from ctypes import wintypes

    security = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32
    descriptor = wintypes.LPVOID()
    owner = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    result = security.GetNamedSecurityInfoW(
        str(path),
        1,
        0x00000001 | 0x00000004,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result != 0:
        raise OSError(result, "GetNamedSecurityInfoW failed")
    owner_text = wintypes.LPWSTR()
    if not security.ConvertSidToStringSidW(owner, ctypes.byref(owner_text)):
        kernel32.LocalFree(descriptor)
        raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
    try:
        trusted_owners = {
            _windows_current_sid(),
            "S-1-5-18",
            "S-1-5-32-544",
        }
        if owner_text.value not in trusted_owners:
            return True
    finally:
        kernel32.LocalFree(owner_text)
    try:

        class AclSizeInformation(ctypes.Structure):
            _fields_ = [
                ("AceCount", wintypes.DWORD),
                ("AclBytesInUse", wintypes.DWORD),
                ("AclBytesFree", wintypes.DWORD),
            ]

        info = AclSizeInformation()
        if not security.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):
            raise OSError(ctypes.get_last_error(), "GetAclInformation failed")
        allowed_write_sids = {
            _windows_current_sid(),
            "S-1-3-0",
            "S-1-3-4",
            "S-1-5-18",
            "S-1-5-32-544",
        }
        write_mask = 0x500D0056
        for index in range(info.AceCount):
            ace = wintypes.LPVOID()
            if not security.GetAce(dacl, index, ctypes.byref(ace)):
                raise OSError(ctypes.get_last_error(), "GetAce failed")
            raw = ctypes.cast(ace, ctypes.POINTER(ctypes.c_ubyte))
            ace_type = int(raw[0])
            if ace_type not in {0, 1, 5, 6, 9, 10, 11, 12}:
                return True
            if ace_type in {1, 6, 10, 12}:
                continue
            mask = ctypes.cast(
                ctypes.addressof(raw.contents) + 4, ctypes.POINTER(wintypes.DWORD)
            ).contents.value
            if not mask & write_mask:
                continue
            sid_offset = 8
            if ace_type in {5, 11}:
                object_flags = ctypes.cast(
                    ctypes.addressof(raw.contents) + 8,
                    ctypes.POINTER(wintypes.DWORD),
                ).contents.value
                sid_offset = 12
                if object_flags & 0x1:
                    sid_offset += 16
                if object_flags & 0x2:
                    sid_offset += 16
            sid_pointer = ctypes.c_void_p(ctypes.addressof(raw.contents) + sid_offset)
            sid_text = wintypes.LPWSTR()
            if not security.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
                raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
            try:
                if sid_text.value not in allowed_write_sids:
                    return True
            finally:
                kernel32.LocalFree(sid_text)
        return False
    finally:
        kernel32.LocalFree(descriptor)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError(f"{field} must be a string without NUL")
    return value


def _nonempty_string(value: object, field: str) -> str:
    result = _string(value, field)
    if not result:
        raise ValueError(f"{field} must not be empty")
    return result


def _parse_aware_time(value: object, field: str) -> datetime:
    text = _nonempty_string(value, field).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def _sort_conflicts(conflicts: Iterable[MigrationConflict]) -> tuple[MigrationConflict, ...]:
    return tuple(sorted(conflicts, key=lambda item: (item.relative_path, item.code, item.message)))


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
