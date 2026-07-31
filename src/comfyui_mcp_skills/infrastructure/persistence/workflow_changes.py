"""Transactional persistence for Phase J Workflow change plans."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from comfyui_mcp_skills.domain.control_plane import derived_control_plane_id
from comfyui_mcp_skills.domain.errors import (
    WorkflowChangeConflict,
    WorkflowChangeNotFound,
)
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.sqlite_workflows import SQLiteWorkflowRepository


class SQLiteWorkflowChangeRepository:
    """Persist prepared changes and commit them against one published base Revision."""

    def __init__(self, store: SQLiteControlPlaneStore) -> None:
        self._store = store
        self._workflows = SQLiteWorkflowRepository(store)

    def describe(self, workflow_id: str, server_id: str) -> dict[str, Any]:
        return self._workflows.describe(workflow_id, server_id)

    def get_revision(self, revision_id: str) -> dict[str, Any]:
        return self._workflows.get_revision(revision_id)

    def create_revision(self, **values: Any) -> dict[str, Any]:
        return self._workflows.create_revision(**values)

    def publish(self, deployment_id: str) -> None:
        self._workflows.publish(deployment_id)

    def save_change_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "plan_id",
            "workflow_id",
            "server_id",
            "base_revision_id",
            "operations",
            "graph",
            "parameter_schema",
            "dependency_contract",
            "content_digest",
            "plan_digest",
            "diff",
            "actor",
            "created_at",
            "expires_at",
        )
        missing = [field for field in fields if field not in plan]
        if missing:
            raise ValueError("Workflow change plan is incomplete")
        encoded = {
            "operations": _canonical_json(plan["operations"]),
            "graph": _canonical_json(plan["graph"]),
            "parameter_schema": _canonical_json(plan["parameter_schema"]),
            "dependency_contract": _canonical_json(plan["dependency_contract"]),
            "diff": _canonical_json(plan["diff"]),
        }
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT plan_digest, workflow_id, server_id, base_revision_id,
                       operations_json, graph_json, parameter_schema_json,
                       dependency_contract_json, content_digest, diff_json,
                       actor, created_at, expires_at, committed_revision_id
                FROM workflow_change_plans WHERE plan_id = ?
                """,
                (plan["plan_id"],),
            ).fetchone()
            expected = (
                plan["plan_digest"],
                plan["workflow_id"],
                plan["server_id"],
                plan["base_revision_id"],
                encoded["operations"],
                encoded["graph"],
                encoded["parameter_schema"],
                encoded["dependency_contract"],
                plan["content_digest"],
                encoded["diff"],
                plan["actor"],
                plan["created_at"],
                plan["expires_at"],
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO workflow_change_plans(
                        plan_id, workflow_id, server_id, base_revision_id,
                        operations_json, graph_json, parameter_schema_json,
                        dependency_contract_json, content_digest, plan_digest,
                        diff_json, actor, created_at, expires_at, committed_revision_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        plan["plan_id"],
                        plan["workflow_id"],
                        plan["server_id"],
                        plan["base_revision_id"],
                        encoded["operations"],
                        encoded["graph"],
                        encoded["parameter_schema"],
                        encoded["dependency_contract"],
                        plan["content_digest"],
                        plan["plan_digest"],
                        encoded["diff"],
                        plan["actor"],
                        plan["created_at"],
                        plan["expires_at"],
                    ),
                )
                committed_revision_id = None
            else:
                if tuple(existing[:13]) != expected:
                    raise RuntimeError(
                        "Workflow change plan identity conflicts with stored payload"
                    )
                committed_revision_id = existing[13]
            connection.commit()
            return {"committed_revision_id": committed_revision_id}
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def commit_change_plan(self, plan_id: str, plan_digest: str) -> dict[str, Any]:
        if not plan_id.startswith("plan_") or not plan_digest:
            raise ValueError("plan_id and plan_digest are required")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT workflow_id, server_id, base_revision_id, graph_json,
                       parameter_schema_json, dependency_contract_json, content_digest,
                       plan_digest, expires_at, committed_revision_id
                FROM workflow_change_plans WHERE plan_id = ?
                """,
                (plan_id,),
            ).fetchone()
            if row is None:
                raise WorkflowChangeNotFound(
                    "Workflow change plan was not found", details={"plan_id": plan_id}
                )
            if str(row[7]) != plan_digest:
                raise WorkflowChangeConflict(
                    "Workflow change plan digest does not match",
                    details={"plan_id": plan_id, "reason": "digest_mismatch"},
                )
            workflow_id, server_id, base_revision_id = map(str, row[:3])
            if row[9] is not None:
                result = self._committed_result(connection, workflow_id, server_id, str(row[9]))
                connection.commit()
                return result
            expires_at = datetime.fromisoformat(str(row[8]))
            if expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc):
                raise WorkflowChangeConflict(
                    "Workflow change plan expired",
                    details={"plan_id": plan_id, "reason": "expired"},
                )
            published = connection.execute(
                """
                SELECT revision_id FROM workflow_deployments
                WHERE workflow_id = ? AND server_id = ? AND published = 1
                  AND enabled = 1 AND validation_status = 'valid'
                """,
                (workflow_id, server_id),
            ).fetchone()
            if published is None or str(published[0]) != base_revision_id:
                raise WorkflowChangeConflict(
                    "Workflow base Revision changed before commit",
                    details={"plan_id": plan_id, "reason": "stale_base"},
                )
            graph_json = str(row[3])
            schema_json = str(row[4])
            dependency_json = str(row[5])
            content_digest = str(row[6])
            existing = connection.execute(
                """
                SELECT revision_id, graph_json, parameter_schema_json,
                       dependency_contract_json
                FROM workflow_revisions
                WHERE workflow_id = ? AND content_digest = ?
                """,
                (workflow_id, content_digest),
            ).fetchone()
            expected = (graph_json, schema_json, dependency_json)
            created_at = datetime.now(timezone.utc).isoformat()
            if existing is None:
                revision_id = derived_control_plane_id(
                    "revision", "workflow-import-v1", [workflow_id, content_digest]
                )
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
            else:
                revision_id = str(existing[0])
                if tuple(existing[1:]) != expected:
                    raise WorkflowChangeConflict(
                        "Workflow Revision digest conflicts with stored payload",
                        details={"reason": "revision_identity"},
                    )
            deployment = connection.execute(
                """
                SELECT deployment_id FROM workflow_deployments
                WHERE workflow_id = ? AND revision_id = ? AND server_id = ?
                """,
                (workflow_id, revision_id, server_id),
            ).fetchone()
            deployment_id = (
                str(deployment[0])
                if deployment is not None
                else derived_control_plane_id(
                    "deployment",
                    "workflow-import-v1",
                    [workflow_id, revision_id, server_id],
                )
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO workflow_deployments(
                    deployment_id, workflow_id, revision_id, server_id, enabled,
                    validation_status, published, created_at
                ) VALUES (?, ?, ?, ?, 1, 'valid', 0, ?)
                """,
                (deployment_id, workflow_id, revision_id, server_id, created_at),
            )
            updated = connection.execute(
                """
                UPDATE workflow_change_plans SET committed_revision_id = ?
                WHERE plan_id = ? AND committed_revision_id IS NULL
                """,
                (revision_id, plan_id),
            ).rowcount
            if updated != 1:
                raise RuntimeError("Workflow change plan was committed concurrently")
            connection.commit()
            return {
                "plan_id": plan_id,
                "workflow_id": workflow_id,
                "server_id": server_id,
                "revision_id": revision_id,
                "deployment_id": deployment_id,
                "content_digest": content_digest,
                "published": False,
            }
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def rollback(
        self,
        workflow_id: str,
        server_id: str,
        target_revision_id: str,
        request_id: str,
        actor: str,
    ) -> dict[str, Any]:
        request_digest = _sha256(
            {
                "workflow_id": workflow_id,
                "server_id": server_id,
                "target_revision_id": target_revision_id,
                "actor": actor,
            }
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """
                SELECT request_digest, target_revision_id, replaced_revision_id,
                       revision_id, deployment_id
                FROM workflow_rollback_requests
                WHERE actor = ? AND request_id = ?
                """,
                (actor, request_id),
            ).fetchone()
            if prior is not None:
                if str(prior[0]) != request_digest:
                    raise WorkflowChangeConflict(
                        "Rollback request_id was reused with different inputs",
                        details={"request_id": request_id, "reason": "idempotency"},
                    )
                result = self._committed_result(connection, workflow_id, server_id, str(prior[3]))
                connection.commit()
                return {
                    **result,
                    "published": True,
                    "rollback_of": str(prior[1]),
                    "replaced_revision_id": str(prior[2]),
                }
            target = connection.execute(
                """
                SELECT graph_json, parameter_schema_json, dependency_contract_json
                FROM workflow_revisions
                WHERE workflow_id = ? AND revision_id = ?
                """,
                (workflow_id, target_revision_id),
            ).fetchone()
            if target is None:
                raise WorkflowChangeNotFound(
                    "Rollback target Revision was not found",
                    details={"target_revision_id": target_revision_id},
                )
            current = connection.execute(
                """
                SELECT revision_id FROM workflow_deployments
                WHERE workflow_id = ? AND server_id = ? AND published = 1
                  AND enabled = 1 AND validation_status = 'valid'
                """,
                (workflow_id, server_id),
            ).fetchone()
            if current is None:
                raise WorkflowChangeNotFound(
                    "Published Workflow Deployment was not found",
                    details={"workflow_id": workflow_id, "server_id": server_id},
                )
            replaced_revision_id = str(current[0])
            graph = json.loads(str(target[0]))
            schema = json.loads(str(target[1]))
            dependencies = json.loads(str(target[2]))
            if "_output_contract" not in schema:
                schema["_output_contract"] = {
                    "version": 0,
                    "coverage": "unknown",
                    "outputs": [],
                }
            schema["_revision"] = {
                "kind": "rollback",
                "rollback_of": target_revision_id,
                "replaces_revision_id": replaced_revision_id,
                "request_id": request_id,
                "actor": actor,
            }
            graph_json = _canonical_json(graph)
            schema_json = _canonical_json(schema)
            dependency_json = _canonical_json(dependencies)
            content_digest = _revision_digest(graph, schema, dependencies)
            revision_id = derived_control_plane_id(
                "revision", "workflow-import-v1", [workflow_id, content_digest]
            )
            deployment_id = derived_control_plane_id(
                "deployment",
                "workflow-import-v1",
                [workflow_id, revision_id, server_id],
            )
            created_at = datetime.now(timezone.utc).isoformat()
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
            connection.execute(
                """
                INSERT INTO workflow_deployments(
                    deployment_id, workflow_id, revision_id, server_id, enabled,
                    validation_status, published, created_at
                ) VALUES (?, ?, ?, ?, 1, 'valid', 0, ?)
                """,
                (deployment_id, workflow_id, revision_id, server_id, created_at),
            )
            connection.execute(
                """
                UPDATE workflow_deployments SET published = 0
                WHERE workflow_id = ? AND server_id = ? AND published = 1
                """,
                (workflow_id, server_id),
            )
            connection.execute(
                "UPDATE workflow_deployments SET published = 1 WHERE deployment_id = ?",
                (deployment_id,),
            )
            connection.execute(
                """
                INSERT INTO workflow_rollback_requests(
                    actor, request_id, request_digest, workflow_id, server_id,
                    target_revision_id, replaced_revision_id, revision_id,
                    deployment_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    actor,
                    request_id,
                    request_digest,
                    workflow_id,
                    server_id,
                    target_revision_id,
                    replaced_revision_id,
                    revision_id,
                    deployment_id,
                    created_at,
                ),
            )
            connection.commit()
            return {
                "workflow_id": workflow_id,
                "server_id": server_id,
                "revision_id": revision_id,
                "deployment_id": deployment_id,
                "content_digest": content_digest,
                "published": True,
                "rollback_of": target_revision_id,
                "replaced_revision_id": replaced_revision_id,
            }
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _committed_result(
        connection: sqlite3.Connection,
        workflow_id: str,
        server_id: str,
        revision_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT d.deployment_id, r.content_digest, d.published
            FROM workflow_deployments AS d
            JOIN workflow_revisions AS r
              ON r.workflow_id = d.workflow_id AND r.revision_id = d.revision_id
            WHERE d.workflow_id = ? AND d.server_id = ? AND d.revision_id = ?
            """,
            (workflow_id, server_id, revision_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("Committed Workflow change lost its Deployment")
        return {
            "workflow_id": workflow_id,
            "server_id": server_id,
            "revision_id": revision_id,
            "deployment_id": str(row[0]),
            "content_digest": str(row[1]),
            "published": bool(row[2]),
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._store.path, isolation_level=None, timeout=5.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA trusted_schema = OFF")
        return connection


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
    value: dict[str, Any] = {
        "identity_version": 2,
        "graph": graph,
        "parameters": parameter_schema.get("parameters", {}),
        "dependencies": dependency_contract,
        "output_contract": parameter_schema.get("_output_contract"),
    }
    metadata = parameter_schema.get("_revision")
    if isinstance(metadata, dict) and metadata:
        value["revision_metadata"] = metadata
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
