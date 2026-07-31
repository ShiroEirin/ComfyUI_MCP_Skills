"""Deterministic G1 Job/Asset import plans and atomic SQLite cutover."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal

from comfyui_mcp_skills.domain.control_plane import (
    canonical_resource_uri,
    derive_legacy_attempt_id,
    derive_legacy_job_id,
    derive_legacy_unknown_job_id,
)
from comfyui_mcp_skills.domain.identifiers import validate_identifier
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.file_migration import (
    ManifestEntry,
    MigrationManifest,
    RehearsalFailure,
    _canonical_json,
    _load_json_object,
)

G1Aggregate = Literal["asset", "job"]
FailureInjector = Callable[[str], None]
_G1_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ASSET_ID = re.compile(r"asset_[0-9a-f]{32}(?:[0-9a-f]{32})?\Z")
_ALLOWED_STATUSES = frozenset(
    {
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
)
_JOB_SWITCH_GROUP = ("artifact", "execution_attempt", "idempotency_record", "job")
_ASSET_SWITCH_GROUP = ("asset",)
_TABLE_ORDER = {
    "asset": ("assets", "legacy_resource_aliases"),
    "job": (
        "jobs",
        "execution_attempts",
        "idempotency_records",
        "artifacts",
        "legacy_resource_aliases",
    ),
}
_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "assets": (
        "asset_id",
        "owner_id",
        "server_id",
        "name",
        "subfolder",
        "media_type",
        "mime_type",
        "size_bytes",
        "sha256",
        "source_type",
        "comfyui_ref",
        "created_at",
        "expires_at",
    ),
    "jobs": (
        "job_id",
        "workflow_id",
        "plan_id",
        "revision_id",
        "deployment_id",
        "owner_id",
        "status",
        "error",
        "outputs_json",
        "retry_of",
        "created_at",
        "created_at_source",
        "legacy_migrated",
        "execution_origin",
    ),
    "execution_attempts": (
        "attempt_id",
        "job_id",
        "attempt",
        "server_id",
        "upstream_prompt_id",
        "upstream_job_id",
        "client_id",
        "submission_state",
        "created_at",
    ),
    "idempotency_records": (
        "owner_id",
        "scope",
        "key",
        "request_digest",
        "state",
        "job_id",
        "client_id",
        "claimed_at",
        "expires_at",
        "lease_token",
        "workflow_id",
    ),
    "artifacts": (
        "artifact_id",
        "job_id",
        "server_id",
        "upstream_node_id",
        "output_key",
        "upstream_output_index",
        "filename",
        "subfolder",
        "storage_type",
        "media_type",
        "digest",
        "created_at",
        "mime_type",
    ),
    "legacy_resource_aliases": (
        "alias_uri",
        "canonical_uri",
        "object_kind",
        "workflow_id",
        "asset_id",
        "job_id",
        "artifact_id",
        "created_at",
    ),
}
_PRIMARY_KEY_INDEXES: dict[str, tuple[int, ...]] = {
    "assets": (0,),
    "jobs": (0,),
    "execution_attempts": (0,),
    "idempotency_records": (0, 1, 2),
    "artifacts": (0,),
    "legacy_resource_aliases": (0,),
}


@dataclass(frozen=True, slots=True)
class FrozenLegacyFile:
    """One exact backup byte sequence bound to its manifest metadata."""

    entry: ManifestEntry
    raw: bytes


@dataclass(frozen=True, slots=True)
class G1TableProjection:
    """Canonical rows for one production table, in primary-key order."""

    table: str
    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]

    def to_digest_value(self) -> dict[str, object]:
        return {"table": self.table, "columns": self.columns, "rows": self.rows}


@dataclass(frozen=True, slots=True)
class G1ImportPlan:
    """Immutable G1 plan derived only from a verified frozen backup."""

    aggregate: G1Aggregate
    version: int
    source_root: Path
    backup_root: Path
    manifest: MigrationManifest
    files: tuple[FrozenLegacyFile, ...]
    selected_entries: tuple[ManifestEntry, ...]
    tables: tuple[G1TableProjection, ...]
    source_counts: tuple[tuple[str, int], ...]
    projection_digest: str
    checksum: str

    def validate_integrity(self) -> None:
        if self.aggregate not in _TABLE_ORDER or self.version != _G1_VERSION:
            raise RehearsalFailure("G1 plan aggregate or version is invalid")
        self.manifest.validate_integrity()
        if not self.source_root.is_absolute() or not self.backup_root.is_absolute():
            raise RehearsalFailure("G1 plan roots must be absolute")
        if tuple(item.entry for item in self.files) != self.manifest.entries:
            raise RehearsalFailure("G1 plan files do not match its manifest")
        for item in self.files:
            if len(item.raw) != item.entry.size_bytes or not hmac.compare_digest(
                hashlib.sha256(item.raw).hexdigest(), item.entry.sha256
            ):
                raise RehearsalFailure(
                    f"G1 plan frozen bytes are invalid: {item.entry.relative_path}"
                )
        manifest_entries = set(self.manifest.entries)
        if any(entry not in manifest_entries for entry in self.selected_entries):
            raise RehearsalFailure("G1 plan selected entries are not in its manifest")
        expected_tables = _TABLE_ORDER[self.aggregate]
        if tuple(table.table for table in self.tables) != expected_tables:
            raise RehearsalFailure("G1 plan table group is incomplete or out of order")
        for table in self.tables:
            if table.columns != _TABLE_COLUMNS[table.table]:
                raise RehearsalFailure(f"G1 plan columns are invalid: {table.table}")
            key_indexes = _PRIMARY_KEY_INDEXES[table.table]
            keys = [tuple(row[index] for index in key_indexes) for row in table.rows]
            if keys != sorted(keys) or len(keys) != len(set(keys)):
                raise RehearsalFailure(f"G1 plan keys are not unique and sorted: {table.table}")
            if any(len(row) != len(table.columns) for row in table.rows):
                raise RehearsalFailure(f"G1 plan row width is invalid: {table.table}")
        projection_digest = _projection_digest(self.tables)
        if not hmac.compare_digest(projection_digest, self.projection_digest):
            raise RehearsalFailure("G1 plan projection digest is invalid")
        checksum = _plan_checksum(
            self.aggregate,
            self.version,
            self.selected_entries,
            self.projection_digest,
        )
        if not hmac.compare_digest(checksum, self.checksum):
            raise RehearsalFailure("G1 plan checksum is invalid")


@dataclass(frozen=True, slots=True)
class G1CutoverResult:
    outcome: Literal["switched", "already_switched"]
    aggregate: G1Aggregate
    version: int
    checksum: str
    manifest_digest: str
    projection_digest: str
    imported: int
    reused: int
    counts: tuple[tuple[str, int], ...]
    switched_at: str


@dataclass(frozen=True, slots=True)
class _JobCandidate:
    job_id: str
    workflow_id: str
    owner_id: str
    status: str
    error: str
    server_id: str
    prompt_id: str | None
    client_id: str
    created_at: str
    created_at_ns: int
    created_at_source: str


class _PlanBuilder:
    def __init__(
        self,
        source_root: Path,
        backup_root: Path,
        manifest: MigrationManifest,
        files: tuple[FrozenLegacyFile, ...],
        aggregate: G1Aggregate,
    ) -> None:
        self.source_root = source_root
        self.backup_root = backup_root
        self.manifest = manifest
        self.files = files
        self.aggregate = aggregate
        self.parsed: dict[str, dict[str, object]] = {}

    def build(self) -> G1ImportPlan:
        for item in self.files:
            if not _entry_belongs_to_aggregate(item.entry.relative_path, self.aggregate):
                continue
            try:
                self.parsed[item.entry.relative_path] = _load_json_object(item.raw)
            except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
                raise RehearsalFailure(
                    f"invalid frozen JSON for {item.entry.relative_path}: {exc}"
                ) from exc
        selected = tuple(
            item.entry
            for item in self.files
            if _entry_belongs_to_aggregate(item.entry.relative_path, self.aggregate)
        )
        if self.aggregate == "asset":
            tables, counts = self._build_assets()
        else:
            tables, counts = self._build_jobs()
        projection_digest = _projection_digest(tables)
        checksum = _plan_checksum(self.aggregate, _G1_VERSION, selected, projection_digest)
        plan = G1ImportPlan(
            aggregate=self.aggregate,
            version=_G1_VERSION,
            source_root=self.source_root,
            backup_root=self.backup_root,
            manifest=self.manifest,
            files=self.files,
            selected_entries=selected,
            tables=tables,
            source_counts=counts,
            projection_digest=projection_digest,
            checksum=checksum,
        )
        plan.validate_integrity()
        return plan

    def _build_assets(
        self,
    ) -> tuple[tuple[G1TableProjection, ...], tuple[tuple[str, int], ...]]:
        rows: list[tuple[object, ...]] = []
        aliases: list[tuple[object, ...]] = []
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
        for relative, value in sorted(self.parsed.items()):
            fields = set(value)
            if fields != expected:
                missing = sorted(expected - fields)
                unknown = sorted(fields - expected)
                raise RehearsalFailure(
                    f"legacy Asset fields cannot be preserved for {relative}: "
                    f"missing={missing}, unknown={unknown}"
                )
            try:
                asset_id = _string(value["asset_id"], "asset_id")
                if (
                    _ASSET_ID.fullmatch(asset_id) is None
                    or PurePosixPath(relative).stem != asset_id
                ):
                    raise ValueError("asset path and asset_id do not match")
                server_id = validate_identifier(value["server_id"], field="server_id")
                owner_id = _string(value["owner_id"], "owner_id", allow_empty=True)
                name = _string(value["name"], "name")
                subfolder = _string(value["subfolder"], "subfolder", allow_empty=True)
                comfyui_ref = _string(value["comfyui_ref"], "comfyui_ref")
                if comfyui_ref != (f"{subfolder}/{name}" if subfolder else name):
                    raise ValueError("comfyui_ref does not match subfolder/name")
                media_type = _enum(value["media_type"], "media_type", {"image", "audio", "video"})
                mime_type = _string(value["mime_type"], "mime_type")
                size_bytes = value["size_bytes"]
                if (
                    isinstance(size_bytes, bool)
                    or not isinstance(size_bytes, int)
                    or size_bytes < 0
                ):
                    raise ValueError("size_bytes must be a non-negative integer")
                digest = _digest(value["sha256"], "sha256")
                created_at, _ = _timestamp(value["created_at"], "created_at")
            except (TypeError, ValueError) as exc:
                raise RehearsalFailure(f"invalid legacy Asset {relative}: {exc}") from exc
            rows.append(
                (
                    asset_id,
                    owner_id,
                    server_id,
                    name,
                    subfolder,
                    media_type,
                    mime_type,
                    size_bytes,
                    digest,
                    "legacy_upload",
                    comfyui_ref,
                    created_at,
                    None,
                )
            )
            aliases.append(
                (
                    f"comfyui://assets/{server_id}/{asset_id}",
                    canonical_resource_uri("asset", asset_id),
                    "asset",
                    None,
                    asset_id,
                    None,
                    None,
                    created_at,
                )
            )
        tables = (
            _table("assets", rows),
            _table("legacy_resource_aliases", aliases),
        )
        return tables, (("asset_files", len(rows)),)

    def _build_jobs(
        self,
    ) -> tuple[tuple[G1TableProjection, ...], tuple[tuple[str, int], ...]]:
        jobs: dict[str, _JobCandidate] = {}
        attempts: dict[str, tuple[object, ...]] = {}
        idempotency: dict[tuple[str, str, str], tuple[object, ...]] = {}
        aliases: dict[str, tuple[object, ...]] = {}
        source_counts = {"mcp_prompt_files": 0, "mcp_idempotency_files": 0, "cli_history_files": 0}

        entry_by_path = {item.entry.relative_path: item.entry for item in self.files}
        for relative, value in sorted(self.parsed.items()):
            entry = entry_by_path[relative]
            parts = PurePosixPath(relative).parts
            try:
                if len(parts) == 5 and parts[:2] == ("data", "runs"):
                    collection = parts[3]
                    if collection == "prompts":
                        source_counts["mcp_prompt_files"] += 1
                    elif collection == "idempotency":
                        source_counts["mcp_idempotency_files"] += 1
                    else:
                        raise ValueError("unknown MCP run collection")
                    self._consume_mcp(
                        relative,
                        parts,
                        collection,
                        value,
                        entry,
                        jobs,
                        attempts,
                        idempotency,
                        aliases,
                    )
                elif len(parts) == 5 and parts[0] == "data" and parts[3] == "history":
                    source_counts["cli_history_files"] += 1
                    self._consume_cli(
                        relative,
                        parts,
                        value,
                        entry,
                        jobs,
                        attempts,
                        idempotency,
                        aliases,
                    )
                else:
                    raise ValueError("unrecognized Job source path")
            except (TypeError, ValueError) as exc:
                raise RehearsalFailure(f"invalid legacy Job source {relative}: {exc}") from exc

        job_rows = [
            (
                item.job_id,
                item.workflow_id,
                None,
                None,
                None,
                item.owner_id,
                item.status,
                item.error,
                "[]",
                None,
                item.created_at,
                item.created_at_source,
                1,
                "legacy_migrated",
            )
            for item in jobs.values()
        ]
        resolved_jobs: dict[str, tuple[str, str, str]] = {}
        for key, row in idempotency.items():
            if row[4] != "resolved":
                continue
            job_id = str(row[5])
            previous = resolved_jobs.get(job_id)
            if previous is not None and previous != key:
                raise RehearsalFailure(
                    f"multiple resolved idempotency keys target migrated Job {job_id}"
                )
            resolved_jobs[job_id] = key

        tables = (
            _table("jobs", job_rows),
            _table("execution_attempts", attempts.values()),
            _table("idempotency_records", idempotency.values()),
            _table("artifacts", ()),
            _table("legacy_resource_aliases", aliases.values()),
        )
        counts = tuple((name, source_counts[name]) for name in sorted(source_counts))
        return tables, counts

    def _consume_mcp(
        self,
        relative: str,
        parts: tuple[str, ...],
        collection: str,
        value: dict[str, object],
        entry: ManifestEntry,
        jobs: dict[str, _JobCandidate],
        attempts: dict[str, tuple[object, ...]],
        idempotency: dict[tuple[str, str, str], tuple[object, ...]],
        aliases: dict[str, tuple[object, ...]],
    ) -> None:
        server_id = validate_identifier(value.get("server_id"), field="server_id")
        if parts[2] != hashlib.sha256(server_id.encode()).hexdigest():
            raise ValueError("MCP server directory hash does not match server_id")
        workflow_id = validate_identifier(value.get("workflow_id"), field="workflow_id")
        owner_id = _string(value.get("owner_id", ""), "owner_id", allow_empty=True)
        status = _status(value.get("status"))
        digest = _digest(value.get("request_digest"), "request_digest")
        prompt_id = _optional_identifier(value.get("prompt_id"), "prompt_id")
        idempotency_key = _string(
            value.get("idempotency_key", ""), "idempotency_key", allow_empty=True
        )
        client_id = _string(value.get("client_id", ""), "client_id", allow_empty=True)
        error = _string(value.get("error", ""), "error", allow_empty=True)
        _reject_outputs(value, relative)
        if collection == "prompts":
            if prompt_id is None:
                raise ValueError("prompt source requires prompt_id")
            if PurePosixPath(relative).stem != hashlib.sha256(prompt_id.encode()).hexdigest():
                raise ValueError("MCP prompt filename hash does not match prompt_id")
        else:
            if not idempotency_key:
                raise ValueError("idempotency source requires idempotency_key")
            expected = hashlib.sha256(f"{owner_id}\0{idempotency_key}".encode()).hexdigest()
            if PurePosixPath(relative).stem != expected:
                raise ValueError("MCP idempotency filename hash does not match its key")

        fallback_time, fallback_ns = _mtime_timestamp(entry)
        claimed_at = _claimed_at(value.get("claimed_at"), fallback_time)
        if status == "reserved":
            if prompt_id is not None:
                raise ValueError("reserved record must not contain prompt_id")
            claim_ns = _claimed_at_ns(value.get("claimed_at"), fallback_ns)
            age_ns = self.manifest.captured_at_ns - claim_ns
            if age_ns < 0 or age_ns <= 300_000_000_000:
                raise RehearsalFailure(f"active reservation blocks migration: {relative}")
            self._merge_idempotency(
                idempotency,
                (
                    owner_id,
                    _scope(server_id),
                    idempotency_key,
                    digest,
                    "expired",
                    None,
                    client_id,
                    claimed_at,
                    None,
                    None,
                    workflow_id,
                ),
                relative,
            )
            return

        if prompt_id is None:
            if status != "submission_unknown" or not idempotency_key:
                raise ValueError("only submission_unknown may omit prompt_id")
            job_id = derive_legacy_unknown_job_id(owner_id, server_id, idempotency_key, digest)
            candidate = _JobCandidate(
                job_id,
                workflow_id,
                owner_id,
                status,
                error,
                server_id,
                None,
                client_id,
                fallback_time,
                fallback_ns,
                "legacy_file_mtime",
            )
            candidate = self._merge_job(jobs, candidate, relative)
            self._merge_attempt(attempts, candidate, None, relative)
            state = "submission_unknown"
        else:
            job_id = derive_legacy_job_id(server_id, prompt_id)
            candidate = _JobCandidate(
                job_id,
                workflow_id,
                owner_id,
                status,
                error,
                server_id,
                prompt_id,
                client_id,
                fallback_time,
                fallback_ns,
                "legacy_file_mtime",
            )
            candidate = self._merge_job(jobs, candidate, relative)
            self._merge_attempt(attempts, candidate, prompt_id, relative)
            self._merge_job_alias(aliases, server_id, prompt_id, candidate, relative)
            state = "resolved"
        if idempotency_key:
            self._merge_idempotency(
                idempotency,
                (
                    owner_id,
                    _scope(server_id),
                    idempotency_key,
                    digest,
                    state,
                    candidate.job_id,
                    client_id,
                    claimed_at,
                    None,
                    None,
                    workflow_id,
                ),
                relative,
            )

    def _consume_cli(
        self,
        relative: str,
        parts: tuple[str, ...],
        value: dict[str, object],
        entry: ManifestEntry,
        jobs: dict[str, _JobCandidate],
        attempts: dict[str, tuple[object, ...]],
        idempotency: dict[tuple[str, str, str], tuple[object, ...]],
        aliases: dict[str, tuple[object, ...]],
    ) -> None:
        server_id = validate_identifier(value.get("server_id"), field="server_id")
        workflow_id = validate_identifier(value.get("workflow_id"), field="workflow_id")
        if (parts[1], parts[2]) != (server_id, workflow_id):
            raise ValueError("CLI history directory does not match record identity")
        status = _status(value.get("status"))
        digest = _digest(value.get("request_digest"), "request_digest")
        args = value.get("args")
        if not isinstance(args, dict):
            raise ValueError("CLI history args must be an object")
        if not hmac.compare_digest(hashlib.sha256(_canonical_json(args)).hexdigest(), digest):
            raise ValueError("CLI request_digest does not match args")
        prompt_id = _optional_identifier(value.get("prompt_id"), "prompt_id")
        client_id = _string(value.get("client_id", ""), "client_id", allow_empty=True)
        error = _string(value.get("error", ""), "error", allow_empty=True)
        owner_id = ""
        _reject_outputs(value, relative)
        created_at, created_ns = _optional_timestamp_or_mtime(value.get("timestamp"), entry)
        stem = PurePosixPath(relative).stem
        if stem.startswith("job-"):
            external_id = validate_identifier(value.get("job_id"), field="job_id")
            if value.get("run_id") != external_id:
                raise ValueError("CLI job run_id does not match job_id")
            if stem != "job-" + hashlib.sha256(external_id.encode()).hexdigest():
                raise ValueError("CLI job filename hash does not match job_id")
            idempotency_key: str | None = external_id
        elif stem.startswith("prompt-"):
            if prompt_id is None:
                raise ValueError("CLI prompt history requires prompt_id")
            external_id = prompt_id
            if value.get("run_id") != external_id or value.get("job_id"):
                raise ValueError("CLI prompt history identity is inconsistent")
            if stem != "prompt-" + hashlib.sha256(external_id.encode()).hexdigest():
                raise ValueError("CLI prompt filename hash does not match prompt_id")
            idempotency_key = None
        else:
            raise ValueError("CLI history filename kind is invalid")

        if status == "reserved":
            if prompt_id is not None or idempotency_key is None:
                raise ValueError("reserved CLI history must be an unsubmitted job")
            age_ns = self.manifest.captured_at_ns - created_ns
            if age_ns < 0 or age_ns <= 300_000_000_000:
                raise RehearsalFailure(f"active reservation blocks migration: {relative}")
            self._merge_idempotency(
                idempotency,
                (
                    owner_id,
                    _scope(server_id),
                    idempotency_key,
                    digest,
                    "expired",
                    None,
                    client_id,
                    created_at,
                    None,
                    None,
                    workflow_id,
                ),
                relative,
            )
            return

        if prompt_id is None:
            if status != "submission_unknown" or idempotency_key is None:
                raise ValueError("only submission_unknown CLI jobs may omit prompt_id")
            job_id = derive_legacy_unknown_job_id(owner_id, server_id, idempotency_key, digest)
            candidate = _JobCandidate(
                job_id,
                workflow_id,
                owner_id,
                status,
                error,
                server_id,
                None,
                client_id,
                created_at,
                created_ns,
                "legacy_timestamp",
            )
            candidate = self._merge_job(jobs, candidate, relative)
            self._merge_attempt(attempts, candidate, None, relative)
            state = "submission_unknown"
        else:
            job_id = derive_legacy_job_id(server_id, prompt_id)
            candidate = _JobCandidate(
                job_id,
                workflow_id,
                owner_id,
                status,
                error,
                server_id,
                prompt_id,
                client_id,
                created_at,
                created_ns,
                "legacy_timestamp",
            )
            candidate = self._merge_job(jobs, candidate, relative)
            self._merge_attempt(attempts, candidate, prompt_id, relative)
            self._merge_job_alias(aliases, server_id, prompt_id, candidate, relative)
            state = "resolved"
        self._merge_job_alias(aliases, server_id, external_id, candidate, relative)
        if idempotency_key is not None:
            self._merge_idempotency(
                idempotency,
                (
                    owner_id,
                    _scope(server_id),
                    idempotency_key,
                    digest,
                    state,
                    candidate.job_id,
                    client_id,
                    created_at,
                    None,
                    None,
                    workflow_id,
                ),
                relative,
            )

    @staticmethod
    def _merge_job(
        jobs: dict[str, _JobCandidate], candidate: _JobCandidate, relative: str
    ) -> _JobCandidate:
        existing = jobs.get(candidate.job_id)
        if existing is None:
            jobs[candidate.job_id] = candidate
            return candidate
        comparable_existing = (
            existing.workflow_id,
            existing.owner_id,
            existing.status,
            existing.error,
            existing.server_id,
            existing.prompt_id,
        )
        comparable_candidate = (
            candidate.workflow_id,
            candidate.owner_id,
            candidate.status,
            candidate.error,
            candidate.server_id,
            candidate.prompt_id,
        )
        if comparable_existing != comparable_candidate:
            raise ValueError(f"conflicting normalized Job facts for {candidate.job_id}: {relative}")
        if existing.client_id and candidate.client_id and existing.client_id != candidate.client_id:
            raise ValueError(f"conflicting Job client_id for {candidate.job_id}: {relative}")
        chosen = existing if existing.created_at_ns <= candidate.created_at_ns else candidate
        client_id = existing.client_id or candidate.client_id
        if chosen.client_id != client_id:
            chosen = replace(chosen, client_id=client_id)
        jobs[candidate.job_id] = chosen
        return chosen

    @staticmethod
    def _merge_attempt(
        attempts: dict[str, tuple[object, ...]],
        candidate: _JobCandidate,
        prompt_id: str | None,
        relative: str,
    ) -> None:
        attempt_id = derive_legacy_attempt_id(candidate.job_id, candidate.server_id, 1)
        row = (
            attempt_id,
            candidate.job_id,
            1,
            candidate.server_id,
            prompt_id,
            None,
            candidate.client_id,
            "submitted" if prompt_id is not None else "submission_unknown",
            candidate.created_at,
        )
        existing = attempts.get(attempt_id)
        if existing is not None and (existing[:6] != row[:6] or existing[7] != row[7]):
            raise ValueError(f"conflicting normalized Attempt facts: {relative}")
        attempts[attempt_id] = row

    @staticmethod
    def _merge_idempotency(
        records: dict[tuple[str, str, str], tuple[object, ...]],
        row: tuple[object, ...],
        relative: str,
    ) -> None:
        key = (str(row[0]), str(row[1]), str(row[2]))
        existing = records.get(key)
        if existing is not None:
            if existing[:7] + existing[8:] != row[:7] + row[8:]:
                raise ValueError(f"conflicting normalized idempotency facts: {relative}")
            row = existing if str(existing[7]) <= str(row[7]) else row
        records[key] = row

    @staticmethod
    def _merge_job_alias(
        aliases: dict[str, tuple[object, ...]],
        server_id: str,
        external_id: str,
        candidate: _JobCandidate,
        relative: str,
    ) -> None:
        alias_uri = f"comfyui://jobs/{server_id}/{external_id}"
        row = (
            alias_uri,
            canonical_resource_uri("job", candidate.job_id),
            "job",
            None,
            None,
            candidate.job_id,
            None,
            candidate.created_at,
        )
        existing = aliases.get(alias_uri)
        if existing is not None and existing[:7] != row[:7]:
            raise ValueError(f"conflicting legacy Job alias: {relative}")
        aliases[alias_uri] = row


def build_g1_import_plan(
    *,
    source_root: Path,
    backup_root: Path,
    manifest: MigrationManifest,
    files: tuple[FrozenLegacyFile, ...],
    aggregate: G1Aggregate,
) -> G1ImportPlan:
    """Build one deterministic aggregate plan from previously verified bytes."""
    if aggregate not in _TABLE_ORDER:
        raise ValueError("G1 aggregate must be 'asset' or 'job'")
    return _PlanBuilder(
        source_root.resolve(strict=False),
        backup_root.resolve(strict=False),
        manifest,
        files,
        aggregate,
    ).build()


def cutover_g1_import_plan(
    plan: G1ImportPlan,
    store: SQLiteControlPlaneStore,
    *,
    verify_evidence: Callable[[], None],
    failure_injector: FailureInjector | None = None,
) -> G1CutoverResult:
    """Import, verify, and switch one G1 aggregate in a single transaction."""
    if not isinstance(plan, G1ImportPlan):
        raise TypeError("plan must be a G1ImportPlan")
    if not isinstance(store, SQLiteControlPlaneStore):
        raise TypeError("store must be an explicitly supplied SQLiteControlPlaneStore")
    plan.validate_integrity()
    rebuilt = build_g1_import_plan(
        source_root=plan.source_root,
        backup_root=plan.backup_root,
        manifest=plan.manifest,
        files=plan.files,
        aggregate=plan.aggregate,
    )
    if rebuilt != plan:
        raise RehearsalFailure("G1 plan differs from its frozen source projection")
    if not store.path.exists() or not store.path.is_file():
        raise RehearsalFailure("G1 cutover requires an initialized SQLite database")
    inject = failure_injector or (lambda _phase: None)
    connection = sqlite3.connect(store.path, isolation_level=None, timeout=5.0)
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
        _require_g1_schema(connection)
        state, switched_at = _check_switch_group(connection, plan)
        inject("after_switch_check")
        if state == "already_switched":
            _verify_database_projection(connection, plan)
            inject("after_projection")
            _verify_foreign_keys(connection)
            verify_evidence()
            inject("before_commit")
            connection.commit()
            transaction_started = False
            return G1CutoverResult(
                "already_switched",
                plan.aggregate,
                plan.version,
                plan.checksum,
                plan.manifest.digest,
                plan.projection_digest,
                0,
                sum(len(table.rows) for table in plan.tables),
                tuple((table.table, len(table.rows)) for table in plan.tables),
                switched_at,
            )

        for table in plan.tables:
            table_imported, table_reused = _import_table(connection, table)
            imported += table_imported
            reused += table_reused
            inject(f"after_{table.table}")
        inject("after_import")
        _verify_database_projection(connection, plan)
        inject("after_projection")
        _verify_foreign_keys(connection)
        verify_evidence()
        inject("before_switch")
        switched_at = _utc_now()
        _write_switch_group(connection, plan, switched_at)
        inject("after_switch")
        verify_evidence()
        inject("before_commit")
        connection.commit()
        transaction_started = False
    except BaseException:
        if transaction_started:
            connection.rollback()
        raise
    finally:
        connection.close()
    return G1CutoverResult(
        "switched",
        plan.aggregate,
        plan.version,
        plan.checksum,
        plan.manifest.digest,
        plan.projection_digest,
        imported,
        reused,
        tuple((table.table, len(table.rows)) for table in plan.tables),
        switched_at,
    )


def _require_g1_schema(connection: sqlite3.Connection) -> None:
    version = connection.execute("SELECT max(version) FROM schema_migrations").fetchone()
    if version is None or version[0] != 2:
        raise RehearsalFailure("G1 cutover requires initialized SQLite schema version 2")
    required_columns = {
        "jobs": {"error", "outputs_json", "execution_origin"},
        "idempotency_records": {"lease_token", "workflow_id"},
        "artifacts": {"mime_type"},
    }
    for table, expected in required_columns.items():
        actual = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        if not expected <= actual:
            raise RehearsalFailure(f"G1 schema columns are missing from {table}")


def _check_switch_group(
    connection: sqlite3.Connection, plan: G1ImportPlan
) -> tuple[Literal["new", "already_switched"], str]:
    group = _switch_group(plan.aggregate)
    placeholders = ",".join("?" for _ in group)
    rows = connection.execute(
        f"""
        SELECT aggregate_kind, version, status, checksum, switched_at
        FROM store_migrations
        WHERE aggregate_kind IN ({placeholders})
        ORDER BY aggregate_kind, version
        """,
        group,
    ).fetchall()
    switched = [row for row in rows if str(row[2]) == "switched"]
    if switched:
        if len(switched) != len(group) or {str(row[0]) for row in switched} != set(group):
            raise RehearsalFailure("partial G1 switch group detected")
        if len(rows) != len(group):
            raise RehearsalFailure("G1 switch group contains conflicting versions")
        evidence = {(int(row[1]), str(row[3]), str(row[4])) for row in switched}
        if len(evidence) != 1:
            raise RehearsalFailure("partial G1 switch evidence is inconsistent")
        version, checksum, switched_at = next(iter(evidence))
        if version != plan.version or not hmac.compare_digest(checksum, plan.checksum):
            raise RehearsalFailure("existing G1 switch checksum or version differs")
        return "already_switched", switched_at
    for row in rows:
        if int(row[1]) != plan.version or not hmac.compare_digest(str(row[3]), plan.checksum):
            raise RehearsalFailure("existing G1 migration checksum or version differs")
        if str(row[2]) == "superseded" or row[4] is not None:
            raise RehearsalFailure("existing G1 migration state fails closed")
    return "new", ""


def _write_switch_group(
    connection: sqlite3.Connection, plan: G1ImportPlan, switched_at: str
) -> None:
    for kind in _switch_group(plan.aggregate):
        row = connection.execute(
            """
            SELECT status, checksum, switched_at FROM store_migrations
            WHERE aggregate_kind = ? AND version = ?
            """,
            (kind, plan.version),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO store_migrations(
                    aggregate_kind, version, status, checksum, switched_at
                ) VALUES (?, ?, 'switched', ?, ?)
                """,
                (kind, plan.version, plan.checksum, switched_at),
            )
        else:
            if str(row[0]) not in {"pending", "migrating", "failed"}:
                raise RehearsalFailure("G1 migration state cannot transition to switched")
            if not hmac.compare_digest(str(row[1]), plan.checksum) or row[2] is not None:
                raise RehearsalFailure("G1 migration evidence changed before switch")
            updated = connection.execute(
                """
                UPDATE store_migrations SET status = 'switched', switched_at = ?
                WHERE aggregate_kind = ? AND version = ?
                  AND status = ? AND checksum = ? AND switched_at IS NULL
                """,
                (switched_at, kind, plan.version, str(row[0]), plan.checksum),
            ).rowcount
            if updated != 1:
                raise RehearsalFailure("G1 migration state changed during switch")


