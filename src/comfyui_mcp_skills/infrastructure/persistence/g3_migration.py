"""Deterministic legacy Workflow import and atomic G3 cutover."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from comfyui_mcp_skills.domain.control_plane import (
    derive_legacy_revision_id,
    derived_control_plane_id,
)
from comfyui_mcp_skills.domain.identifiers import validate_identifier
from comfyui_mcp_skills.domain.workflow_schema import (
    build_input_schema,
    normalize_parameters,
    validate_parameter_targets,
)
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.file_migration import (
    _capture_open_file,
)

_G3_VERSION = 1
_SWITCH_GROUP = ("workflow", "revision", "deployment")
_LEGACY_CREATED_AT = "1970-01-01T00:00:00+00:00"
_MAX_WORKFLOW_FILE_BYTES = 2 * 1024 * 1024
FailureInjector = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class G3WorkflowRecord:
    workflow_id: str
    created_at: str


@dataclass(frozen=True, slots=True)
class G3RevisionRecord:
    revision_id: str
    workflow_id: str
    graph_json: str
    parameter_schema_json: str
    dependency_contract_json: str
    content_digest: str
    created_at: str


@dataclass(frozen=True, slots=True)
class G3DeploymentRecord:
    deployment_id: str
    workflow_id: str
    revision_id: str
    server_id: str
    enabled: int
    validation_status: str
    published: int
    created_at: str


@dataclass(frozen=True, slots=True)
class G3ImportPlan:
    """Canonical G3 rows derived entirely from legacy Workflow files."""

    source_root: Path
    version: int
    workflows: tuple[G3WorkflowRecord, ...]
    revisions: tuple[G3RevisionRecord, ...]
    deployments: tuple[G3DeploymentRecord, ...]
    checksum: str


@dataclass(frozen=True, slots=True)
class G3CutoverResult:
    outcome: Literal["switched", "already_switched"]
    version: int
    checksum: str
    imported: int
    reused: int
    switched_at: str


def build_g3_import_plan(base_dir: Path) -> G3ImportPlan:
    """Read legacy Workflow pairs and return a deterministic normalized projection."""
    source_root = base_dir.resolve()
    data_path = source_root / "data"
    if data_path.is_symlink():
        raise ValueError("legacy Workflow data root must not be a symbolic link")
    data_root = data_path.resolve()
    try:
        data_root.relative_to(source_root)
    except ValueError as exc:
        raise ValueError("legacy Workflow data root escapes project root") from exc
    workflow_rows: dict[str, G3WorkflowRecord] = {}
    revision_rows: dict[str, G3RevisionRecord] = {}
    deployment_rows: dict[str, G3DeploymentRecord] = {}

    if data_root.is_dir():
        for server_dir in sorted(data_root.iterdir(), key=lambda path: path.name):
            if server_dir.name in {"assets", "runs"}:
                continue
            if not server_dir.is_dir() and not server_dir.is_symlink():
                continue
            _require_safe_path(server_dir, data_root, directory=True)
            server_id = validate_identifier(server_dir.name, field="server_id")
            for workflow_dir in sorted(server_dir.iterdir(), key=lambda path: path.name):
                if not workflow_dir.is_dir() and not workflow_dir.is_symlink():
                    continue
                _require_safe_path(workflow_dir, data_root, directory=True)
                workflow_id = validate_identifier(workflow_dir.name, field="workflow_id")
                schema_path = workflow_dir / "schema.json"
                graph_path = workflow_dir / "workflow.json"
                if not schema_path.is_file() and not graph_path.is_file():
                    continue
                if not schema_path.is_file() or not graph_path.is_file():
                    raise ValueError(
                        f"legacy Workflow requires schema.json and workflow.json: "
                        f"{server_id}/{workflow_id}"
                    )
                schema = _load_json_object(schema_path, root=data_root)
                graph = _load_json_object(graph_path, root=data_root)
                parameters = normalize_parameters(schema)
                validate_parameter_targets(parameters, graph)
                build_input_schema(parameters)

                graph_json = _canonical_json_text(graph)
                schema_json = _canonical_json_text(schema)
                content_digest = hashlib.sha256(f"{graph_json}\n{schema_json}".encode()).hexdigest()
                revision_id = derive_legacy_revision_id(workflow_id, content_digest)
                deployment_id = derived_control_plane_id(
                    "deployment",
                    "legacy-deployment-v1",
                    [workflow_id, revision_id, server_id],
                )
                workflow_rows.setdefault(
                    workflow_id, G3WorkflowRecord(workflow_id, _LEGACY_CREATED_AT)
                )
                revision = G3RevisionRecord(
                    revision_id,
                    workflow_id,
                    graph_json,
                    schema_json,
                    "{}",
                    content_digest,
                    _LEGACY_CREATED_AT,
                )
                previous_revision = revision_rows.setdefault(revision_id, revision)
                if previous_revision != revision:
                    raise ValueError(f"conflicting legacy Workflow revision: {workflow_id}")
                deployment = G3DeploymentRecord(
                    deployment_id,
                    workflow_id,
                    revision_id,
                    server_id,
                    int(schema.get("enabled", True) is True),
                    "valid",
                    1,
                    _LEGACY_CREATED_AT,
                )
                previous_deployment = deployment_rows.setdefault(deployment_id, deployment)
                if previous_deployment != deployment:
                    raise ValueError(
                        f"conflicting legacy Workflow deployment: {server_id}/{workflow_id}"
                    )

    workflows = tuple(sorted(workflow_rows.values(), key=lambda row: row.workflow_id))
    revisions = tuple(sorted(revision_rows.values(), key=lambda row: row.revision_id))
    deployments = tuple(sorted(deployment_rows.values(), key=lambda row: row.deployment_id))
    checksum = _plan_checksum(workflows, revisions, deployments)
    return G3ImportPlan(
        source_root,
        _G3_VERSION,
        workflows,
        revisions,
        deployments,
        checksum,
    )


def cutover_g3_import_plan(
    plan: G3ImportPlan,
    store: SQLiteControlPlaneStore,
    *,
    failure_injector: FailureInjector | None = None,
) -> G3CutoverResult:
    """Import all three G3 aggregates and switch them in one transaction."""
    if not isinstance(plan, G3ImportPlan):
        raise TypeError("plan must be a G3ImportPlan")
    if not isinstance(store, SQLiteControlPlaneStore):
        raise TypeError("store must be an explicitly supplied SQLiteControlPlaneStore")
    expected_checksum = _plan_checksum(plan.workflows, plan.revisions, plan.deployments)
    if plan.version != _G3_VERSION or not hmac.compare_digest(plan.checksum, expected_checksum):
        raise ValueError("G3 import plan integrity check failed")
    if not store.path.is_file():
        raise RuntimeError("G3 cutover requires an initialized SQLite database")

    inject = failure_injector or (lambda _phase: None)
    connection = _connect(store)
    transaction_started = False
    imported = 0
    reused = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        transaction_started = True
        _require_schema(connection)
        state, switched_at = _check_switch_group(connection, plan)
        if state == "already_switched":
            _verify_projection(connection, plan)
            connection.commit()
            transaction_started = False
            return G3CutoverResult(
                "already_switched",
                plan.version,
                plan.checksum,
                0,
                len(plan.workflows) + len(plan.revisions) + len(plan.deployments),
                switched_at,
            )

        for table, columns, records in _projections(plan):
            table_imported, table_reused = _import_records(connection, table, columns, records)
            imported += table_imported
            reused += table_reused
        inject("after_import")
        _verify_projection(connection, plan)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("G3 projection violates SQLite foreign keys")
        switched_at = datetime.now(timezone.utc).isoformat()
        for aggregate_kind in _SWITCH_GROUP:
            connection.execute(
                """
                INSERT INTO store_migrations(
                    aggregate_kind, version, status, checksum, switched_at
                ) VALUES (?, ?, 'switched', ?, ?)
                """,
                (aggregate_kind, plan.version, plan.checksum, switched_at),
            )
        connection.commit()
        transaction_started = False
    except BaseException:
        if transaction_started:
            connection.rollback()
        raise
    finally:
        connection.close()
    return G3CutoverResult("switched", plan.version, plan.checksum, imported, reused, switched_at)


def _connect(store: SQLiteControlPlaneStore) -> sqlite3.Connection:
    connection = sqlite3.connect(store.path, isolation_level=None, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA trusted_schema = OFF")
    return connection


def _require_schema(connection: sqlite3.Connection) -> None:
    required = {"workflows", "workflow_revisions", "workflow_deployments", "store_migrations"}
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN (?, ?, ?, ?)",
        tuple(sorted(required)),
    ).fetchall()
    if {str(row[0]) for row in rows} != required:
        raise RuntimeError("G3 cutover requires initialized Workflow SQLite tables")


def _check_switch_group(
    connection: sqlite3.Connection, plan: G3ImportPlan
) -> tuple[Literal["new", "already_switched"], str]:
    rows = connection.execute(
        """
        SELECT aggregate_kind, version, status, checksum, switched_at
        FROM store_migrations
        WHERE aggregate_kind IN (?, ?, ?)
        ORDER BY aggregate_kind, version
        """,
        _SWITCH_GROUP,
    ).fetchall()
    switched = tuple(row for row in rows if str(row[2]) == "switched")
    if switched:
        if len(rows) != len(_SWITCH_GROUP) or len(switched) != len(_SWITCH_GROUP):
            raise RuntimeError("partial G3 switch group conflicts with requested cutover")
        if {str(row[0]) for row in switched} != set(_SWITCH_GROUP):
            raise RuntimeError("partial G3 switch group conflicts with requested cutover")
        evidence = {(int(row[1]), str(row[3]), str(row[4])) for row in switched}
        if len(evidence) != 1:
            raise RuntimeError("G3 switch evidence conflicts across aggregates")
        version, checksum, switched_at = next(iter(evidence))
        if version != plan.version or not hmac.compare_digest(checksum, plan.checksum):
            raise RuntimeError("existing G3 switch conflicts with import plan")
        return "already_switched", switched_at
    if rows:
        raise RuntimeError("existing G3 migration state conflicts with import plan")
    return "new", ""


def _projections(
    plan: G3ImportPlan,
) -> tuple[tuple[str, tuple[str, ...], tuple[object, ...]], ...]:
    return (
        ("workflows", ("workflow_id", "created_at"), plan.workflows),
        (
            "workflow_revisions",
            (
                "revision_id",
                "workflow_id",
                "graph_json",
                "parameter_schema_json",
                "dependency_contract_json",
                "content_digest",
                "created_at",
            ),
            plan.revisions,
        ),
        (
            "workflow_deployments",
            (
                "deployment_id",
                "workflow_id",
                "revision_id",
                "server_id",
                "enabled",
                "validation_status",
                "published",
                "created_at",
            ),
            plan.deployments,
        ),
    )


def _import_records(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    records: tuple[object, ...],
) -> tuple[int, int]:
    imported = 0
    reused = 0
    columns_sql = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    key = columns[0]
    for record in records:
        values = tuple(getattr(record, column) for column in columns)
        existing = connection.execute(
            f"SELECT {columns_sql} FROM {table} WHERE {key} = ?", (values[0],)
        ).fetchone()
        if existing is None:
            connection.execute(
                f"INSERT INTO {table}({columns_sql}) VALUES ({placeholders})", values
            )
            imported += 1
        elif tuple(existing) == values:
            reused += 1
        else:
            raise RuntimeError(f"database row conflicts with G3 projection: {table}")
    return imported, reused


def _verify_projection(connection: sqlite3.Connection, plan: G3ImportPlan) -> None:
    for table, columns, records in _projections(plan):
        columns_sql = ", ".join(columns)
        key = columns[0]
        for record in records:
            values = tuple(getattr(record, column) for column in columns)
            actual = connection.execute(
                f"SELECT {columns_sql} FROM {table} WHERE {key} = ?", (values[0],)
            ).fetchone()
            if actual is None or tuple(actual) != values:
                raise RuntimeError(f"database projection conflicts in {table}")


def _plan_checksum(
    workflows: tuple[G3WorkflowRecord, ...],
    revisions: tuple[G3RevisionRecord, ...],
    deployments: tuple[G3DeploymentRecord, ...],
) -> str:
    payload = {
        "namespace": "g3-workflow-v1",
        "version": _G3_VERSION,
        "workflows": [[row.workflow_id, row.created_at] for row in workflows],
        "revisions": [
            [
                row.revision_id,
                row.workflow_id,
                row.graph_json,
                row.parameter_schema_json,
                row.dependency_contract_json,
                row.content_digest,
                row.created_at,
            ]
            for row in revisions
        ],
        "deployments": [
            [
                row.deployment_id,
                row.workflow_id,
                row.revision_id,
                row.server_id,
                row.enabled,
                row.validation_status,
                row.published,
                row.created_at,
            ]
            for row in deployments
        ],
    }
    return hashlib.sha256(_canonical_json_text(payload).encode("utf-8")).hexdigest()


def _load_json_object(path: Path, *, root: Path) -> dict[str, object]:
    safe = _require_safe_path(path, root, directory=False)
    relative = safe.relative_to(root).as_posix()
    try:
        captured = _capture_open_file(safe, root, relative)
        if captured.entry.size_bytes > _MAX_WORKFLOW_FILE_BYTES:
            raise ValueError(f"legacy Workflow JSON exceeds size limit: {path}")
        value = json.loads(captured.raw)
    except ValueError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read legacy Workflow JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"legacy Workflow JSON must be an object: {path}")
    return value


def _require_safe_path(path: Path, root: Path, *, directory: bool) -> Path:
    if path.is_symlink():
        raise ValueError(f"legacy Workflow path must not be a symbolic link: {path}")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"legacy Workflow path escapes data root: {path}") from exc
    if directory and not resolved.is_dir():
        raise ValueError(f"legacy Workflow directory is invalid: {path}")
    if not directory and not resolved.is_file():
        raise ValueError(f"legacy Workflow file is invalid: {path}")
    return resolved


def _canonical_json_text(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
