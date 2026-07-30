"""Isolated executable contract for the minimal Revision -> Plan -> Job slice."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from comfyui_mcp_skills.domain.control_plane import (
    canonical_resource_uri,
    derived_control_plane_id,
    new_control_plane_id,
    parse_legacy_resource_uri,
)
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore


class ContractHarnessFailure(RuntimeError):
    """The isolated contract could not complete atomically."""


@dataclass(frozen=True, slots=True)
class RevisionPlanJobEvidence:
    workflow_id: str
    revision_id: str
    deployment_id: str
    plan_id: str
    job_id: str
    revision_immutable: bool
    plan_immutable: bool
    job_bound_to_plan: bool
    legacy_alias_resolves: bool
    legacy_alias_immutable: bool
    production_switches_written: bool


class RevisionPlanJobContractHarness:
    """Materialize the minimum control-plane lineage in a new scratch database."""

    def __init__(self, database: str | Path) -> None:
        candidate = Path(database).absolute()
        _validate_new_database_path(candidate)
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(candidate, flags, 0o600)
        reserved_identity: tuple[int, int] | None = None
        cleanup_root: Path | None = None
        try:
            reserved_info = os.fstat(descriptor)
            reserved_identity = (reserved_info.st_dev, reserved_info.st_ino)
            os.close(descriptor)
            descriptor = -1
            self._nonce = secrets.token_hex(32)
            staging_root = Path(tempfile.mkdtemp(prefix=".contract-harness-", dir=candidate.parent))
            cleanup_root = staging_root
            if os.name != "nt":
                staging_root.chmod(0o700)
            staging_database = staging_root / candidate.name
            SQLiteControlPlaneStore(staging_database).initialize()
            connection = sqlite3.connect(staging_database, isolation_level=None, timeout=5.0)
            try:
                _configure(connection)
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO test_migration_database_role(singleton, role, nonce)
                    VALUES (1, 'g0_contract_harness', ?)
                    """,
                    (self._nonce,),
                )
                connection.commit()
            except BaseException:
                _rollback_preserving_error(connection)
                raise
            finally:
                _close_preserving_error(connection)
            if _file_identity(candidate) != reserved_identity:
                raise ContractHarnessFailure("reserved contract database identity changed")
            self.database = staging_database
            self._staging_root = staging_root
            self._identity = _file_identity(staging_database)
            cleanup_root = None
        except BaseException:
            if reserved_identity is None:
                _remove_exclusive_candidate(candidate)
            else:
                _remove_owned_database(candidate, reserved_identity)
            raise
        finally:
            if descriptor >= 0:
                _close_descriptor_preserving_error(descriptor)
            if cleanup_root is not None:
                shutil.rmtree(cleanup_root, ignore_errors=True)

    def run(
        self,
        *,
        graph: dict[str, object],
        resolved_inputs: dict[str, object],
        fail_before_commit: bool = False,
    ) -> RevisionPlanJobEvidence:
        graph_json = _canonical_json(graph)
        graph_snapshot = json.loads(graph_json)
        inputs_json = _canonical_json(resolved_inputs)
        inputs_snapshot = json.loads(inputs_json)
        workflow_id = "contract-workflow"
        content_digest = hashlib.sha256(
            _canonical_json_bytes(
                {
                    "graph": graph_snapshot,
                    "parameter_schema": {},
                    "dependency_contract": {},
                }
            )
        ).hexdigest()
        revision_id = derived_control_plane_id(
            "revision", "contract-harness-v1", [workflow_id, content_digest]
        )
        deployment_id = new_control_plane_id("deployment")
        input_digest = hashlib.sha256(inputs_json.encode("utf-8")).hexdigest()
        plan_digest = hashlib.sha256(
            _canonical_json_bytes([revision_id, deployment_id, "local", inputs_snapshot])
        ).hexdigest()
        plan_id = derived_control_plane_id("plan", "contract-harness-v1", [plan_digest])
        job_id = new_control_plane_id("job")
        created_at = _utc_now()
        revision_immutable = False
        plan_immutable = False
        if _file_identity(self.database) != self._identity:
            raise ContractHarnessFailure("isolated contract database identity changed")
        connection = sqlite3.connect(self.database, isolation_level=None, timeout=5.0)
        try:
            _configure(connection)
            marker = connection.execute(
                "SELECT role, nonce FROM test_migration_database_role WHERE singleton = 1"
            ).fetchone()
            if marker != ("g0_contract_harness", self._nonce):
                raise ContractHarnessFailure("database is not this isolated contract harness")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO workflows(workflow_id, created_at) VALUES (?, ?)",
                (workflow_id, created_at),
            )
            connection.execute(
                """
                INSERT INTO workflow_revisions(
                    revision_id, workflow_id, graph_json, parameter_schema_json,
                    dependency_contract_json, content_digest, created_at
                ) VALUES (?, ?, ?, '{}', '{}', ?, ?)
                """,
                (revision_id, workflow_id, graph_json, content_digest, created_at),
            )
            connection.execute(
                """
                INSERT INTO workflow_deployments(
                    deployment_id, workflow_id, revision_id, server_id, enabled,
                    validation_status, published, created_at
                ) VALUES (?, ?, ?, 'local', 1, 'valid', 1, ?)
                """,
                (deployment_id, workflow_id, revision_id, created_at),
            )
            connection.execute(
                """
                INSERT INTO execution_plans(
                    plan_id, workflow_id, revision_id, deployment_id, server_id,
                    resolved_inputs_json, input_digest, plan_digest, created_at
                ) VALUES (?, ?, ?, ?, 'local', ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    workflow_id,
                    revision_id,
                    deployment_id,
                    inputs_json,
                    input_digest,
                    plan_digest,
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, workflow_id, plan_id, revision_id, deployment_id,
                    owner_id, status, retry_of, created_at, created_at_source,
                    legacy_migrated, execution_origin
                ) VALUES (?, ?, ?, ?, ?, 'contract-owner', 'queued', NULL, ?, 'runtime', 0,
                          'planned')
                """,
                (job_id, workflow_id, plan_id, revision_id, deployment_id, created_at),
            )
            alias_uri = "comfyui://workflows/local/contract-workflow"
            connection.execute(
                """
                INSERT INTO legacy_resource_aliases(
                    alias_uri, canonical_uri, object_kind, workflow_id, created_at
                ) VALUES (?, ?, 'workflow', ?, ?)
                """,
                (
                    alias_uri,
                    canonical_resource_uri("workflow", workflow_id),
                    workflow_id,
                    created_at,
                ),
            )
            try:
                connection.execute(
                    "UPDATE workflow_revisions SET graph_json = '{}' WHERE revision_id = ?",
                    (revision_id,),
                )
            except sqlite3.IntegrityError:
                revision_immutable = True
            try:
                connection.execute(
                    "UPDATE execution_plans SET resolved_inputs_json = '{}' WHERE plan_id = ?",
                    (plan_id,),
                )
            except sqlite3.IntegrityError:
                plan_immutable = True
            if fail_before_commit:
                raise ContractHarnessFailure("injected failure before contract commit")
            connection.commit()
        except BaseException:
            _rollback_preserving_error(connection)
            raise
        finally:
            _close_preserving_error(connection)

        job_binding, legacy_ref, alias, alias_immutable, switches = _verify_contract(
            self.database,
            job_id=job_id,
            alias_uri=alias_uri,
        )
        return RevisionPlanJobEvidence(
            workflow_id=workflow_id,
            revision_id=revision_id,
            deployment_id=deployment_id,
            plan_id=plan_id,
            job_id=job_id,
            revision_immutable=revision_immutable,
            plan_immutable=plan_immutable,
            job_bound_to_plan=job_binding == (plan_id, revision_id, deployment_id, "local"),
            legacy_alias_resolves=legacy_ref is not None
            and alias == (canonical_resource_uri("workflow", workflow_id),),
            legacy_alias_immutable=alias_immutable,
            production_switches_written=switches != 0,
        )


