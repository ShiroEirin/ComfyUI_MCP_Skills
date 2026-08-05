"""SQLite persistence for Phase K routing plans and candidate snapshots."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from comfyui_mcp_skills.domain.errors import ServerNotFound
from comfyui_mcp_skills.domain.workflow_schema import normalize_parameters
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore


class SQLiteRoutingRepository:
    def __init__(self, store: SQLiteControlPlaneStore) -> None:
        self._store = store

    def list_routing_contexts(self, owner_id: str, workflow_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            marker = connection.execute(
                "SELECT 1 FROM config_workflow_snapshots WHERE owner_id=?", (owner_id,)
            ).fetchone()
            if marker is None:
                rows = connection.execute(
                    """SELECT d.server_id,d.revision_id,d.deployment_id,r.content_digest,
                       r.parameter_schema_json,'{}',0,''
                    FROM workflow_deployments AS d JOIN workflow_revisions AS r
                     ON r.workflow_id=d.workflow_id AND r.revision_id=d.revision_id
                    WHERE d.workflow_id=? AND d.published=1 AND d.enabled=1
                     AND d.validation_status='valid' ORDER BY d.server_id""",
                    (workflow_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT d.server_id,d.revision_id,d.deployment_id,r.content_digest,
                       r.parameter_schema_json,sr.config_json,sr.revision,sr.config_digest
                    FROM config_workflow_deployments AS b
                    JOIN config_workflow_states AS state ON state.owner_id=b.owner_id
                     AND state.server_id=b.server_id AND state.workflow_id=b.workflow_id
                    JOIN workflow_deployments AS d ON d.deployment_id=b.deployment_id
                    JOIN workflow_revisions AS r ON r.workflow_id=d.workflow_id
                     AND r.revision_id=d.revision_id
                    JOIN managed_servers AS managed ON managed.owner_id=b.owner_id
                     AND managed.server_id=b.server_id AND managed.lifecycle_status='active'
                    JOIN server_revisions AS sr ON sr.owner_id=managed.owner_id
                     AND sr.server_id=managed.server_id AND sr.revision=managed.current_revision
                    WHERE b.owner_id=? AND b.workflow_id=? AND state.enabled=1
                     AND d.enabled=1 AND d.validation_status='valid' ORDER BY d.server_id""",
                    (owner_id, workflow_id),
                ).fetchall()
            return [{**self._context(row), "owner_id": owner_id} for row in rows]
        finally:
            connection.close()

    def current_server_connection(self, owner_id: str, server_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT sr.config_json FROM managed_servers AS managed
                   JOIN server_revisions AS sr ON sr.owner_id=managed.owner_id
                    AND sr.server_id=managed.server_id AND sr.revision=managed.current_revision
                    AND sr.config_digest=managed.current_digest
                   WHERE managed.owner_id=? AND managed.server_id=?
                    AND managed.lifecycle_status='active'""",
                (owner_id, server_id),
            ).fetchone()
            if row is None:
                marker = connection.execute(
                    "SELECT 1 FROM config_workflow_snapshots WHERE owner_id=?", (owner_id,)
                ).fetchone()
                if marker is not None:
                    raise ServerNotFound("Owner-managed Server was not found")
                return None
            return {"id": server_id, **_object(str(row[0]), "Server config")}
        finally:
            connection.close()

    def resolve_server_connection(
        self,
        owner_id: str,
        server_id: str,
        revision: int,
        config_digest: str,
    ) -> dict[str, Any] | None:
        if revision == 0 and not config_digest:
            return None
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT sr.config_json FROM server_revisions AS sr
                   JOIN managed_servers AS managed ON managed.owner_id=sr.owner_id
                    AND managed.server_id=sr.server_id
                    AND managed.current_revision=sr.revision
                    AND managed.current_digest=sr.config_digest
                   WHERE sr.owner_id=? AND sr.server_id=? AND sr.revision=?
                    AND sr.config_digest=? AND managed.lifecycle_status='active'""",
                (owner_id, server_id, revision, config_digest),
            ).fetchone()
            if row is None:
                raise ValueError("Routing Server revision conflict")
            return {"id": server_id, **_object(str(row[0]), "Server config")}
        finally:
            connection.close()

    def save_routing_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        created_at = datetime.now(timezone.utc).isoformat()
        result = {**plan, "created_at": created_at}
        encoded = _canonical_json(result)
        if len(encoded.encode()) > 1024 * 1024:
            raise ValueError("Routing plan exceeds 1 MiB")
        connection = self._connect()
        transaction_started = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            transaction_started = True
            existing = connection.execute(
                "SELECT owner_id,plan_digest,content_json,status,job_id,committed_at "
                "FROM routing_plans WHERE plan_id=?",
                (plan["plan_id"],),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO routing_plans(
                    plan_id,owner_id,workflow_id,selected_server_id,revision_id,deployment_id,
                    plan_digest,content_json,status,job_id,created_at,committed_at,resource_uri)
                    VALUES(?,?,?,?,?,?,?,?,?,NULL,?,NULL,?)""",
                    (
                        plan["plan_id"],
                        plan["owner_id"],
                        plan["workflow_id"],
                        plan["selected_server_id"],
                        plan["revision_id"],
                        plan["deployment_id"],
                        plan["plan_digest"],
                        encoded,
                        "planned",
                        created_at,
                        plan["resource_uri"],
                    ),
                )
            elif (str(existing[0]), str(existing[1])) != (
                plan["owner_id"],
                plan["plan_digest"],
            ):
                raise ValueError("Routing plan identity conflict")
            else:
                result = _object(str(existing[2]), "routing plan")
                result.update(
                    {
                        "status": str(existing[3]),
                        "job_id": "" if existing[4] is None else str(existing[4]),
                        "committed_at": "" if existing[5] is None else str(existing[5]),
                    }
                )
            connection.commit()
            transaction_started = False
            return result
        except BaseException:
            if transaction_started:
                connection.rollback()
            raise
        finally:
            connection.close()

    def claim_routing_commit(
        self,
        plan_id: str,
        plan_digest: str,
        owner_id: str,
        idempotency_digest: str,
    ) -> None:
        connection = self._connect()
        transaction_started = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            transaction_started = True
            existing = connection.execute(
                """SELECT plan_id,plan_digest FROM routing_commit_idempotency
                   WHERE owner_id=? AND idempotency_digest=?""",
                (owner_id, idempotency_digest),
            ).fetchone()
            plan_claim = connection.execute(
                """SELECT idempotency_digest FROM routing_commit_idempotency
                   WHERE owner_id=? AND plan_id=?""",
                (owner_id, plan_id),
            ).fetchone()
            if existing is None and plan_claim is not None:
                raise ValueError("Routing plan already uses another idempotency key")
            if existing is None:
                connection.execute(
                    """INSERT INTO routing_commit_idempotency(
                       owner_id,idempotency_digest,plan_id,plan_digest,created_at)
                       VALUES(?,?,?,?,?)""",
                    (
                        owner_id,
                        idempotency_digest,
                        plan_id,
                        plan_digest,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            elif (str(existing[0]), str(existing[1])) != (plan_id, plan_digest):
                raise ValueError("Routing commit idempotency key conflict")
            connection.commit()
            transaction_started = False
        except BaseException:
            if transaction_started:
                connection.rollback()
            raise
        finally:
            connection.close()

    def get_routing_plan(self, plan_id: str, owner_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT content_json,status,job_id,committed_at FROM routing_plans "
                "WHERE plan_id=? AND owner_id=?",
                (plan_id, owner_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        result = _object(str(row[0]), "routing plan")
        result.update(
            {
                "status": str(row[1]),
                "job_id": "" if row[2] is None else str(row[2]),
                "committed_at": "" if row[3] is None else str(row[3]),
            }
        )
        return result

    def mark_routing_plan_committed(
        self, plan_id: str, plan_digest: str, owner_id: str, job_id: str
    ) -> dict[str, Any]:
        committed_at = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        transaction_started = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            transaction_started = True
            row = connection.execute(
                "SELECT status,job_id,selected_server_id,revision_id,deployment_id "
                "FROM routing_plans WHERE plan_id=? AND plan_digest=? AND owner_id=?",
                (plan_id, plan_digest, owner_id),
            ).fetchone()
            if row is None:
                raise ValueError("Routing plan digest conflict")
            job_match = connection.execute(
                "SELECT 1 FROM jobs WHERE job_id=? AND owner_id=? AND server_id=? "
                "AND revision_id=? AND deployment_id=?",
                (job_id, owner_id, str(row[2]), str(row[3]), str(row[4])),
            ).fetchone()
            if job_match is None:
                raise ValueError("Routing Job identity does not match the reviewed plan")
            if str(row[0]) == "planned":
                connection.execute(
                    "UPDATE routing_plans SET status='committed',job_id=?,committed_at=? "
                    "WHERE plan_id=? AND plan_digest=? AND owner_id=? AND status='planned'",
                    (job_id, committed_at, plan_id, plan_digest, owner_id),
                )
            elif str(row[1]) != job_id:
                raise ValueError("Routing plan was committed to another Job")
            connection.commit()
            transaction_started = False
        except BaseException:
            if transaction_started:
                connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_routing_plan(plan_id, owner_id)
        if result is None:
            raise RuntimeError("Committed routing plan is unavailable")
        return result

    @staticmethod
    def _context(row: sqlite3.Row | tuple[object, ...]) -> dict[str, Any]:
        schema = _object(str(row[4]), "parameter schema")
        config = _object(str(row[5]), "Server config")
        missing = config.get("missing_dependencies", [])
        return {
            "server_id": str(row[0]),
            "revision_id": str(row[1]),
            "deployment_id": str(row[2]),
            "content_digest": str(row[3]),
            "parameters": normalize_parameters(schema),
            "server_revision": int(str(row[6])),
            "server_config_digest": str(row[7]),
            "queue_depth": _bounded_config_int(config, "routing_queue_depth", 0, 0, 100_000),
            "execution_slots": _bounded_config_int(config, "execution_slots", 1, 1, 64),
            "subject_submission_quota": _bounded_config_int(
                config, "subject_submission_quota", 0, 0, 10_000
            ),
            "available_vram_bytes": _bounded_config_int(
                config, "routing_available_vram_bytes", 0, 0, 2**63 - 1
            ),
            "required_vram_bytes": _bounded_config_int(
                config, "routing_required_vram_bytes", 0, 0, 2**63 - 1
            ),
            "missing_dependencies": (
                [str(item) for item in missing]
                if isinstance(missing, list) and all(isinstance(item, str) for item in missing)
                else []
            ),
            "reuse_mode": str(config.get("routing_reuse_mode", "none")),
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._store.path, isolation_level=None, timeout=5.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA trusted_schema = OFF")
        return connection


def _bounded_config_int(
    config: dict[str, Any], key: str, default: int, minimum: int, maximum: int
) -> int:
    value = config.get(key, default)
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum
        else default
    )


def _object(raw: str, field: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


__all__ = ["SQLiteRoutingRepository"]