def _import_table(connection: sqlite3.Connection, projection: G1TableProjection) -> tuple[int, int]:
    columns_sql = ", ".join(projection.columns)
    values_sql = ", ".join("?" for _ in projection.columns)
    imported = 0
    reused = 0
    for row in projection.rows:
        existing = _select_row(connection, projection, row)
        if existing is None:
            connection.execute(
                f"INSERT INTO {projection.table}({columns_sql}) VALUES ({values_sql})", row
            )
            imported += 1
        elif tuple(existing) == row:
            reused += 1
        else:
            raise RehearsalFailure(f"database row conflicts with G1 projection: {projection.table}")
    return imported, reused


def _select_row(
    connection: sqlite3.Connection,
    projection: G1TableProjection,
    row: tuple[object, ...],
) -> sqlite3.Row | None:
    key_indexes = _PRIMARY_KEY_INDEXES[projection.table]
    predicate = " AND ".join(f"{projection.columns[index]} = ?" for index in key_indexes)
    values = tuple(row[index] for index in key_indexes)
    columns_sql = ", ".join(projection.columns)
    return connection.execute(
        f"SELECT {columns_sql} FROM {projection.table} WHERE {predicate}", values
    ).fetchone()


def _verify_database_projection(connection: sqlite3.Connection, plan: G1ImportPlan) -> None:
    actual_tables: list[G1TableProjection] = []
    for table in plan.tables:
        rows: list[tuple[object, ...]] = []
        for expected in table.rows:
            actual = _select_row(connection, table, expected)
            if actual is None or tuple(actual) != expected:
                raise RehearsalFailure(f"database projection mismatch in {table.table}")
            rows.append(tuple(actual))
        if len(rows) != len(table.rows):
            raise RehearsalFailure(f"database projection count mismatch in {table.table}")
        actual_tables.append(G1TableProjection(table.table, table.columns, tuple(rows)))
    _verify_related_id_sets(connection, plan)
    actual_digest = _projection_digest(tuple(actual_tables))
    if not hmac.compare_digest(actual_digest, plan.projection_digest):
        raise RehearsalFailure("database projection checksum mismatch")


