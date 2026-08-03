"""SQLite compatibility facade for the current RunRepository contract."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from comfyui_mcp_skills.domain.control_plane import (
    derive_legacy_attempt_id,
    derive_legacy_job_id,
)
from comfyui_mcp_skills.domain.models import Job
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore

_STATUS_PRIORITY = {
    "submitted": 1,
    "queued": 2,
    "running": 3,
    "completed": 4,
    "cancelled": 5,
    "interrupted": 5,
    "error": 5,
    "lost": 5,
}
_TERMINAL_STATUSES = frozenset({"completed", "cancelled", "interrupted", "error", "lost"})


class SQLiteRunRepository:
    """Map the legacy prompt-facing application port onto normalized SQLite facts."""

    def __init__(self, store: SQLiteControlPlaneStore) -> None:
        self._store = store

    def claim(
        self,
        server_id: str,
        workflow_id: str,
        idempotency_key: str,
        arguments: dict[str, Any],
        owner_id: str = "",
        client_id: str = "",
    ) -> str | None:
        if not idempotency_key:
            return ""
        digest = self.request_digest(workflow_id, arguments)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=300)
        lease = secrets.token_hex(32)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT request_digest, state, expires_at
                FROM idempotency_records
                WHERE owner_id = ? AND scope = ? AND key = ?
                """,
                (owner_id, _scope(server_id), idempotency_key),
            ).fetchone()
            if row is not None:
                if str(row[0]) != digest:
                    return None
                expired_reservation = str(row[1]) == "reserved" and _is_expired(row[2], now)
                if str(row[1]) != "expired" and not expired_reservation:
                    return None
                connection.execute(
                    """
                    DELETE FROM idempotency_records
                    WHERE owner_id = ? AND scope = ? AND key = ?
                    """,
                    (owner_id, _scope(server_id), idempotency_key),
                )
            connection.execute(
                """
                INSERT INTO idempotency_records(
                    owner_id, scope, key, request_digest, state, job_id,
                    client_id, claimed_at, expires_at, lease_token, workflow_id
                ) VALUES (?, ?, ?, ?, 'reserved', NULL, ?, ?, ?, ?, ?)
                """,
                (
                    owner_id,
                    _scope(server_id),
                    idempotency_key,
                    digest,
                    client_id,
                    now.isoformat(),
                    expires.isoformat(),
                    lease,
                    workflow_id,
                ),
            )
            connection.commit()
            return lease
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_claim(self, server_id: str, key: str, owner_id: str = "") -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT request_digest, state, client_id, claimed_at, expires_at,
                       lease_token, job_id, workflow_id
                FROM idempotency_records
                WHERE owner_id = ? AND scope = ? AND key = ?
                """,
                (owner_id, _scope(server_id), key),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return {
            "request_digest": row[0],
            "status": row[1],
            "client_id": row[2],
            "claimed_at": row[3],
            "expires_at": row[4],
            "lease_token": row[5],
            "prompt_id": self._prompt_for_job(row[6]) if row[6] else "",
            "workflow_id": row[7],
        }

    def release_claim(
        self,
        server_id: str,
        key: str,
        request_digest: str,
        lease_token: str,
        owner_id: str = "",
    ) -> None:
        self._delete_reserved(server_id, key, owner_id, request_digest, lease_token)

    def mark_submission_unknown(
        self,
        server_id: str,
        key: str,
        lease_token: str,
        owner_id: str = "",
    ) -> None:
        if not key:
            return
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE idempotency_records
                SET state = 'submission_unknown', expires_at = NULL
                WHERE owner_id = ? AND scope = ? AND key = ?
                  AND state = 'reserved' AND lease_token = ?
                """,
                (owner_id, _scope(server_id), key, lease_token),
            ).rowcount
            if updated != 1:
                raise RuntimeError("idempotency lease is no longer owned")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def request_digest(workflow_id: str, arguments: dict[str, Any]) -> str:
        payload = json.dumps(
            {"workflow_id": workflow_id, "arguments": arguments},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def save(self, job: Job, *, lease_token: str = "") -> None:
        job_id = job.job_id or derive_legacy_job_id(job.server_id, job.prompt_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if job.idempotency_key and not lease_token:
                resolved = connection.execute(
                    """
                    SELECT job_id FROM idempotency_records
                    WHERE owner_id = ? AND scope = ? AND key = ?
                      AND state = 'resolved' AND request_digest = ? AND workflow_id = ?
                    """,
                    (
                        job.owner_id,
                        _scope(job.server_id),
                        job.idempotency_key,
                        job.request_digest,
                        job.workflow_id,
                    ),
                ).fetchone()
                if resolved is not None:
                    job_id = str(resolved[0])
            if job.idempotency_key and not lease_token:
                migrated = connection.execute(
                    """
                    SELECT request_digest, workflow_id, job_id, client_id
                    FROM idempotency_records
                    WHERE owner_id = ? AND scope = ? AND key = ?
                      AND state = 'submission_unknown' AND lease_token IS NULL
                    """,
                    (job.owner_id, _scope(job.server_id), job.idempotency_key),
                ).fetchone()
                if migrated is not None:
                    if (
                        str(migrated[0]) != job.request_digest
                        or str(migrated[1]) != job.workflow_id
                        or str(migrated[3]) != job.client_id
                        or not job.client_id
                    ):
                        raise RuntimeError("migrated unknown submission identity conflicts")
                    migrated_job_id = str(migrated[2])
                    updated = connection.execute(
                        """
                        UPDATE execution_attempts
                        SET upstream_prompt_id = ?, submission_state = 'submitted'
                        WHERE job_id = ? AND server_id = ?
                          AND submission_state = 'submission_unknown'
                          AND upstream_prompt_id IS NULL AND client_id = ?
                        """,
                        (job.prompt_id, migrated_job_id, job.server_id, job.client_id),
                    ).rowcount
                    if updated != 1:
                        raise RuntimeError("migrated unknown submission cannot be reconciled")
                    connection.execute(
                        """
                        UPDATE jobs SET status = ?, error = ?, outputs_json = ?
                        WHERE job_id = ? AND owner_id = ? AND workflow_id = ?
                        """,
                        (
                            job.status,
                            job.error,
                            _serialize_outputs(job.outputs),
                            migrated_job_id,
                            job.owner_id,
                            job.workflow_id,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE idempotency_records
                        SET state = 'resolved'
                        WHERE owner_id = ? AND scope = ? AND key = ?
                          AND state = 'submission_unknown' AND job_id = ?
                        """,
                        (
                            job.owner_id,
                            _scope(job.server_id),
                            job.idempotency_key,
                            migrated_job_id,
                        ),
                    )
                    connection.commit()
                    return
            existing = connection.execute(
                "SELECT status, owner_id, workflow_id, server_id FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if existing is not None and (
                str(existing[1]) != job.owner_id
                or str(existing[2]) != job.workflow_id
                or str(existing[3]) != job.server_id
            ):
                raise RuntimeError("job identity is already owned by a different request")
            existing_status = str(existing[0]) if existing is not None else ""
            if (
                existing is None
                or existing_status == job.status
                or (
                    existing_status not in _TERMINAL_STATUSES
                    and _STATUS_PRIORITY.get(existing_status, 0)
                    <= _STATUS_PRIORITY.get(job.status, 0)
                )
            ):
                connection.execute(
                    """
                    INSERT INTO jobs(
                        job_id, workflow_id, owner_id, server_id, status, created_at,
                        created_at_source, legacy_migrated, error, outputs_json,
                        execution_origin
                    ) VALUES (?, ?, ?, ?, ?, ?, 'runtime', 0, ?, ?, 'pre_g4_runtime')
                    ON CONFLICT(job_id) DO UPDATE SET
                        status = excluded.status, error = excluded.error,
                        outputs_json = excluded.outputs_json
                    """,
                    (
                        job_id,
                        job.workflow_id,
                        job.owner_id,
                        job.server_id,
                        job.status,
                        datetime.now(timezone.utc).isoformat(),
                        job.error,
                        _serialize_outputs(job.outputs),
                    ),
                )
            attempt_id = derive_legacy_attempt_id(job_id, job.server_id, 1)
            connection.execute(
                """
                INSERT OR IGNORE INTO execution_attempts(
                    attempt_id, job_id, attempt, server_id, upstream_prompt_id,
                    upstream_job_id, client_id, submission_state, created_at
                ) VALUES (?, ?, 1, ?, ?, NULL, ?, 'submitted', ?)
                """,
                (
                    attempt_id,
                    job_id,
                    job.server_id,
                    job.prompt_id,
                    job.client_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            if job.idempotency_key:
                record = connection.execute(
                    """
                    SELECT request_digest, state, job_id, lease_token, workflow_id
                    FROM idempotency_records
                    WHERE owner_id = ? AND scope = ? AND key = ?
                    """,
                    (job.owner_id, _scope(job.server_id), job.idempotency_key),
                ).fetchone()
                same_finalized = bool(
                    record
                    and record[1] == "resolved"
                    and record[2] == job_id
                    and record[0] == job.request_digest
                    and record[4] == job.workflow_id
                )
                owns_pending = bool(
                    record
                    and record[1] in {"reserved", "submission_unknown"}
                    and record[0] == job.request_digest
                    and record[3] == lease_token
                    and lease_token
                )
                if not same_finalized and not owns_pending:
                    raise RuntimeError("idempotency lease is no longer owned")
                if owns_pending:
                    connection.execute(
                        """
                        UPDATE idempotency_records
                        SET state = 'resolved', job_id = ?, expires_at = NULL,
                            lease_token = NULL
                        WHERE owner_id = ? AND scope = ? AND key = ?
                        """,
                        (job_id, job.owner_id, _scope(job.server_id), job.idempotency_key),
                    )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, server_id: str, prompt_id: str) -> Job | None:
        return self._read_job(
            "execution_attempts.server_id = ? AND execution_attempts.upstream_prompt_id = ?",
            (server_id, prompt_id),
        )

    def get_by_idempotency(self, server_id: str, key: str, owner_id: str = "") -> Job | None:
        return self._read_job(
            "idempotency_records.owner_id = ? AND idempotency_records.scope = ? "
            "AND idempotency_records.key = ? AND idempotency_records.state = 'resolved' "
            "AND execution_attempts.submission_state = 'submitted' "
            "AND execution_attempts.upstream_prompt_id IS NOT NULL",
            (owner_id, _scope(server_id), key),
            join_idempotency=True,
        )

    def list_jobs(
        self,
        owner_id: str,
        *,
        limit: int,
        status: str = "",
        workflow_id: str = "",
        server_id: str = "",
        created_after: str = "",
        after_created_at: str = "",
        after_job_id: str = "",
    ) -> list[dict[str, str]]:
        """Read an owner-bound page from the indexed canonical job facts."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 101:
            raise ValueError("limit must be an integer between 1 and 101")
        if bool(after_created_at) != bool(after_job_id):
            raise ValueError("keyset position requires created_at and job_id")
        predicates = ["jobs.owner_id = ?"]
        parameters: list[object] = [owner_id]
        index_name = "ix_jobs_owner_created"
        if status:
            predicates.append("jobs.status = ?")
            parameters.append(status)
            index_name = "ix_jobs_owner_status_created"
        if workflow_id:
            predicates.append("jobs.workflow_id = ?")
            parameters.append(workflow_id)
            if not status:
                index_name = "ix_jobs_owner_workflow_created"
        if server_id:
            predicates.append("latest_attempt.server_id = ?")
            parameters.append(server_id)
        if created_after:
            predicates.append("jobs.created_at > ?")
            parameters.append(created_after)
        if after_created_at:
            predicates.append("(jobs.created_at < ? OR (jobs.created_at = ? AND jobs.job_id > ?))")
            parameters.extend((after_created_at, after_created_at, after_job_id))
        parameters.append(limit)
        connection = self._connect()
        try:
            rows = connection.execute(
                f"""
                SELECT jobs.job_id, jobs.workflow_id,
                       COALESCE(jobs.revision_id, ''),
                       COALESCE(jobs.deployment_id, ''),
                       COALESCE(latest_attempt.server_id, ''),
                       jobs.status, jobs.created_at
                FROM jobs INDEXED BY {index_name}
                LEFT JOIN execution_attempts AS latest_attempt
                  ON latest_attempt.job_id = jobs.job_id
                 AND latest_attempt.attempt = (
                     SELECT MAX(candidate.attempt)
                     FROM execution_attempts AS candidate
                     WHERE candidate.job_id = jobs.job_id
                 )
                WHERE {" AND ".join(predicates)}
                ORDER BY jobs.created_at DESC, jobs.job_id ASC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        finally:
            connection.close()
        return [
            {
                "job_id": str(row[0]),
                "workflow_id": str(row[1]),
                "revision_id": str(row[2]),
                "deployment_id": str(row[3]),
                "server_id": str(row[4]),
                "status": str(row[5]),
                "created_at": str(row[6]),
            }
            for row in rows
        ]

    def _read_job(
        self,
        predicate: str,
        parameters: tuple[object, ...],
        *,
        join_idempotency: bool = False,
    ) -> Job | None:
        connection = self._connect()
        try:
            join = (
                "JOIN idempotency_records ON idempotency_records.job_id = jobs.job_id"
                if join_idempotency
                else "LEFT JOIN idempotency_records ON idempotency_records.job_id = jobs.job_id"
            )
            row = connection.execute(
                f"""
                SELECT execution_attempts.upstream_prompt_id, execution_attempts.server_id,
                       jobs.workflow_id, jobs.status, jobs.error, jobs.outputs_json,
                       COALESCE(idempotency_records.key, ''), execution_attempts.client_id,
                       COALESCE(idempotency_records.request_digest, ''), jobs.owner_id,
                       jobs.job_id, jobs.plan_id, jobs.revision_id, jobs.deployment_id,
                       COALESCE(execution_plans.plan_digest, '')
                FROM jobs
                JOIN execution_attempts ON execution_attempts.job_id = jobs.job_id
                LEFT JOIN execution_plans ON execution_plans.plan_id = jobs.plan_id
                {join}
                WHERE {predicate}
                """,
                parameters,
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return Job(
            prompt_id=row[0],
            server_id=row[1],
            workflow_id=row[2],
            status=row[3],
            error=row[4],
            outputs=_deserialize_outputs(row[5]),
            idempotency_key=row[6],
            client_id=row[7],
            request_digest=row[8],
            owner_id=row[9],
            job_id=row[10] if row[11] else "",
            plan_id=row[11] or "",
            revision_id=row[12] or "",
            deployment_id=row[13] or "",
            plan_digest=row[14] or "",
        )

    def _delete_reserved(
        self, server_id: str, key: str, owner_id: str, digest: str, lease: str
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                DELETE FROM idempotency_records
                WHERE owner_id = ? AND scope = ? AND key = ?
                  AND request_digest = ? AND state = 'reserved' AND lease_token = ?
                """,
                (owner_id, _scope(server_id), key, digest, lease),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _prompt_for_job(self, job_id: str) -> str:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT upstream_prompt_id FROM execution_attempts WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        finally:
            connection.close()
        return str(row[0]) if row and row[0] else ""

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._store.path, isolation_level=None, timeout=5.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA trusted_schema = OFF")
        return connection


def terminalize_job_snapshot(
    connection: sqlite3.Connection,
    job: Job,
    outputs_json: str,
) -> bool:
    """Verify and update a persisted Job inside the caller's open transaction."""
    if job.status != "completed" or not job.job_id or not job.owner_id:
        raise ValueError("canonical completed Job identity required")
    row = _terminalization_row(connection, job.job_id)
    if row is None or not _matches_execution_identity(row, job):
        raise RuntimeError("completed Job conflicts with persisted execution facts")
    persisted_status = str(row[5])
    if persisted_status == "completed":
        if str(row[6]) != job.error or str(row[7]) != outputs_json:
            raise RuntimeError("completed Job conflicts with persisted output snapshot")
        return False
    if persisted_status in _TERMINAL_STATUSES or str(row[7]) != "[]":
        raise RuntimeError("completed Job conflicts with persisted terminal state")
    updated = connection.execute(
        """UPDATE jobs SET status='completed', error=?, outputs_json=?
           WHERE job_id=? AND status=? AND owner_id=? AND workflow_id=?""",
        (
            job.error,
            outputs_json,
            job.job_id,
            persisted_status,
            job.owner_id,
            job.workflow_id,
        ),
    ).rowcount
    if updated != 1:
        raise RuntimeError("completed Job changed during terminalization")
    return True


def _terminalization_row(connection: sqlite3.Connection, job_id: str) -> Any:
    return connection.execute(
        """
        SELECT jobs.workflow_id, COALESCE(jobs.plan_id, ''),
               COALESCE(jobs.revision_id, ''), COALESCE(jobs.deployment_id, ''),
               jobs.owner_id, jobs.status, jobs.error, jobs.outputs_json,
               execution_attempts.server_id,
               COALESCE(execution_attempts.upstream_prompt_id, ''),
               execution_attempts.submission_state,
               COALESCE(execution_plans.plan_digest, '')
        FROM jobs JOIN execution_attempts
          ON execution_attempts.job_id = jobs.job_id
         AND execution_attempts.attempt = 1
        LEFT JOIN execution_plans ON execution_plans.plan_id = jobs.plan_id
        WHERE jobs.job_id = ?
        """,
        (job_id,),
    ).fetchone()


def _matches_execution_identity(row: Any, job: Job) -> bool:
    persisted = tuple(str(row[index]) for index in (0, 1, 2, 3, 4, 8, 9, 10, 11))
    expected = (
        job.workflow_id,
        job.plan_id,
        job.revision_id,
        job.deployment_id,
        job.owner_id,
        job.server_id,
        job.prompt_id,
        "submitted",
        job.plan_digest,
    )
    return persisted == expected


def serialize_job_outputs(outputs: Sequence[Mapping[str, Any]]) -> str:
    """Return the canonical compatibility snapshot stored on one Job."""
    return json.dumps(
        tuple(dict(output) for output in outputs),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _scope(server_id: str) -> str:
    return f"legacy-execute:{server_id}"


def _is_expired(value: object, now: datetime) -> bool:
    if not isinstance(value, str):
        return False
    try:
        expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return expires <= now


def _serialize_outputs(outputs: tuple[dict[str, Any], ...]) -> str:
    return json.dumps(
        outputs, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _deserialize_outputs(value: object) -> tuple[dict[str, Any], ...]:
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError, ValueError):
        return ()
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        return ()
    return tuple(parsed)