def _verify_contract(
    database: Path, *, job_id: str, alias_uri: str
) -> tuple[tuple[object, ...] | None, object, tuple[object, ...] | None, bool, int]:
    verification = sqlite3.connect(database, isolation_level=None)
    try:
        _configure(verification)
        job_binding = verification.execute(
            """
            SELECT jobs.plan_id, jobs.revision_id, jobs.deployment_id,
                   execution_plans.server_id
            FROM jobs JOIN execution_plans USING(plan_id)
            WHERE jobs.job_id = ?
            """,
            (job_id,),
        ).fetchone()
        legacy_ref = parse_legacy_resource_uri(alias_uri)
        alias = verification.execute(
            "SELECT canonical_uri FROM legacy_resource_aliases WHERE alias_uri = ?",
            (alias_uri,),
        ).fetchone()
        alias_immutable = False
        try:
            verification.execute(
                "UPDATE legacy_resource_aliases SET canonical_uri = canonical_uri "
                "WHERE alias_uri = ?",
                (alias_uri,),
            )
        except sqlite3.IntegrityError:
            try:
                verification.execute(
                    "DELETE FROM legacy_resource_aliases WHERE alias_uri = ?",
                    (alias_uri,),
                )
            except sqlite3.IntegrityError:
                alias_immutable = True
        switches = int(verification.execute("SELECT count(*) FROM store_migrations").fetchone()[0])
        return job_binding, legacy_ref, alias, alias_immutable, switches
    finally:
        _close_preserving_error(verification)