def _verify_related_id_sets(connection: sqlite3.Connection, plan: G1ImportPlan) -> None:
    projections = {table.table: table for table in plan.tables}
    if plan.aggregate == "asset":
        asset_ids = tuple(str(row[0]) for row in projections["assets"].rows)
        _verify_related_rows(
            connection,
            projections["legacy_resource_aliases"],
            "asset_id",
            asset_ids,
        )
        return
    job_ids = tuple(str(row[0]) for row in projections["jobs"].rows)
    for table_name, column in (
        ("execution_attempts", "job_id"),
        ("idempotency_records", "job_id"),
        ("artifacts", "job_id"),
        ("legacy_resource_aliases", "job_id"),
    ):
        _verify_related_rows(connection, projections[table_name], column, job_ids)


def _verify_related_rows(
    connection: sqlite3.Connection,
    projection: G1TableProjection,
    relation_column: str,
    identifiers: tuple[str, ...],
) -> None:
    if not identifiers:
        return
    identifier_set = set(identifiers)
    relation_index = projection.columns.index(relation_column)
    expected = tuple(row for row in projection.rows if row[relation_index] in identifier_set)
    columns_sql = ", ".join(projection.columns)
    actual: list[tuple[object, ...]] = []
    for start in range(0, len(identifiers), 400):
        batch = identifiers[start : start + 400]
        placeholders = ", ".join("?" for _ in batch)
        rows = connection.execute(
            f"SELECT {columns_sql} FROM {projection.table} "
            f"WHERE {relation_column} IN ({placeholders})",
            batch,
        ).fetchall()
        actual.extend(tuple(row) for row in rows)
    key_indexes = _PRIMARY_KEY_INDEXES[projection.table]
    ordered_actual = tuple(
        sorted(actual, key=lambda row: tuple(row[index] for index in key_indexes))
    )
    if ordered_actual != expected:
        raise RehearsalFailure(f"database related ID set mismatch in {projection.table}")


