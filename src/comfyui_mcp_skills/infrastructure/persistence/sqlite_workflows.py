"""SQLite-backed Workflow repository used after the G3 cutover."""

from __future__ import annotations

import builtins
import json
import sqlite3
from typing import Any

from comfyui_mcp_skills.domain.models import Workflow
from comfyui_mcp_skills.domain.workflow_schema import normalize_parameters
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore


class SQLiteWorkflowRepository:
    """Read published Workflow deployments and atomically publish revisions."""

    def __init__(self, store: SQLiteControlPlaneStore) -> None:
        self._store = store

    def list(self) -> list[Workflow]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT d.server_id, d.workflow_id, d.enabled,
                       r.graph_json, r.parameter_schema_json
                FROM workflow_deployments AS d
                JOIN workflow_revisions AS r
                  ON r.workflow_id = d.workflow_id AND r.revision_id = d.revision_id
                WHERE d.published = 1 AND d.enabled = 1 AND d.validation_status = 'valid'
                ORDER BY d.server_id, d.workflow_id
                """
            ).fetchall()
        finally:
            connection.close()
        return [self._workflow_from_row(row) for row in rows]

    def get(self, server_id: str, workflow_id: str) -> Workflow | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT d.server_id, d.workflow_id, d.enabled,
                       r.graph_json, r.parameter_schema_json
                FROM workflow_deployments AS d
                JOIN workflow_revisions AS r
                  ON r.workflow_id = d.workflow_id AND r.revision_id = d.revision_id
                WHERE d.server_id = ? AND d.workflow_id = ? AND d.published = 1
                  AND d.enabled = 1 AND d.validation_status = 'valid'
                """,
                (server_id, workflow_id),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else self._workflow_from_row(row)

    def list_revisions(self, workflow_id: str) -> builtins.list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT revision_id, workflow_id, content_digest, created_at
                FROM workflow_revisions
                WHERE workflow_id = ?
                ORDER BY created_at, revision_id
                """,
                (workflow_id,),
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
            row = connection.execute(
                """
                SELECT d.server_id, d.workflow_id, r.parameter_schema_json,
                       r.revision_id, d.deployment_id, r.content_digest,
                       d.validation_status, d.published
                FROM workflow_deployments AS d
                JOIN workflow_revisions AS r
                  ON r.workflow_id = d.workflow_id AND r.revision_id = d.revision_id
                WHERE d.workflow_id = ? AND d.server_id = ? AND d.published = 1
                  AND d.enabled = 1 AND d.validation_status = 'valid'
                LIMIT 1
                """,
                (workflow_id, server_id),
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
            connection.execute(
                """
                UPDATE workflow_deployments
                SET published = 0
                WHERE workflow_id = ? AND server_id = ? AND published = 1
                """,
                (workflow_id, server_id),
            )
            updated = connection.execute(
                """
                UPDATE workflow_deployments
                SET published = 1
                WHERE deployment_id = ? AND workflow_id = ? AND server_id = ?
                """,
                (deployment_id, workflow_id, server_id),
            ).rowcount
            if updated != 1:
                raise RuntimeError("Workflow deployment changed during publish")
            connection.commit()
            transaction_started = False
        except BaseException:
            if transaction_started:
                connection.rollback()
            raise
        finally:
            connection.close()

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


def _json_object(raw: str, *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"stored {field} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"stored {field} must be an object")
    return value
