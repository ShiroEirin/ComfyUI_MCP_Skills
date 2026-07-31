"""Single-server immutable execution planning for the G4 cutover."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from comfyui_mcp_skills.application.ports import WorkflowRepository
from comfyui_mcp_skills.domain.control_plane import derived_control_plane_id
from comfyui_mcp_skills.domain.errors import PayloadTooLarge
from comfyui_mcp_skills.domain.workflow_schema import normalize_parameters
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.orchestration_schedule import (
    schedule_job_reconciliation,
)

FailureInjector = Callable[[str], None]

_MAX_INPUT_SNAPSHOT_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ExecutionIdentity:
    job_id: str
    plan_id: str
    revision_id: str
    deployment_id: str
    server_id: str
    plan_digest: str


class ExecutionPlanningService:
    """Materialize one immutable Plan, canonical Job, and reserved Attempt atomically."""

    def __init__(self, store: SQLiteControlPlaneStore, workflows: WorkflowRepository) -> None:
        self._store = store
        self._workflows = workflows

    def materialize(
        self,
        *,
        server_id: str,
        workflow_id: str,
        owner_id: str,
        arguments: dict[str, Any],
        client_id: str,
        failure_injector: FailureInjector | None = None,
        resolved_inputs: dict[str, Any] | None = None,
        workflow_graph: dict[str, Any] | None = None,
        parameter_schema: dict[str, Any] | None = None,
    ) -> ExecutionIdentity:
        input_snapshot = {
            "arguments": arguments,
            "resolved_inputs": resolved_inputs if resolved_inputs is not None else arguments,
        }
        inputs_json = _canonical_json(input_snapshot)
        encoded_inputs = inputs_json.encode("utf-8")
        if len(encoded_inputs) > _MAX_INPUT_SNAPSHOT_BYTES:
            raise PayloadTooLarge("Execution Plan input snapshot exceeds 1 MiB")
        input_digest = hashlib.sha256(encoded_inputs).hexdigest()
        created_at = datetime.now(timezone.utc).isoformat()
        connection = _connect(self._store)
        try:
            connection.execute("BEGIN IMMEDIATE")
            published = connection.execute(
                """
                SELECT d.revision_id, d.deployment_id, r.graph_json,
                       r.parameter_schema_json
                FROM workflow_deployments AS d
                JOIN workflow_revisions AS r
                  ON r.workflow_id = d.workflow_id AND r.revision_id = d.revision_id
                WHERE d.server_id = ? AND d.workflow_id = ? AND d.published = 1
                  AND d.enabled = 1 AND d.validation_status = 'valid'
                """,
                (server_id, workflow_id),
            ).fetchone()
            if published is None:
                raise LookupError(f"published Workflow not found: {workflow_id}")
            revision_id, deployment_id = str(published[0]), str(published[1])
            if workflow_graph is not None and _canonical_json(workflow_graph) != _canonical_json(
                json.loads(str(published[2]))
            ):
                raise RuntimeError("published workflow changed before execution planning")
            if parameter_schema is not None and _canonical_json(
                parameter_schema
            ) != _canonical_json(normalize_parameters(json.loads(str(published[3])))):
                raise RuntimeError("published workflow schema changed before execution planning")
            plan_digest = hashlib.sha256(
                _canonical_json(
                    {
                        "owner_id": owner_id,
                        "revision_id": revision_id,
                        "deployment_id": deployment_id,
                        "server_id": server_id,
                        "inputs": input_snapshot,
                    }
                ).encode()
            ).hexdigest()
            plan_id = derived_control_plane_id("plan", "g4-plan-v1", [plan_digest])
            job_id = derived_control_plane_id("job", "g4-job-v1", [owner_id, plan_id, client_id])
            attempt_id = derived_control_plane_id(
                "attempt", "g4-attempt-v1", [job_id, server_id, "1"]
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO execution_plans(
                    plan_id, workflow_id, revision_id, deployment_id, server_id,
                    resolved_inputs_json, input_digest, plan_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    workflow_id,
                    revision_id,
                    deployment_id,
                    server_id,
                    inputs_json,
                    input_digest,
                    plan_digest,
                    created_at,
                ),
            )
            _inject(failure_injector, "after_plan")
            connection.execute(
                """
                INSERT OR IGNORE INTO jobs(
                    job_id, workflow_id, plan_id, revision_id, deployment_id,
                    owner_id, status, retry_of, created_at, created_at_source,
                    legacy_migrated, execution_origin, error, outputs_json
                ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', NULL, ?, 'runtime', 0,
                          'planned', '', '[]')
                """,
                (
                    job_id,
                    workflow_id,
                    plan_id,
                    revision_id,
                    deployment_id,
                    owner_id,
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO execution_attempts(
                    attempt_id, job_id, attempt, server_id, upstream_prompt_id,
                    upstream_job_id, client_id, submission_state, created_at
                ) VALUES (?, ?, 1, ?, NULL, NULL, ?, 'submission_unknown', ?)
                """,
                (attempt_id, job_id, server_id, client_id, created_at),
            )
            schedule_job_reconciliation(
                connection,
                job_id=job_id,
                server_id=server_id,
                owner_id=owner_id,
                occurred_at=created_at,
            )
            _inject(failure_injector, "after_reconciliation_schedule")
            _verify_identity(
                connection,
                job_id=job_id,
                plan_id=plan_id,
                revision_id=revision_id,
                deployment_id=deployment_id,
                owner_id=owner_id,
                client_id=client_id,
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return ExecutionIdentity(
            job_id, plan_id, revision_id, deployment_id, server_id, plan_digest
        )

    def identity_for_client(self, server_id: str, client_id: str) -> ExecutionIdentity | None:
        connection = _connect(self._store)
        try:
            row = connection.execute(
                """
                SELECT jobs.job_id, jobs.plan_id, jobs.revision_id, jobs.deployment_id,
                       execution_plans.plan_digest
                FROM execution_attempts
                JOIN jobs ON jobs.job_id = execution_attempts.job_id
                JOIN execution_plans ON execution_plans.plan_id = jobs.plan_id
                WHERE execution_attempts.server_id = ?
                  AND execution_attempts.client_id = ?
                """,
                (server_id, client_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return ExecutionIdentity(
            str(row[0]), str(row[1]), str(row[2]), str(row[3]), server_id, str(row[4])
        )

    def mark_submission_unknown(self, identity: ExecutionIdentity, error: str) -> None:
        connection = _connect(self._store)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE jobs SET status = 'submission_unknown', error = ? "
                "WHERE job_id = ? AND status IN ('reserved', 'queued')",
                (error, identity.job_id),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def finalize_submission(
        self,
        identity: ExecutionIdentity,
        *,
        upstream_prompt_id: str,
        upstream_job_id: str = "",
        idempotency_key: str = "",
        request_digest: str = "",
        lease_token: str = "",
    ) -> None:
        if not upstream_prompt_id and not upstream_job_id:
            raise ValueError("submission requires an upstream identity")
        connection = _connect(self._store)
        try:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE execution_attempts
                SET upstream_prompt_id = ?, upstream_job_id = ?, submission_state = 'submitted'
                WHERE job_id = ? AND attempt = 1 AND server_id = ?
                  AND submission_state = 'submission_unknown'
                  AND upstream_prompt_id IS NULL AND upstream_job_id IS NULL
                """,
                (
                    upstream_prompt_id or None,
                    upstream_job_id or None,
                    identity.job_id,
                    identity.server_id,
                ),
            ).rowcount
            if updated != 1:
                existing = connection.execute(
                    """
                    SELECT upstream_prompt_id, upstream_job_id, submission_state
                    FROM execution_attempts WHERE job_id = ? AND attempt = 1
                    """,
                    (identity.job_id,),
                ).fetchone()
                if existing is None or str(existing[2]) != "submitted":
                    raise RuntimeError("submission identity conflicts with existing attempt")
                requested_prompt = upstream_prompt_id or None
                requested_job = upstream_job_id or None
                if (
                    existing[0] is not None
                    and requested_prompt is not None
                    and str(existing[0]) != requested_prompt
                ) or (
                    existing[1] is not None
                    and requested_job is not None
                    and str(existing[1]) != requested_job
                ):
                    raise RuntimeError("submission identity conflicts with existing attempt")
                merged_prompt = existing[0] or requested_prompt
                merged_job = existing[1] or requested_job
                if merged_prompt != existing[0] or merged_job != existing[1]:
                    connection.execute(
                        """
                        UPDATE execution_attempts
                        SET upstream_prompt_id = ?, upstream_job_id = ?
                        WHERE job_id = ? AND attempt = 1 AND server_id = ?
                        """,
                        (merged_prompt, merged_job, identity.job_id, identity.server_id),
                    )
            connection.execute(
                "UPDATE jobs SET status = 'submitted' "
                "WHERE job_id = ? AND status IN ('reserved', 'submission_unknown')",
                (identity.job_id,),
            )
            if idempotency_key:
                resolved = connection.execute(
                    """
                    UPDATE idempotency_records
                    SET state = 'resolved', job_id = ?, expires_at = NULL, lease_token = NULL
                    WHERE owner_id = (SELECT owner_id FROM jobs WHERE job_id = ?)
                      AND scope = ? AND key = ? AND request_digest = ?
                      AND state IN ('reserved', 'submission_unknown') AND lease_token = ?
                    """,
                    (
                        identity.job_id,
                        identity.job_id,
                        f"legacy-execute:{identity.server_id}",
                        idempotency_key,
                        request_digest,
                        lease_token,
                    ),
                ).rowcount
                if resolved != 1:
                    raise RuntimeError("idempotency lease is no longer owned")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _verify_identity(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    plan_id: str,
    revision_id: str,
    deployment_id: str,
    owner_id: str,
    client_id: str,
) -> None:
    row = connection.execute(
        """
        SELECT jobs.plan_id, jobs.revision_id, jobs.deployment_id, jobs.owner_id,
               execution_attempts.client_id
        FROM jobs JOIN execution_attempts ON execution_attempts.job_id = jobs.job_id
        WHERE jobs.job_id = ? AND execution_attempts.attempt = 1
        """,
        (job_id,),
    ).fetchone()
    expected = (plan_id, revision_id, deployment_id, owner_id, client_id)
    if row is None or tuple(row) != expected:
        raise RuntimeError("canonical execution identity conflicts with existing facts")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _connect(store: SQLiteControlPlaneStore) -> sqlite3.Connection:
    connection = sqlite3.connect(store.path, isolation_level=None, timeout=5.0)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA trusted_schema = OFF")
    return connection


def _inject(injector: FailureInjector | None, phase: str) -> None:
    if injector is not None:
        injector(phase)