def _verify_foreign_keys(connection: sqlite3.Connection) -> None:
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RehearsalFailure("G1 projection violates SQLite foreign keys")


def _table(table: str, rows: Iterable[tuple[object, ...]]) -> G1TableProjection:
    materialized = tuple(tuple(row) for row in rows)
    key_indexes = _PRIMARY_KEY_INDEXES[table]
    ordered = tuple(
        sorted(materialized, key=lambda row: tuple(row[index] for index in key_indexes))
    )
    return G1TableProjection(table, _TABLE_COLUMNS[table], ordered)


def _projection_digest(tables: tuple[G1TableProjection, ...]) -> str:
    return hashlib.sha256(
        _canonical_json([table.to_digest_value() for table in tables])
    ).hexdigest()


def _plan_checksum(
    aggregate: str,
    version: int,
    entries: tuple[ManifestEntry, ...],
    projection_digest: str,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "namespace": f"g1-{aggregate}-v{version}",
                "aggregate": aggregate,
                "version": version,
                "entries": [entry.to_dict() for entry in entries],
                "projection_digest": projection_digest,
            }
        )
    ).hexdigest()


def _entry_belongs_to_aggregate(path: str, aggregate: str) -> bool:
    parts = PurePosixPath(path).parts
    if aggregate == "asset":
        return len(parts) == 3 and parts[:2] == ("data", "assets") and path.endswith(".json")
    return (
        len(parts) == 5
        and (
            (parts[:2] == ("data", "runs") and parts[3] in {"prompts", "idempotency"})
            or (parts[0] == "data" and parts[3] == "history")
        )
        and path.endswith(".json")
    )


