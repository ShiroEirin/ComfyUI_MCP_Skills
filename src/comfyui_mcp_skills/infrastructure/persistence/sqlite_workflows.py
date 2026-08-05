"""SQLite-backed Workflow repository used after the G3 cutover."""

from __future__ import annotations

import builtins
import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from comfyui_mcp_skills.domain.control_plane import derived_control_plane_id
from comfyui_mcp_skills.domain.models import Workflow
from comfyui_mcp_skills.domain.workflow_schema import normalize_parameters
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore


class SQLiteWorkflowRepository:
    """Read published Workflow deployments and atomically publish revisions."""

    def __init__(
        self,
        store: SQLiteControlPlaneStore,
        *,
        owner_id: str | None = None,
        owner_provider: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._owner_id_value = owner_id
        self._owner_provider = owner_provider

    @property
    def _owner_id(self) -> str | None:
        return self._owner_provider() if self._owner_provider is not None else self._owner_id_value

    def list(self) -> list[Workflow]:
        connection = self._connect()
        try:
            if not self._has_owner_overlay(connection):
                rows = connection.execute(
                    """SELECT d.server_id,d.workflow_id,d.enabled,
                    r.graph_json,r.parameter_schema_json
                    FROM workflow_deployments AS d JOIN workflow_revisions AS r
                    ON r.workflow_id=d.workflow_id AND r.revision_id=d.revision_id
                    WHERE d.published=1 AND d.enabled=1 AND d.validation_status='valid'
                    ORDER BY d.server_id,d.workflow_id"""
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT d.server_id,d.workflow_id,s.enabled,
                    r.graph_json,r.parameter_schema_json
                    FROM config_workflow_deployments AS b
                    JOIN workflow_deployments AS d ON d.deployment_id=b.deployment_id
                    JOIN workflow_revisions AS r ON r.workflow_id=d.workflow_id
                    AND r.revision_id=d.revision_id
                    JOIN config_workflow_states AS s ON s.owner_id=b.owner_id
                    AND s.server_id=b.server_id AND s.workflow_id=b.workflow_id
                    JOIN managed_servers AS m ON m.owner_id=b.owner_id
                    AND m.server_id=b.server_id AND m.lifecycle_status='active'
                    WHERE b.owner_id=? AND s.enabled=1 AND d.enabled=1
                    AND d.validation_status='valid' ORDER BY d.server_id,d.workflow_id""",
                    (self._owner_id,),
                ).fetchall()
        finally:
            connection.close()
        return [self._workflow_from_row(row) for row in rows]

    def get(self, server_id: str, workflow_id: str) -> Workflow | None:
        connection = self._connect()
        try:
            if not self._has_owner_overlay(connection):
                row = connection.execute(
                    """SELECT d.server_id,d.workflow_id,d.enabled,
                    r.graph_json,r.parameter_schema_json
                    FROM workflow_deployments AS d JOIN workflow_revisions AS r
                    ON r.workflow_id=d.workflow_id AND r.revision_id=d.revision_id
                    WHERE d.server_id=? AND d.workflow_id=? AND d.published=1
                    AND d.enabled=1 AND d.validation_status='valid'""",
                    (server_id, workflow_id),
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT d.server_id,d.workflow_id,s.enabled,
                    r.graph_json,r.parameter_schema_json
                    FROM config_workflow_deployments AS b
                    JOIN workflow_deployments AS d ON d.deployment_id=b.deployment_id
                    JOIN workflow_revisions AS r ON r.workflow_id=d.workflow_id
                    AND r.revision_id=d.revision_id
                    JOIN config_workflow_states AS s ON s.owner_id=b.owner_id
                    AND s.server_id=b.server_id AND s.workflow_id=b.workflow_id
                    JOIN managed_servers AS m ON m.owner_id=b.owner_id
                    AND m.server_id=b.server_id AND m.lifecycle_status='active'
                    WHERE b.owner_id=? AND b.server_id=? AND b.workflow_id=?
                    AND s.enabled=1 AND d.enabled=1 AND d.validation_status='valid'""",
                    (self._owner_id, server_id, workflow_id),
                ).fetchone()
        finally:
            connection.close()
        return None if row is None else self._workflow_from_row(row)

    def list_revisions(self, workflow_id: str) -> builtins.list[dict[str, Any]]:
        connection = self._connect()
        try:
            if not self._has_owner_overlay(connection):
                rows = connection.execute(
                    """SELECT revision_id,workflow_id,content_digest,created_at
                       FROM workflow_revisions WHERE workflow_id=?
                       ORDER BY created_at,revision_id""",
                    (workflow_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT r.revision_id,r.workflow_id,r.content_digest,r.created_at
                       FROM workflow_revisions AS r WHERE r.workflow_id=? AND (
                         EXISTS(SELECT 1 FROM config_workflow_deployments AS b
                           JOIN workflow_deployments AS d ON d.deployment_id=b.deployment_id
                           WHERE b.owner_id=? AND d.workflow_id=r.workflow_id
                            AND d.revision_id=r.revision_id)
                         OR EXISTS(SELECT 1 FROM workflow_change_plans AS p
                           WHERE p.actor=? AND p.workflow_id=r.workflow_id
                            AND p.committed_revision_id=r.revision_id))
                       ORDER BY r.created_at,r.revision_id""",
                    (workflow_id, self._owner_id, self._owner_id),
                ).fetchall()
        finally:
            connection.close()
        return [
            {
                "revision_id": str(row[0]),
                "workflow_id": str(row[1]),
                "content_digest": str(row[2]),
                "created_at": str(row[3]),
            }
            for row in rows
        ]

    def describe(self, workflow_id: str, server_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            if not self._has_owner_overlay(connection):
                row = connection.execute(
                    """SELECT d.server_id,d.workflow_id,r.parameter_schema_json,r.revision_id,
                    d.deployment_id,r.content_digest,d.validation_status,d.published
                    FROM workflow_deployments AS d JOIN workflow_revisions AS r
                    ON r.workflow_id=d.workflow_id AND r.revision_id=d.revision_id
                    WHERE d.workflow_id=? AND d.server_id=? AND d.published=1
                    AND d.enabled=1 AND d.validation_status='valid' LIMIT 1""",
                    (workflow_id, server_id),
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT d.server_id,d.workflow_id,r.parameter_schema_json,r.revision_id,
                    d.deployment_id,r.content_digest,d.validation_status,d.published
                    FROM config_workflow_deployments AS b
                    JOIN workflow_deployments AS d ON d.deployment_id=b.deployment_id
                    JOIN workflow_revisions AS r ON r.workflow_id=d.workflow_id
                    AND r.revision_id=d.revision_id
                    JOIN config_workflow_states AS s ON s.owner_id=b.owner_id
                    AND s.server_id=b.server_id AND s.workflow_id=b.workflow_id
                    JOIN managed_servers AS m ON m.owner_id=b.owner_id
                    AND m.server_id=b.server_id AND m.lifecycle_status='active'
                    WHERE b.owner_id=? AND b.workflow_id=? AND b.server_id=?
                    AND s.enabled=1 AND d.enabled=1 AND d.validation_status='valid' LIMIT 1""",
                    (self._owner_id, workflow_id, server_id),
                ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise LookupError(f"published Workflow not found: {workflow_id}")
        schema = _json_object(str(row[2]), field="parameter schema")
        return {
            "server_id": str(row[0]),
            "workflow_id": str(row[1]),
            "description": str(schema.get("description", "")),
            "revision_id": str(row[3]),
            "deployment_id": str(row[4]),
            "content_digest": str(row[5]),
            "validation_status": str(row[6]),
            "published": bool(row[7]),
        }

    def get_revision(self, revision_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            owner_id = self._owner_id
            if not self._has_owner_overlay(connection):
                row = connection.execute(
                    """SELECT revision_id,workflow_id,graph_json,parameter_schema_json,
                              dependency_contract_json,content_digest,created_at
                       FROM workflow_revisions WHERE revision_id=?""",
                    (revision_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT r.revision_id,r.workflow_id,r.graph_json,r.parameter_schema_json,
                              r.dependency_contract_json,r.content_digest,r.created_at
                       FROM workflow_revisions AS r WHERE r.revision_id=? AND (
                         EXISTS(SELECT 1 FROM config_workflow_deployments AS b
                           JOIN workflow_deployments AS d ON d.deployment_id=b.deployment_id
                           WHERE b.owner_id=? AND d.workflow_id=r.workflow_id
                            AND d.revision_id=r.revision_id)
                         OR EXISTS(SELECT 1 FROM workflow_change_plans AS p
                           WHERE p.actor=? AND p.workflow_id=r.workflow_id
                            AND p.committed_revision_id=r.revision_id))""",
                    (revision_id, owner_id, owner_id),
                ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise LookupError(f"Workflow revision not found: {revision_id}")
        return {
            "revision_id": str(row[0]),
            "workflow_id": str(row[1]),
            "graph": _json_object(str(row[2]), field="Workflow graph"),
            "parameter_schema": _json_object(str(row[3]), field="parameter schema"),
            "dependency_contract": _json_object(str(row[4]), field="dependency contract"),
            "content_digest": str(row[5]),
            "created_at": str(row[6]),
        }

    def get_published_revision(self, workflow_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            if not self._has_owner_overlay(connection):
                row = connection.execute(
                    """SELECT r.revision_id FROM workflow_deployments AS d
                    JOIN workflow_revisions AS r ON r.workflow_id=d.workflow_id
                    AND r.revision_id=d.revision_id WHERE d.workflow_id=? AND d.published=1
                    AND d.enabled=1 AND d.validation_status='valid'
                    ORDER BY r.created_at DESC,r.revision_id DESC,d.server_id LIMIT 1""",
                    (workflow_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT r.revision_id FROM config_workflow_deployments AS b
                    JOIN workflow_deployments AS d ON d.deployment_id=b.deployment_id
                    JOIN workflow_revisions AS r ON r.workflow_id=d.workflow_id
                    AND r.revision_id=d.revision_id
                    JOIN config_workflow_states AS s ON s.owner_id=b.owner_id
                    AND s.server_id=b.server_id AND s.workflow_id=b.workflow_id
                    JOIN managed_servers AS m ON m.owner_id=b.owner_id
                    AND m.server_id=b.server_id AND m.lifecycle_status='active'
                    WHERE b.owner_id=? AND b.workflow_id=? AND s.enabled=1 AND d.enabled=1
                    AND d.validation_status='valid'
                    ORDER BY r.created_at DESC,r.revision_id DESC,d.server_id LIMIT 1""",
                    (self._owner_id, workflow_id),
                ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise LookupError(f"published Workflow not found: {workflow_id}")
        return self.get_revision(str(row[0]))

    def create_revision(
        self,
        *,
        workflow_id: str,
        server_id: str,
        graph: dict[str, Any],
        parameter_schema: dict[str, Any],
        dependency_contract: dict[str, Any],
        content_digest: str,
    ) -> dict[str, Any]:
        graph_json = _canonical_json(graph)
        schema_json = _canonical_json(parameter_schema)
        dependency_json = _canonical_json(dependency_contract)
        calculated = _revision_digest(graph, parameter_schema, dependency_contract)
        if content_digest != calculated:
            raise ValueError("Workflow revision content digest does not match payload")
        revision_id = derived_control_plane_id(
            "revision", "workflow-import-v1", [workflow_id, content_digest]
        )
        deployment_id = derived_control_plane_id(
            "deployment",
            "workflow-import-v1",
            [workflow_id, revision_id, server_id],
        )
        created_at = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        transaction_started = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            transaction_started = True
            if (
                self._owner_id is not None
                and connection.execute(
                    "SELECT 1 FROM managed_servers WHERE owner_id=? AND server_id=? "
                    "AND lifecycle_status='active'",
                    (self._owner_id, server_id),
                ).fetchone()
                is None
            ):
                raise LookupError(f"active managed Server not found: {server_id}")
            connection.execute(
                "INSERT OR IGNORE INTO workflows(workflow_id, created_at) VALUES (?, ?)",
                (workflow_id, created_at),
            )
            existing = connection.execute(
                """
                SELECT graph_json, parameter_schema_json, dependency_contract_json
                FROM workflow_revisions WHERE revision_id = ?
                """,
                (revision_id,),
            ).fetchone()
            expected = (graph_json, schema_json, dependency_json)
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO workflow_revisions(
                        revision_id, workflow_id, graph_json, parameter_schema_json,
                        dependency_contract_json, content_digest, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        workflow_id,
                        graph_json,
                        schema_json,
                        dependency_json,
                        content_digest,
                        created_at,
                    ),
                )
            elif tuple(existing) != expected:
                raise RuntimeError("Workflow revision identity conflicts with stored payload")
            connection.execute(
                """
                INSERT OR IGNORE INTO workflow_deployments(
                    deployment_id, workflow_id, revision_id, server_id, enabled,
                    validation_status, published, created_at
                ) VALUES (?, ?, ?, ?, 1, 'valid', 0, ?)
                """,
                (deployment_id, workflow_id, revision_id, server_id, created_at),
            )
            deployment = connection.execute(
                """
                SELECT validation_status, published
                FROM workflow_deployments
                WHERE deployment_id = ?
                """,
                (deployment_id,),
            ).fetchone()
            if deployment is None:
                raise RuntimeError("Workflow deployment was not persisted")
            validation_status = str(deployment[0])
            published = bool(deployment[1])
            connection.commit()
            transaction_started = False
        except BaseException:
            if transaction_started:
                connection.rollback()
            raise
        finally:
            connection.close()
        return {
            "workflow_id": workflow_id,
            "revision_id": revision_id,
            "deployment_id": deployment_id,
            "content_digest": content_digest,
            "validation_status": validation_status,
            "published": published,
        }

    def publish(self, deployment_id: str) -> None:
        connection = self._connect()
        transaction_started = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            transaction_started = True
            row = connection.execute(
                """
                SELECT workflow_id, server_id
                FROM workflow_deployments
                WHERE deployment_id = ? AND enabled = 1 AND validation_status = 'valid'
                """,
                (deployment_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Workflow deployment not found: {deployment_id}")
            workflow_id, server_id = str(row[0]), str(row[1])
            updated_at = datetime.now(timezone.utc).isoformat()
            if self._owner_id is None:
                connection.execute(
                    """UPDATE workflow_deployments SET published = 0
                    WHERE workflow_id = ? AND server_id = ? AND published = 1""",
                    (workflow_id, server_id),
                )
                updated = connection.execute(
                    """UPDATE workflow_deployments SET published = 1
                    WHERE deployment_id = ? AND workflow_id = ? AND server_id = ?""",
                    (deployment_id, workflow_id, server_id),
                ).rowcount
                if updated != 1:
                    raise RuntimeError("Workflow deployment changed during publish")
            else:
                owns_server = connection.execute(
                    "SELECT 1 FROM managed_servers WHERE owner_id=? AND server_id=? "
                    "AND lifecycle_status='active'",
                    (self._owner_id, server_id),
                ).fetchone()
                if owns_server is None:
                    raise LookupError(f"active managed Server not found: {server_id}")
                connection.execute(
                    "INSERT INTO config_workflow_deployments"
                    "(owner_id,deployment_id,server_id,workflow_id) VALUES(?,?,?,?) "
                    "ON CONFLICT(owner_id,server_id,workflow_id) DO UPDATE SET "
                    "deployment_id=excluded.deployment_id",
                    (self._owner_id, deployment_id, server_id, workflow_id),
                )
                connection.execute(
                    "INSERT INTO config_workflow_snapshots(owner_id,updated_at) VALUES(?,?) "
                    "ON CONFLICT(owner_id) DO UPDATE SET updated_at=excluded.updated_at",
                    (self._owner_id, updated_at),
                )
                connection.execute(
                    "INSERT INTO config_workflow_states"
                    "(owner_id,server_id,workflow_id,enabled,updated_at) "
                    "VALUES(?,?,?,1,?) ON CONFLICT(owner_id,server_id,workflow_id) DO UPDATE SET "
                    "enabled=1,updated_at=excluded.updated_at",
                    (self._owner_id, server_id, workflow_id, updated_at),
                )
                _advance_config_state(connection, self._owner_id, updated_at)
            connection.commit()
            transaction_started = False
        except BaseException:
            if transaction_started:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _has_owner_overlay(self, connection: sqlite3.Connection) -> bool:
        return (
            self._owner_id is not None
            and connection.execute(
                "SELECT 1 FROM config_workflow_snapshots WHERE owner_id=?",
                (self._owner_id,),
            ).fetchone()
            is not None
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._store.path, isolation_level=None, timeout=5.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA trusted_schema = OFF")
        return connection

    @staticmethod
    def _workflow_from_row(row: sqlite3.Row | tuple[object, ...]) -> Workflow:
        graph = _json_object(str(row[3]), field="Workflow graph")
        schema = _json_object(str(row[4]), field="parameter schema")
        return Workflow(
            server_id=str(row[0]),
            workflow_id=str(row[1]),
            description=str(schema.get("description", "")),
            parameters=normalize_parameters(schema),
            graph=graph,
            enabled=bool(row[2]),
        )


def _advance_config_state(connection: sqlite3.Connection, owner_id: str, updated_at: str) -> None:
    row = connection.execute(
        "SELECT current_revision FROM config_state WHERE owner_id=?", (owner_id,)
    ).fetchone()
    revision = 1 if row is None else int(row[0]) + 1
    facts = {
        "servers": [
            tuple(item)
            for item in connection.execute(
                "SELECT server_id,current_revision,current_digest,lifecycle_status "
                "FROM managed_servers "
                "WHERE owner_id=? ORDER BY server_id",
                (owner_id,),
            ).fetchall()
        ],
        "default_server": connection.execute(
            "SELECT server_id FROM server_defaults WHERE owner_id=?", (owner_id,)
        ).fetchone(),
        "workflows": [
            tuple(item)
            for item in connection.execute(
                "SELECT server_id,workflow_id,enabled FROM config_workflow_states "
                "WHERE owner_id=? ORDER BY server_id,workflow_id",
                (owner_id,),
            ).fetchall()
        ],
    }
    digest = hashlib.sha256(_canonical_json(facts).encode("utf-8")).hexdigest()
    connection.execute(
        "INSERT INTO config_state"
        "(owner_id,current_revision,current_digest,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(owner_id) DO UPDATE SET current_revision=excluded.current_revision,"
        "current_digest=excluded.current_digest,updated_at=excluded.updated_at",
        (owner_id, revision, digest, updated_at),
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _revision_digest(
    graph: dict[str, Any],
    parameter_schema: dict[str, Any],
    dependency_contract: dict[str, Any],
) -> str:
    value = {
        "identity_version": 2,
        "graph": graph,
        "parameters": parameter_schema.get("parameters", {}),
        "dependencies": dependency_contract,
        "output_contract": parameter_schema.get("_output_contract"),
    }
    metadata = parameter_schema.get("_revision")
    if isinstance(metadata, dict) and metadata:
        value["revision_metadata"] = metadata
    payload = _canonical_json(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_object(raw: str, *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"stored {field} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"stored {field} must be an object")
    return value
