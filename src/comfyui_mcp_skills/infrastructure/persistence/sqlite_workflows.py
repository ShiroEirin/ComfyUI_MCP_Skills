"""SQLite-backed Workflow repository used after the G3 cutover."""

from __future__ import annotations

import builtins
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from comfyui_mcp_skills.domain.control_plane import derived_control_plane_id
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

    def get_revision(self, revision_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT revision_id, workflow_id, graph_json, parameter_schema_json,
                       dependency_contract_json, content_digest, created_at
                FROM workflow_revisions
                WHERE revision_id = ?
                """,
                (revision_id,),
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
            row = connection.execute(
                """
                SELECT r.revision_id
                FROM workflow_deployments AS d
                JOIN workflow_revisions AS r
                  ON r.workflow_id = d.workflow_id AND r.revision_id = d.revision_id
                WHERE d.workflow_id = ? AND d.published = 1 AND d.enabled = 1
                  AND d.validation_status = 'valid'
                ORDER BY r.created_at DESC, r.revision_id DESC, d.server_id
                LIMIT 1
                """,
                (workflow_id,),
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
    payload = _canonical_json(
        {
            "graph": graph,
            "parameters": parameter_schema.get("parameters", {}),
            "dependencies": dependency_contract,
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_object(raw: str, *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"stored {field} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"stored {field} must be an object")
    return value