def _switch_group(aggregate: str) -> tuple[str, ...]:
    return _ASSET_SWITCH_GROUP if aggregate == "asset" else _JOB_SWITCH_GROUP


def _scope(server_id: str) -> str:
    return f"legacy-execute:{server_id}"


def _string(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value or (not allow_empty and not value):
        qualifier = "valid" if allow_empty else "non-empty"
        raise ValueError(f"{field} must be a {qualifier} string")
    if len(value) > 4096:
        raise ValueError(f"{field} exceeds the migration length limit")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _enum(value: object, field: str, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{field} has an unsupported value")
    return value


def _status(value: object) -> str:
    if not isinstance(value, str) or value not in _ALLOWED_STATUSES:
        raise ValueError("status has an unsupported value")
    return value


def _optional_identifier(value: object, field: str) -> str | None:
    if value in {None, ""}:
        return None
    return validate_identifier(value, field=field)


def _timestamp(value: object, field: str) -> tuple[str, int]:
    text = _string(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    normalized = parsed.astimezone(timezone.utc)
    nanoseconds = int(normalized.timestamp() * 1_000_000_000)
    return normalized.isoformat(), nanoseconds


def _optional_timestamp_or_mtime(value: object, entry: ManifestEntry) -> tuple[str, int]:
    if value in {None, ""}:
        return _mtime_timestamp(entry)
    return _timestamp(value, "timestamp")


def _mtime_timestamp(entry: ManifestEntry) -> tuple[str, int]:
    seconds, nanoseconds = divmod(entry.mtime_ns, 1_000_000_000)
    parsed = datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=nanoseconds // 1000)
    return parsed.isoformat(), entry.mtime_ns


def _claimed_at(value: object, fallback: str) -> str:
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("claimed_at must be a finite Unix timestamp")
    try:
        parsed = datetime.fromtimestamp(float(value), timezone.utc)
    except (ValueError, OSError, OverflowError) as exc:
        raise ValueError("claimed_at must be a finite Unix timestamp") from exc
    return parsed.isoformat()


def _claimed_at_ns(value: object, fallback_ns: int) -> int:
    if value is None:
        return fallback_ns
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("claimed_at must be a finite Unix timestamp")
    nanoseconds = int(float(value) * 1_000_000_000)
    if nanoseconds < 0 or nanoseconds > 2**63 - 1:
        raise ValueError("claimed_at exceeds the migration timestamp range")
    return nanoseconds


def _reject_outputs(value: dict[str, object], relative: str) -> None:
    outputs = value.get("outputs")
    if outputs:
        raise RehearsalFailure(
            f"legacy outputs cannot produce deterministic Artifact facts: {relative}"
        )
    if outputs is not None and not isinstance(outputs, list):
        raise ValueError("outputs must be a list when present")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
