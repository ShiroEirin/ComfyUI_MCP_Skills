"""SQLite persistence for approved runtime restart execution (drain/fence)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Literal

from comfyui_mcp_skills.domain.errors import (
    RestartApprovalInvalid,
    RestartFenced,
    RestartPlanConflict,
    RestartPlanNotFound,
)
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore

_TERMINAL_JOBS = frozenset({"completed", "error", "interrupted", "cancelled", "lost"})
_ACTIVE_STATUSES = ("draining", "restarting")
_DRAIN_LIMIT = 10000

_ACTIVE_PLAN_COLUMNS = """
    plan_id, approval_id, owner_id, server_id, plan_digest,
    approved_impact_summary_json, status, approval_actor, approval_reason,
    approval_decided_at, approval_expires_at, controller_binding_json,
    controller_binding_digest, controller_available,
    execution_impact_summary_json, execution_impact_digest,
    execution_intent_committed_at, commit_request_id, commit_result_json,
    committed_at, error, created_at, updated_at, expires_at, completed_at,
    resource_uri
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return an aware datetime")
    return value.astimezone(timezone.utc)


def _time(value: datetime) -> str:
    return _aware(value).isoformat()


def _encode(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _decode(value: object) -> dict[str, Any]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise ValueError("runtime restart payload must be an object")
    return parsed


def _row_plan(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "plan_id": str(row["plan_id"]),
        "approval_id": str(row["approval_id"]),
        "owner_id": str(row["owner_id"]),
        "server_id": str(row["server_id"]),
        "plan_digest": str(row["plan_digest"]),
        "approved_impact_summary": _decode(row["approved_impact_summary_json"]),
        "status": str(row["status"]),
        "approval_actor": row["approval_actor"],
        "approval_reason": str(row["approval_reason"]),
        "approval_decided_at": row["approval_decided_at"],
        "approval_expires_at": str(row["approval_expires_at"]),
        "controller_binding": _decode(row["controller_binding_json"]),
        "controller_binding_digest": str(row["controller_binding_digest"]),
        "controller_available": bool(row["controller_available"]),
        "execution_impact_summary": (
            _decode(row["execution_impact_summary_json"])
            if row["execution_impact_summary_json"] is not None
            else None
        ),
        "execution_impact_digest": row["execution_impact_digest"],
        "execution_intent_committed_at": row["execution_intent_committed_at"],
        "commit_request_id": row["commit_request_id"],
        "commit_result": (
            _decode(row["commit_result_json"]) if row["commit_result_json"] is not None else None
        ),
        "committed_at": row["committed_at"],
        "error": row["error"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "expires_at": str(row["expires_at"]),
        "completed_at": row["completed_at"],
        "resource_uri": str(row["resource_uri"]),
    }


class SQLiteRuntimeRestartRepository:
    """Own the restart session state machine with fencing and single-use approval."""

    def __init__(
        self,
        store: SQLiteControlPlaneStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # -- plan -------------------------------------------------------------

    def save_plan(
        self,
        *,
        plan_id: str,
        approval_id: str,
        owner_id: str,
        server_id: str,
        plan_digest: str,
        approved_summary: dict[str, Any],
        impact_rows: list[tuple[str, str, str]],
        controller_binding: dict[str, Any],
        controller_binding_digest: str,
        controller_available: bool,
        approval_expires_at: str,
        expires_at: str,
        now: datetime,
    ) -> None:
        now_text = _time(now)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO runtime_restart_plans(
                    plan_id, approval_id, owner_id, server_id, plan_digest,
                    approved_impact_summary_json, status, approval_expires_at,
                    controller_binding_json, controller_binding_digest,
                    controller_available, created_at, updated_at, expires_at, resource_uri
                ) VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    approval_id,
                    owner_id,
                    server_id,
                    plan_digest,
                    _encode(approved_summary),
                    approval_expires_at,
                    _encode(controller_binding),
                    controller_binding_digest,
                    int(controller_available),
                    now_text,
                    now_text,
                    expires_at,
                    f"comfyui://plans/{plan_id}",
                ),
            )
            connection.executemany(
                """
                INSERT INTO runtime_restart_impact_jobs(
                    plan_id, job_id, owner_id, status, ordinal
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (plan_id, job_id, job_owner, status, ordinal)
                    for ordinal, (job_id, job_owner, status) in enumerate(impact_rows)
                ],
            )

    def find_reusable_plan(
        self, owner_id: str, server_id: str, plan_digest: str, now: datetime
    ) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                f"""
                SELECT {_ACTIVE_PLAN_COLUMNS} FROM runtime_restart_plans
                WHERE owner_id = ? AND server_id = ? AND status = 'planned'
                  AND plan_digest = ?
                  AND julianday(approval_expires_at) > julianday(?)
                ORDER BY created_at DESC LIMIT 1
                """,
                (owner_id, server_id, plan_digest, _time(now)),
            ).fetchone()
            return _row_plan(row) if row is not None else None
        finally:
            connection.close()

    def get_plan(self, plan_id: str, owner_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                f"""
                SELECT {_ACTIVE_PLAN_COLUMNS} FROM runtime_restart_plans
                WHERE plan_id = ? AND owner_id = ?
                """,
                (plan_id, owner_id),
            ).fetchone()
            return _row_plan(row) if row is not None else None
        finally:
            connection.close()

    def impact_jobs(
        self, plan_id: str, *, limit: int = 200, cursor: int = 0
    ) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT job_id, owner_id, status, ordinal FROM runtime_restart_impact_jobs
                WHERE plan_id = ? AND ordinal >= ? ORDER BY ordinal LIMIT ?
                """,
                (plan_id, cursor, limit),
            ).fetchall()
            return [
                {
                    "job_id": str(row[0]),
                    "owner_id": str(row[1]),
                    "status": str(row[2]),
                    "ordinal": int(row[3]),
                }
                for row in rows
            ]
        finally:
            connection.close()

    def impact_count(self, plan_id: str) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT count(*) FROM runtime_restart_impact_jobs WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            return int(row[0])
        finally:
            connection.close()

    # -- approval ---------------------------------------------------------

    def approve(
        self,
        plan_id: str,
        decision: str,
        owner_id: str,
        reason: str,
        now: datetime,
    ) -> dict[str, Any]:
        now_text = _time(now)
        status = "approved" if decision == "approved" else "rejected"
        with self._transaction() as connection:
            updated = connection.execute(
                """
                UPDATE runtime_restart_plans
                SET status = ?, approval_actor = ?, approval_reason = ?,
                    approval_decided_at = ?, updated_at = ?
                WHERE plan_id = ? AND owner_id = ? AND status = 'planned'
                  AND julianday(approval_expires_at) > julianday(?)
                """,
                (status, owner_id, reason, now_text, now_text, plan_id, owner_id, now_text),
            ).rowcount
            if updated != 1:
                row = connection.execute(
                    "SELECT status, approval_decided_at FROM runtime_restart_plans "
                    "WHERE plan_id = ? AND owner_id = ?",
                    (plan_id, owner_id),
                ).fetchone()
                if row is None:
                    raise RestartPlanNotFound("Restart plan was not found")
                if str(row[0]) != "planned":
                    raise RestartApprovalInvalid("Restart plan is not pending approval")
                raise RestartApprovalInvalid("Restart approval has expired")
            row = connection.execute(
                f"SELECT {_ACTIVE_PLAN_COLUMNS} FROM runtime_restart_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            return _row_plan(row)

    # -- commit -----------------------------------------------------------

    def receipt(self, plan_id: str, request_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                f"""
                SELECT {_ACTIVE_PLAN_COLUMNS} FROM runtime_restart_plans
                WHERE plan_id = ? AND commit_request_id = ?
                """,
                (plan_id, request_id),
            ).fetchone()
            return _row_plan(row) if row is not None else None
        finally:
            connection.close()

    def begin_drain(self, plan_id: str, request_id: str, now: datetime) -> dict[str, Any]:
        """approved -> draining with the fence opened and the execution intent
        enumerated and persisted inside the SAME transaction, so no gap exists
        between the fence and the snapshot. The active partial unique index
        serializes concurrent restarts of the same server."""
        now_text = _time(now)
        try:
            with self._transaction() as connection:
                updated = connection.execute(
                    """
                    UPDATE runtime_restart_plans
                    SET status = 'draining', commit_request_id = ?,
                        updated_at = ?
                    WHERE plan_id = ? AND status = 'approved'
                      AND julianday(approval_expires_at) > julianday(?)
                    """,
                    (request_id, now_text, plan_id, now_text),
                ).rowcount
                if updated != 1:
                    row = connection.execute(
                        "SELECT status FROM runtime_restart_plans WHERE plan_id = ?",
                        (plan_id,),
                    ).fetchone()
                    if row is None:
                        raise RestartPlanNotFound("Restart plan was not found")
                    status = str(row[0])
                    if status in _ACTIVE_STATUSES:
                        raise RestartPlanConflict("A restart is already draining or running")
                    raise RestartApprovalInvalid("Restart plan is not approved or has expired")
                row = connection.execute(
                    f"SELECT {_ACTIVE_PLAN_COLUMNS} FROM runtime_restart_plans WHERE plan_id = ?",
                    (plan_id,),
                ).fetchone()
                plan = _row_plan(row)
                # Same-transaction intent: enumerate active jobs on this connection
                # and persist the summary/digest while the fence is held.
                summary, digest = self._intent_on(connection, plan["server_id"], now_text)
                connection.execute(
                    """
                    UPDATE runtime_restart_plans
                    SET execution_impact_summary_json = ?, execution_impact_digest = ?,
                        execution_intent_committed_at = ?, updated_at = ?
                    WHERE plan_id = ?
                    """,
                    (_encode(summary), digest, now_text, now_text, plan_id),
                )
                plan["execution_impact_summary"] = summary
                plan["execution_impact_digest"] = digest
                plan["execution_intent_committed_at"] = now_text
                return plan
        except sqlite3.IntegrityError:
            raise RestartPlanConflict("A restart is already draining or running") from None

    def refresh_execution_intent(self, plan_id: str, now: datetime) -> dict[str, Any]:
        """Final drain check and intent refresh in one transaction: admissions
        must be zero on this connection, then enumerate, update the intent and
        move draining -> restarting atomically."""
        now_text = _time(now)
        with self._transaction() as connection:
            updated = connection.execute(
                """
                UPDATE runtime_restart_plans
                SET status = 'restarting', updated_at = ?
                WHERE plan_id = ? AND status = 'draining'
                """,
                (now_text, plan_id),
            ).rowcount
            if updated != 1:
                raise RestartPlanConflict("Restart plan is no longer draining")
            row = connection.execute(
                f"SELECT {_ACTIVE_PLAN_COLUMNS} FROM runtime_restart_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            plan = _row_plan(row)
            pending = connection.execute(
                "SELECT count(*) FROM runtime_submission_admissions WHERE server_id = ?",
                (plan["server_id"],),
            ).fetchone()
            if int(pending[0]) != 0:
                raise RestartPlanConflict(
                    "submissions did not settle within the drain window "
                    f"({int(pending[0])} pending)"
                )
            summary, digest = self._intent_on(connection, plan["server_id"], now_text)
            connection.execute(
                """
                UPDATE runtime_restart_plans
                SET execution_impact_summary_json = ?, execution_impact_digest = ?,
                    execution_intent_committed_at = ?, updated_at = ?
                WHERE plan_id = ?
                """,
                (_encode(summary), digest, now_text, now_text, plan_id),
            )
            plan["execution_impact_summary"] = summary
            plan["execution_impact_digest"] = digest
            plan["execution_intent_committed_at"] = now_text
            return plan

    def complete(
        self, plan_id: str, result: dict[str, Any], now: datetime
    ) -> dict[str, Any]:
        return self._terminal(plan_id, result, error=None, now=now)

    def fail(
        self,
        plan_id: str,
        result: dict[str, Any],
        *,
        error: str,
        now: datetime,
    ) -> dict[str, Any]:
        return self._terminal(plan_id, result, error=error, now=now)

    def _terminal(
        self,
        plan_id: str,
        result: dict[str, Any],
        *,
        error: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        now_text = _time(now)
        status = "failed" if error is not None else "completed"
        with self._transaction() as connection:
            updated = connection.execute(
                """
                UPDATE runtime_restart_plans
                SET status = ?, commit_result_json = ?, committed_at = ?,
                    completed_at = ?, error = ?, updated_at = ?
                WHERE plan_id = ? AND status IN ('draining', 'restarting')
                """,
                (
                    status,
                    _encode(result),
                    now_text,
                    now_text,
                    error,
                    now_text,
                    plan_id,
                ),
            ).rowcount
            if updated != 1:
                raise RestartPlanConflict("Restart plan is not in an executing state")
            row = connection.execute(
                f"SELECT {_ACTIVE_PLAN_COLUMNS} FROM runtime_restart_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            return _row_plan(row)

    # -- snapshots and fence ----------------------------------------------

    def server_active_jobs(
        self, server_id: str, *, limit: int = 10000
    ) -> list[tuple[str, str, str]]:
        if not 1 <= limit <= 10000:
            raise ValueError("active job enumeration is bounded to 10000")
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT job_id, owner_id, status FROM jobs
                WHERE server_id = ?
                  AND status NOT IN ('completed', 'error', 'interrupted', 'cancelled', 'lost')
                ORDER BY created_at, job_id LIMIT ?
                """,
                (server_id, limit + 1),
            ).fetchall()
            if len(rows) > limit:
                raise ValueError("active job impact exceeds the enumeration bound")
            known = _TERMINAL_JOBS | {"queued", "submitted", "running"}
            result: list[tuple[str, str, str]] = []
            for row in rows:
                status = str(row[2])
                if status not in known:
                    raise ValueError(f"job status is not a known value: {status}")
                result.append((str(row[0]), str(row[1]), status))
            return result
        finally:
            connection.close()

    def _intent_on(
        self, connection: sqlite3.Connection, server_id: str, now_text: str
    ) -> tuple[dict[str, Any], str]:
        """Enumerate the active jobs on the caller's connection and derive the
        execution intent summary/digest (same transaction as the fence)."""
        rows = connection.execute(
            """
            SELECT job_id, owner_id, status FROM jobs
            WHERE server_id = ?
              AND status NOT IN ('completed', 'error', 'interrupted', 'cancelled', 'lost')
            ORDER BY created_at, job_id LIMIT ?
            """,
            (server_id, _DRAIN_LIMIT + 1),
        ).fetchall()
        if len(rows) > _DRAIN_LIMIT:
            raise ValueError("active job impact exceeds the enumeration bound")
        known = _TERMINAL_JOBS | {"queued", "submitted", "running"}
        impact: list[tuple[str, str, str]] = []
        for row in rows:
            status = str(row[2])
            if status not in known:
                raise ValueError(f"job status is not a known value: {status}")
            impact.append((str(row[0]), str(row[1]), status))
        pending = connection.execute(
            "SELECT count(*) FROM runtime_submission_admissions WHERE server_id = ?",
            (server_id,),
        ).fetchone()
        summary = {
            "job_count": len(impact),
            "pending_admissions": int(pending[0]),
            "enumerated_at": now_text,
        }
        digest = hashlib.sha256(
            json.dumps(
                ["runtime_restart_execution", server_id, impact],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return summary, digest

    def active_restart(self, server_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                f"""
                SELECT {_ACTIVE_PLAN_COLUMNS} FROM runtime_restart_plans
                WHERE server_id = ? AND status IN ('draining', 'restarting')
                """,
                (server_id,),
            ).fetchone()
            return _row_plan(row) if row is not None else None
        finally:
            connection.close()

    def admit(self, server_id: str, admission_id: str, now: datetime) -> str:
        """Atomic admission gate: fenced servers reject new submissions inside
        the same write transaction that would later open the fence."""
        now_text = _time(now)
        with self._transaction() as connection:
            fence = connection.execute(
                "SELECT plan_id FROM runtime_restart_plans "
                "WHERE server_id = ? AND status IN ('draining', 'restarting')",
                (server_id,),
            ).fetchone()
            if fence is not None:
                raise RestartFenced(
                    "host restart in progress; submission is fenced",
                    details={"plan_id": str(fence[0]), "server_id": server_id},
                )
            connection.execute(
                "INSERT INTO runtime_submission_admissions(admission_id, server_id, created_at) "
                "VALUES (?, ?, ?)",
                (admission_id, server_id, now_text),
            )
            return admission_id

    def release_admission(self, admission_id: str) -> None:
        if not admission_id:
            return
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM runtime_submission_admissions WHERE admission_id = ?",
                (admission_id,),
            )

    def pending_admissions(self, server_id: str) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT count(*) FROM runtime_submission_admissions WHERE server_id = ?",
                (server_id,),
            ).fetchone()
            return int(row[0])
        finally:
            connection.close()

    def clear_admissions(self) -> int:
        with self._transaction() as connection:
            row = connection.execute("DELETE FROM runtime_submission_admissions")
            return int(row.rowcount)

    # -- recovery ---------------------------------------------------------

    def recover_orphaned(self, now: datetime) -> list[dict[str, Any]]:
        """Crash recovery: mark draining/restarting sessions failed and release
        the fence. Never auto-retries the external restart (double-restart risk)."""
        now_text = _time(now)
        recovered: list[dict[str, Any]] = []
        with self._transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT {_ACTIVE_PLAN_COLUMNS} FROM runtime_restart_plans
                WHERE status IN ('draining', 'restarting')
                """,
            ).fetchall()
            for row in rows:
                plan = _row_plan(row)
                summary = plan["execution_impact_summary"] or {
                    "job_count": 0,
                    "source": "recovery",
                }
                receipt = {
                    "status": "failed",
                    "execution_impact_summary": summary,
                    "execution_impact_digest": plan["execution_impact_digest"],
                    "error_code": "RESTART_INTERRUPTED_UNKNOWN",
                    "retryable": True,
                    "details": {"message": "restart interrupted by process exit"},
                    "committed_at": now_text,
                }
                connection.execute(
                    """
                    UPDATE runtime_restart_plans
                    SET status = 'failed', commit_result_json = ?, committed_at = ?,
                        completed_at = ?, error = 'restart_interrupted_unknown',
                        updated_at = ?
                    WHERE plan_id = ?
                    """,
                    (
                        _encode(receipt),
                        now_text,
                        now_text,
                        now_text,
                        plan["plan_id"],
                    ),
                )
                plan["status"] = "failed"
                plan["error"] = "restart_interrupted_unknown"
                recovered.append(plan)
        return recovered

    def _connect(self) -> sqlite3.Connection:
        return self._store._connect()

    def _transaction(self) -> _Transaction:
        return _Transaction(self._connect())


class _Transaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
        except BaseException:
            self.connection.close()
            raise
        return self.connection

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> Literal[False]:
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()
        return False