def _validate_new_database_path(candidate: Path) -> None:
    if candidate.is_symlink() or candidate.exists():
        raise FileExistsError("contract harness requires a new isolated database")
    for parent in candidate.parents:
        if parent.is_symlink():
            raise ValueError("contract harness database path must not contain symbolic links")
    parent = candidate.parent
    if not parent.exists() or not parent.is_dir():
        raise ValueError("contract harness database parent must be an existing directory")
    if os.name != "nt":
        info = parent.stat()
        if info.st_uid != Path.home().stat().st_uid:
            raise ValueError("contract harness database parent must be owned by the current user")
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ValueError("contract harness database parent must be private")


def _file_identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


def _remove_exclusive_candidate(path: Path) -> None:
    active_error = sys.exc_info()[0] is not None
    try:
        path.unlink(missing_ok=True)
    except BaseException:
        if not active_error:
            raise


def _remove_owned_database(path: Path, identity: tuple[int, int]) -> None:
    active_error = sys.exc_info()[0] is not None
    try:
        if _file_identity(path) != identity:
            return
        for target in (
            path.with_name(f"{path.name}-wal"),
            path.with_name(f"{path.name}-shm"),
            path,
        ):
            target.unlink(missing_ok=True)
    except FileNotFoundError:
        return
    except BaseException:
        if not active_error:
            raise


def _close_descriptor_preserving_error(descriptor: int) -> None:
    active_error = sys.exc_info()[0] is not None
    try:
        os.close(descriptor)
    except BaseException:
        if not active_error:
            raise


def _rollback_preserving_error(connection: sqlite3.Connection) -> None:
    active_error = sys.exc_info()[0] is not None
    try:
        connection.rollback()
    except BaseException:
        if not active_error:
            raise


def _close_preserving_error(connection: sqlite3.Connection) -> None:
    active_error = sys.exc_info()[0] is not None
    try:
        connection.close()
    except BaseException:
        if not active_error:
            raise


def _configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA trusted_schema = OFF")


def _canonical_json(value: object) -> str:
    return _canonical_json_bytes(value).decode("utf-8")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
