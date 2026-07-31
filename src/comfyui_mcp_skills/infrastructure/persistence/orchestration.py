"""SQLite repositories for durable G5 work, leases, events, and outbox delivery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from comfyui_mcp_skills.domain.control_plane import validate_control_plane_id
from comfyui_mcp_skills.domain.orchestration import (
    JobReconciliationContext,
    OutboxMessage,
    WorkItem,
    WorkLease,
)
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore

_TERMINAL_JOB_STATUSES = frozenset({"completed", "error", "interrupted", "cancelled", "lost"})


class SQLiteOrchestrationRepository:
    """Persist work progress with expiring leases and fencing-token guarded writes."""

    def __init__(self, store: SQLiteControlPlaneStore) -> None:
        self._store = store

    def acquire_next(
        self, worker_id: str, *, now: datetime, lease_seconds: int = 30
    ) -> WorkLease | None:
        if not worker_id or lease_seconds <= 0:
            raise ValueError("worker_id and a positive lease_seconds are required")
        now_text = _time(now)
        expires_at = _time(now + timedelta(seconds=lease_seconds))
        connection = self._store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT work.work_item_id, COALESCE(lease.fencing_token, 0)
                FROM operation_work_items AS work
                LEFT JOIN work_leases AS lease USING(work_item_id)
                WHERE work.status IN ('pending', 'running')
                  AND julianday(work.next_attempt_at) <= julianday(?)
                  AND (lease.work_item_id IS NULL OR julianday(lease.expires_at) <= julianday(?))
                ORDER BY work.next_attempt_at, work.created_at, work.work_item_id
                LIMIT 1
                """,
                (now_text, now_text),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            work_item_id, fencing_token = str(row[0]), int(row[1]) + 1
            connection.execute(
                """
                INSERT INTO work_leases(
                    work_item_id, worker_id, fencing_token, acquired_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(work_item_id) DO UPDATE SET
                    worker_id = excluded.worker_id,
                    fencing_token = excluded.fencing_token,
                    acquired_at = excluded.acquired_at,
                    expires_at = excluded.expires_at
                WHERE julianday(work_leases.expires_at) <= julianday(excluded.acquired_at)
                """,
                (work_item_id, worker_id, fencing_token, now_text, expires_at),
            )
            connection.execute(
                "UPDATE operation_work_items SET status = 'running', updated_at = ? "
                "WHERE work_item_id = ?",
                (now_text, work_item_id),
            )
            connection.commit()
            return WorkLease(work_item_id, worker_id, fencing_token, expires_at)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_work_item(self, work_item_id: str) -> WorkItem:
        connection = self._store._connect()
        try:
            row = connection.execute(
                """
                SELECT work_item_id, subject_uri, work_type, payload_json,
                       checkpoint_json, status
                FROM operation_work_items WHERE work_item_id = ?
                """,
                (work_item_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise LookupError(f"work item not found: {work_item_id}")
        return WorkItem(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            _decode(row[3]),
            _decode(row[4]),
            str(row[5]),  # type: ignore[arg-type]
        )

    def job_context(self, job_id: str) -> JobReconciliationContext:
        connection = self._store._connect()
        try:
            row = connection.execute(
                """
                SELECT jobs.status, execution_attempts.server_id,
                       COALESCE(execution_attempts.upstream_prompt_id, ''),
                       COALESCE(execution_attempts.upstream_job_id, ''),
                       execution_attempts.client_id
                FROM jobs
                JOIN execution_attempts ON execution_attempts.job_id = jobs.job_id
                WHERE jobs.job_id = ?
                ORDER BY execution_attempts.attempt DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise LookupError(f"job execution attempt not found: {job_id}")
        return JobReconciliationContext(
            status=str(row[0]),
            server_id=str(row[1]),
            prompt_id=str(row[2]),
            upstream_job_id=str(row[3]),
            client_id=str(row[4]),
        )

    def checkpoint(
        self,
        lease: WorkLease,
        checkpoint: dict[str, Any],
        *,
        now: datetime,
        delay_seconds: int = 0,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")
        now_text = _time(now)
        next_attempt = _time(now + timedelta(seconds=delay_seconds))
        connection = self._store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._require_lease(connection, lease, now_text)
            connection.execute(
                """
                UPDATE operation_work_items
                SET checkpoint_json = ?, status = 'pending', next_attempt_at = ?, updated_at = ?
                WHERE work_item_id = ?
                """,
                (_encode(checkpoint), next_attempt, now_text, lease.work_item_id),
            )
            connection.execute(
                "UPDATE work_leases SET expires_at = ? WHERE work_item_id = ?",
                (now_text, lease.work_item_id),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def apply_reconciliation(
        self,
        lease: WorkLease,
        *,
        checkpoint: dict[str, Any],
        now: datetime,
        job_status: str | None = None,
        completed: bool = False,
        event_type: str = "",
        event_data: dict[str, Any] | None = None,
        generation: str = "",
        upstream_prompt_id: str = "",
        delay_seconds: int = 0,
    ) -> None:
        now_text = _time(now)
        connection = self._store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._require_lease(connection, lease, now_text)
            work = connection.execute(
                "SELECT subject_uri, payload_json FROM operation_work_items WHERE work_item_id = ?",
                (lease.work_item_id,),
            ).fetchone()
            if work is None:
                raise RuntimeError("leased work item disappeared")
            payload = _decode(work[1])
            job_id = str(payload["job_id"])
            server_id = str(payload["server_id"])
            current = connection.execute(
                "SELECT status, owner_id FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if current is None:
                raise RuntimeError("reconciled job does not exist")
            owner_id = str(current[1])
            if generation:
                connection.execute(
                    """
                    INSERT INTO server_generation_observations(server_id, generation, observed_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(server_id) DO UPDATE SET
                        generation = excluded.generation, observed_at = excluded.observed_at
                    """,
                    (server_id, generation, now_text),
                )
            if upstream_prompt_id:
                attempt = connection.execute(
                    """
                    SELECT attempt, server_id, upstream_prompt_id, submission_state
                    FROM execution_attempts
                    WHERE job_id = ? ORDER BY attempt DESC LIMIT 1
                    """,
                    (job_id,),
                ).fetchone()
                if attempt is None or str(attempt[1]) != server_id:
                    raise RuntimeError("reconciled execution attempt is unavailable")
                existing_prompt = attempt[2]
                if existing_prompt is not None and str(existing_prompt) != upstream_prompt_id:
                    raise RuntimeError("recovered prompt identity conflicts with attempt")
                if existing_prompt is None:
                    connection.execute(
                        """
                        UPDATE execution_attempts
                        SET upstream_prompt_id = ?, submission_state = 'submitted'
                        WHERE job_id = ? AND attempt = ? AND server_id = ?
                        """,
                        (upstream_prompt_id, job_id, int(attempt[0]), server_id),
                    )
            if job_status is not None and str(current[0]) not in _TERMINAL_JOB_STATUSES:
                connection.execute(
                    "UPDATE jobs SET status = ? WHERE job_id = ?", (job_status, job_id)
                )
            status = "completed" if completed else "pending"
            next_attempt = _time(now + timedelta(seconds=max(delay_seconds, 0)))
            connection.execute(
                """
                UPDATE operation_work_items
                SET checkpoint_json = ?, status = ?, next_attempt_at = ?, updated_at = ?
                WHERE work_item_id = ?
                """,
                (_encode(checkpoint), status, next_attempt, now_text, lease.work_item_id),
            )
            if event_type:
                self._append_event_and_outbox(
                    connection,
                    event_type=event_type,
                    subject_uri=str(work[0]),
                    correlation_id=lease.work_item_id,
                    principal_id=owner_id,
                    data=event_data or {},
                    occurred_at=now_text,
                )
            connection.execute(
                "UPDATE work_leases SET expires_at = ? WHERE work_item_id = ?",
                (now_text, lease.work_item_id),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def job_owner_for_uri(self, uri: str) -> str | None:
        prefix = "comfyui://jobs/"
        if not uri.startswith(prefix) or "/" in uri[len(prefix) :]:
            return None
        try:
            job_id = validate_control_plane_id("job", uri[len(prefix) :])
        except ValueError:
            return None
        connection = self._store._connect()
        try:
            row = connection.execute(
                "SELECT owner_id FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else str(row[0])

    def pending_outbox(self, *, limit: int = 100) -> list[OutboxMessage]:
        if limit <= 0:
            return []
        connection = self._store._connect()
        try:
            rows = connection.execute(
                """
                SELECT outbox_id, topic, payload_json FROM outbox
                WHERE status IN ('pending', 'failed') AND topic = 'resources.updated'
                ORDER BY created_at, outbox_id LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            connection.close()
        messages: list[OutboxMessage] = []
        for row in rows:
            try:
                payload = _decode(row[2])
            except (TypeError, ValueError):
                payload = {}
            messages.append(OutboxMessage(str(row[0]), str(row[1]), payload))
        return messages

    def mark_outbox_delivered(self, outbox_id: str, *, now: datetime) -> None:
        connection = self._store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE outbox SET status = 'delivered', delivered_at = ?
                WHERE outbox_id = ? AND status IN ('pending', 'failed')
                """,
                (_time(now), outbox_id),
            ).rowcount
            if updated != 1:
                delivered = connection.execute(
                    "SELECT status FROM outbox WHERE outbox_id = ?", (outbox_id,)
                ).fetchone()
                if delivered is None or str(delivered[0]) != "delivered":
                    raise RuntimeError("outbox message is not pending")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _require_lease(connection: sqlite3.Connection, lease: WorkLease, now_text: str) -> None:
        row = connection.execute(
            """
            SELECT 1 FROM work_leases
            WHERE work_item_id = ? AND worker_id = ? AND fencing_token = ?
              AND julianday(expires_at) >= julianday(?)
            """,
            (lease.work_item_id, lease.worker_id, lease.fencing_token, now_text),
        ).fetchone()
        if row is None:
            raise RuntimeError("work lease is expired or fenced")

    @staticmethod
    def _append_event_and_outbox(
        connection: sqlite3.Connection,
        *,
        event_type: str,
        subject_uri: str,
        correlation_id: str,
        data: dict[str, Any],
        occurred_at: str,
        principal_id: str,
    ) -> None:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM domain_events WHERE subject_uri = ?",
            (subject_uri,),
        ).fetchone()
        sequence = int(row[0])
        event_id = _stable_id("event", correlation_id, str(sequence), event_type)
        outbox_id = _stable_id("outbox", event_id)
        connection.execute(
            """
            INSERT INTO domain_events(
                event_id, event_type, subject_uri, sequence, occurred_at,
                principal_id, correlation_id, data_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                event_type,
                subject_uri,
                sequence,
                occurred_at,
                principal_id,
                correlation_id,
                _encode(data),
            ),
        )
        connection.execute(
            """
            INSERT INTO outbox(outbox_id, event_id, topic, payload_json, status, created_at)
            VALUES (?, ?, 'resources.updated', ?, 'pending', ?)
            """,
            (
                outbox_id,
                event_id,
                _encode({"uri": subject_uri, "sequence": sequence, "owner_id": principal_id}),
                occurred_at,
            ),
        )


def _stable_id(prefix: str, *components: str) -> str:
    payload = json.dumps(
        [prefix, "g5-orchestration-v1", *components], separators=(",", ":")
    ).encode()
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()}"


def _encode(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _decode(value: object) -> dict[str, Any]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise RuntimeError("orchestration payload must be an object")
    return parsed


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("orchestration timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()
