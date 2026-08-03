"""Owner-bound SQLite persistence for structured diagnostics and retry plans."""
# ruff: noqa: E501

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore

_MAX_LIMIT = 64
_MAX_LINE = 2048
_AUTHORIZATION = re.compile(
    r"""(?ix)(["']?authorization["']?\s*[:=]\s*["']?)(?:basic|bearer)\s+[A-Za-z0-9._~+/=-]+["']?"""
)
_SECRET = re.compile(
    r"""(?ix)(["']?(?:password|passwd|secret|token|api[_-]?key)["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}\]]+)"""
)
_PATH = re.compile(r"(?i)(?:[a-z]:\\|/)(?:[^\s\\/]+[\\/])*[^\s,;]+")


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _limit(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_LIMIT:
        raise ValueError(f"{field} must be between 1 and {_MAX_LIMIT}")
    return value


def _redact(value: object) -> str:
    text = str(value)
    text = _AUTHORIZATION.sub(lambda m: f"{m.group(1)}<redacted>", text)
    text = _SECRET.sub(lambda m: f"{m.group(1)}<redacted>", text)
    return _PATH.sub("<path>", text)[:_MAX_LINE]


def _safe_json(text: str) -> object:
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return {}


class SQLiteDiagnosticRetryRepository:
    """Persist immutable Diagnostic Reports and append-once retry evidence."""

    def __init__(self, store: SQLiteControlPlaneStore) -> None:
        self._store = store

    def get_job_diagnostic_context(
        self, job_id: str, owner_id: str, *, event_limit: int, log_line_limit: int
    ) -> dict[str, Any] | None:
        event_limit, log_line_limit = (
            _limit(event_limit, "event_limit"),
            _limit(log_line_limit, "log_line_limit"),
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT job_id,owner_id,workflow_id,server_id,status,error FROM jobs WHERE job_id=? AND owner_id=?",
                (job_id, owner_id),
            ).fetchone()
            return (
                None
                if row is None
                else self._job_context(connection, row, event_limit, log_line_limit)
            )
        finally:
            connection.close()

    def get_server_diagnostic_context(
        self, server_id: str, owner_id: str, *, event_limit: int, log_line_limit: int
    ) -> dict[str, Any] | None:
        event_limit, log_line_limit = (
            _limit(event_limit, "event_limit"),
            _limit(log_line_limit, "log_line_limit"),
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT job_id,owner_id,workflow_id,server_id,status,error FROM jobs WHERE owner_id=? AND server_id=? ORDER BY created_at DESC,job_id DESC LIMIT 1",
                (owner_id, server_id),
            ).fetchone()
            if row is None:
                if (
                    connection.execute(
                        "SELECT 1 FROM jobs WHERE server_id=? LIMIT 1", (server_id,)
                    ).fetchone()
                    is not None
                ):
                    return None
                return {
                    "server_id": server_id,
                    "owner_id": owner_id,
                    "status": "unknown",
                    "error": "",
                    "failed_node": {},
                    "events": [],
                    "log_lines": [],
                }
            result = self._job_context(connection, row, event_limit, log_line_limit)
            result["job_id"] = ""
            result["server_id"] = server_id
            return result
        finally:
            connection.close()

    def _job_context(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        event_limit: int,
        log_line_limit: int,
    ) -> dict[str, Any]:
        job_id, owner_id, workflow_id, server_id, status, error = (str(item) for item in row)
        subject_uri = f"comfyui://jobs/{job_id}"
        classification_parts = [error]
        event_rows = connection.execute(
            "SELECT event_type,occurred_at,data_json FROM domain_events WHERE subject_uri=? AND principal_id=? ORDER BY sequence DESC LIMIT ?",
            (subject_uri, owner_id, event_limit),
        ).fetchall()
        events: list[dict[str, str]] = []
        for event in reversed(event_rows):
            data = _safe_json(str(event[2]))
            message = data.get("message", "") if isinstance(data, dict) else ""
            classification_parts.append(str(message))
            events.append(
                {
                    "event_type": _redact(event[0]),
                    "occurred_at": _redact(event[1]),
                    "message": _redact(message),
                }
            )
        logs = [_redact(line) for line in str(error).splitlines() if line][:log_line_limit]
        return {
            "job_id": job_id,
            "owner_id": owner_id,
            "workflow_id": workflow_id,
            "server_id": server_id,
            "status": status,
            "error": _redact(error),
            "classification_text": "\n".join(classification_parts)[:32768],
            "failed_node": {},
            "events": events,
            "log_lines": logs,
        }

    def save_diagnostic(self, report: dict[str, Any]) -> dict[str, Any]:
        value = _validate_report(report)
        uri = str(value["subject_uri"])
        is_job = uri.startswith("comfyui://jobs/")
        subject_id = uri.rsplit("/", 1)[-1]
        row = (
            value["diagnostic_id"],
            value["owner_id"],
            value["registry_version"],
            uri,
            "job" if is_job else "server",
            subject_id if is_job else None,
            None if is_job else subject_id,
            value["classification"],
            value["rule_id"],
            int(value["retryable"]),
            _json(value["evidence"]),
            _json(value["safe_actions"]),
            _json(value["approval_actions"]),
            value["created_at"],
            value["resource_uri"],
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT diagnostic_id,owner_id,registry_version,subject_uri,classification,rule_id,retryable,evidence_json,safe_actions_json,approval_actions_json,created_at,resource_uri FROM diagnostic_reports WHERE diagnostic_id=?",
                (value["diagnostic_id"],),
            ).fetchone()
            compare = row[:4] + row[7:]
            if existing is not None:
                if str(existing[1]) != value["owner_id"] or tuple(existing) != compare:
                    raise ValueError("diagnostic identity conflict")
                connection.commit()
                return copy.deepcopy(value)
            connection.execute(
                "INSERT INTO diagnostic_reports(diagnostic_id,owner_id,registry_version,subject_uri,subject_kind,job_id,server_id,classification,rule_id,retryable,evidence_json,safe_actions_json,approval_actions_json,created_at,resource_uri) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            _event(
                connection,
                "DIAGNOSTIC_RECORDED",
                uri,
                value["diagnostic_id"],
                value["owner_id"],
                value["created_at"],
                {
                    "diagnostic_id": value["diagnostic_id"],
                    "classification": value["classification"],
                },
            )
            connection.commit()
            return copy.deepcopy(value)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_diagnostic(self, diagnostic_id: str, owner_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT diagnostic_id,owner_id,registry_version,subject_uri,classification,rule_id,retryable,evidence_json,safe_actions_json,approval_actions_json,created_at,resource_uri FROM diagnostic_reports WHERE diagnostic_id=? AND owner_id=?",
                (diagnostic_id, owner_id),
            ).fetchone()
            if row is None:
                return None
            return {
                "diagnostic_id": str(row[0]),
                "owner_id": str(row[1]),
                "registry_version": str(row[2]),
                "subject_uri": str(row[3]),
                "classification": str(row[4]),
                "rule_id": str(row[5]),
                "retryable": bool(row[6]),
                "evidence": json.loads(str(row[7])),
                "safe_actions": json.loads(str(row[8])),
                "approval_actions": json.loads(str(row[9])),
                "created_at": str(row[10]),
                "resource_uri": str(row[11]),
            }
        finally:
            connection.close()

    def get_retry_context(self, job_id: str, owner_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT jobs.job_id,jobs.owner_id,jobs.workflow_id,jobs.server_id,jobs.status,jobs.plan_id,jobs.revision_id,jobs.deployment_id,revisions.content_digest,plans.resolved_inputs_json,jobs.legacy_migrated FROM jobs LEFT JOIN execution_plans AS plans ON plans.plan_id=jobs.plan_id LEFT JOIN workflow_revisions AS revisions ON revisions.workflow_id=jobs.workflow_id AND revisions.revision_id=jobs.revision_id WHERE jobs.job_id=? AND jobs.owner_id=?",
                (job_id, owner_id),
            ).fetchone()
            if row is None:
                return None
            raw: object = _safe_json(str(row[9])) if row[9] else {}
            args = raw.get("arguments", {}) if isinstance(raw, dict) else {}
            return {
                "job_id": str(row[0]),
                "owner_id": str(row[1]),
                "workflow_id": str(row[2]),
                "server_id": str(row[3]),
                "status": str(row[4]),
                "plan_id": row[5] or "",
                "revision_id": row[6] or "",
                "deployment_id": row[7] or "",
                "content_digest": row[8] or "",
                "raw_arguments": copy.deepcopy(args) if isinstance(args, dict) else {},
                "legacy_migrated": bool(row[10]),
            }
        finally:
            connection.close()

    def save_repair_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        value = _validate_plan(plan)
        row = _plan_row(value)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT repair_plan_id,plan_digest,owner_id,resource_uri,original_job_id,workflow_id,server_id,pinned_plan_id,pinned_revision_id,pinned_deployment_id,pinned_content_digest,original_arguments_json,original_arguments_digest,normalized_changes_json,resulting_arguments_json,resulting_arguments_digest,diff_json,status,created_at,expires_at FROM repair_plans WHERE repair_plan_id=?",
                (value["repair_plan_id"],),
            ).fetchone()
            if existing is not None:
                if str(existing[2]) != value["owner_id"] or tuple(existing) != row:
                    raise ValueError("repair plan identity conflict")
                connection.commit()
                return self._get_repair_plan(
                    connection, value["repair_plan_id"], value["owner_id"]
                ) or copy.deepcopy(value)
            connection.execute(
                "INSERT INTO repair_plans(repair_plan_id,plan_digest,owner_id,resource_uri,original_job_id,workflow_id,server_id,pinned_plan_id,pinned_revision_id,pinned_deployment_id,pinned_content_digest,original_arguments_json,original_arguments_digest,normalized_changes_json,resulting_arguments_json,resulting_arguments_digest,diff_json,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            connection.commit()
            return self._get_repair_plan(
                connection, value["repair_plan_id"], value["owner_id"]
            ) or copy.deepcopy(value)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_repair_plan(self, repair_plan_id: str, owner_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            return self._get_repair_plan(connection, repair_plan_id, owner_id)
        finally:
            connection.close()

    def _get_repair_plan(
        self, connection: sqlite3.Connection, repair_plan_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT repair_plans.repair_plan_id,repair_plans.plan_digest,repair_plans.owner_id,resource_uri,repair_plans.original_job_id,workflow_id,server_id,pinned_plan_id,pinned_revision_id,pinned_deployment_id,pinned_content_digest,original_arguments_json,original_arguments_digest,normalized_changes_json,resulting_arguments_json,resulting_arguments_digest,diff_json,status,created_at,expires_at,commits.retry_job_id,commits.result_job_uri,commits.committed_at FROM repair_plans LEFT JOIN repair_plan_commits AS commits ON commits.repair_plan_id=repair_plans.repair_plan_id AND commits.owner_id=repair_plans.owner_id WHERE repair_plans.repair_plan_id=? AND repair_plans.owner_id=?",
            (repair_plan_id, owner_id),
        ).fetchone()
        if row is None:
            return None
        result: dict[str, Any] = {
            "repair_plan_id": str(row[0]),
            "plan_digest": str(row[1]),
            "owner_id": str(row[2]),
            "resource_uri": str(row[3]),
            "original_job_id": str(row[4]),
            "workflow_id": str(row[5]),
            "server_id": str(row[6]),
            "pinned_plan_id": str(row[7]),
            "pinned_revision_id": str(row[8]),
            "pinned_deployment_id": str(row[9]),
            "pinned_content_digest": str(row[10]),
            "original_arguments_snapshot": json.loads(str(row[11])),
            "original_arguments_digest": str(row[12]),
            "normalized_changes": json.loads(str(row[13])),
            "resulting_arguments": json.loads(str(row[14])),
            "resulting_arguments_digest": str(row[15]),
            "diff": json.loads(str(row[16])),
            "status": "committed" if row[20] is not None else str(row[17]),
            "created_at": str(row[18]),
            "expires_at": str(row[19]),
        }
        if row[20] is not None:
            result.update(
                {
                    "result_job_id": str(row[20]),
                    "result_job_uri": str(row[21]),
                    "retry_of": str(result["original_job_id"]),
                    "committed_at": str(row[22]),
                }
            )
        return result

    def reserve_repair_plan_commit(
        self,
        repair_plan_id: str,
        plan_digest: str,
        owner_id: str,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        reserved_at = _time(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            plan = connection.execute(
                "SELECT plan_digest,expires_at FROM repair_plans WHERE repair_plan_id=? AND owner_id=?",
                (repair_plan_id, owner_id),
            ).fetchone()
            if plan is None:
                raise LookupError("Repair plan was not found")
            if str(plan[0]) != plan_digest:
                raise ValueError("Repair plan digest conflicts")
            existing = connection.execute(
                "SELECT plan_digest FROM repair_plan_commit_intents WHERE repair_plan_id=? AND owner_id=?",
                (repair_plan_id, owner_id),
            ).fetchone()
            if existing is None:
                if _parse_time(str(plan[1])) <= now.astimezone(timezone.utc):
                    raise ValueError("Repair plan is expired")
                connection.execute(
                    "INSERT INTO repair_plan_commit_intents(repair_plan_id,owner_id,plan_digest,reserved_at) VALUES(?,?,?,?)",
                    (repair_plan_id, owner_id, plan_digest, reserved_at),
                )
            elif str(existing[0]) != plan_digest:
                raise ValueError("Repair plan commit intent conflicts")
            connection.commit()
            return self._get_repair_plan(connection, repair_plan_id, owner_id)  # type: ignore[return-value]
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_repair_plan_committed(
        self,
        repair_plan_id: str,
        plan_digest: str,
        owner_id: str,
        retry_job_id: str,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        committed_at = _time(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            plan = connection.execute(
                "SELECT repair_plan_id,plan_digest,owner_id,original_job_id,workflow_id,server_id,pinned_revision_id,pinned_deployment_id,resulting_arguments_json,expires_at FROM repair_plans WHERE repair_plan_id=? AND owner_id=?",
                (repair_plan_id, owner_id),
            ).fetchone()
            if plan is None:
                raise LookupError("Repair plan was not found")
            if str(plan[1]) != plan_digest:
                raise ValueError("Repair plan digest conflicts")
            existing = connection.execute(
                "SELECT retry_job_id FROM repair_plan_commits WHERE repair_plan_id=? AND owner_id=?",
                (repair_plan_id, owner_id),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != retry_job_id:
                    raise ValueError("Repair plan already committed to another Job")
                connection.commit()
                return self._get_repair_plan(connection, repair_plan_id, owner_id)  # type: ignore[return-value]
            intent = connection.execute(
                "SELECT reserved_at FROM repair_plan_commit_intents WHERE repair_plan_id=? AND owner_id=? AND plan_digest=?",
                (repair_plan_id, owner_id, plan_digest),
            ).fetchone()
            if intent is None or _parse_time(str(intent[0])) >= _parse_time(str(plan[9])):
                raise ValueError("Repair plan commit intent is missing or expired")
            retry = connection.execute(
                "SELECT job_id,workflow_id,server_id,plan_id,revision_id,deployment_id,retry_of FROM jobs WHERE job_id=? AND owner_id=?",
                (retry_job_id, owner_id),
            ).fetchone()
            if retry is None:
                raise ValueError("Retry Job was not found for this owner")
            if tuple(str(retry[index]) for index in (1, 2, 4, 5)) != tuple(
                str(plan[index]) for index in (4, 5, 6, 7)
            ):
                raise ValueError("Retry Job pin binding conflicts with repair plan")
            result_plan = connection.execute(
                "SELECT resolved_inputs_json FROM execution_plans WHERE plan_id=?", (retry[3],)
            ).fetchone()
            if result_plan is None or _arguments_from_snapshot(str(result_plan[0])) != json.loads(
                str(plan[8])
            ):
                raise ValueError("Retry Job arguments do not match repair plan")
            connection.execute(
                "INSERT INTO repair_plan_commits(repair_plan_id,owner_id,plan_digest,original_job_id,retry_job_id,result_job_uri,committed_at) VALUES(?,?,?,?,?,?,?)",
                (
                    repair_plan_id,
                    owner_id,
                    plan_digest,
                    str(plan[3]),
                    retry_job_id,
                    f"comfyui://jobs/{retry_job_id}",
                    committed_at,
                ),
            )
            connection.execute(
                "UPDATE jobs SET retry_of=? WHERE job_id=? AND owner_id=? AND retry_of IS NULL",
                (str(plan[3]), retry_job_id, owner_id),
            )
            if connection.execute(
                "SELECT retry_of FROM jobs WHERE job_id=?", (retry_job_id,)
            ).fetchone()[0] != str(plan[3]):
                raise ValueError("Retry Job lineage was not persisted")
            _event(
                connection,
                "JOB_RETRY_COMMITTED",
                f"comfyui://jobs/{retry_job_id}",
                repair_plan_id,
                owner_id,
                committed_at,
                {"retry_of": str(plan[3]), "repair_plan_id": repair_plan_id},
            )
            connection.commit()
            return self._get_repair_plan(connection, repair_plan_id, owner_id)  # type: ignore[return-value]
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def cleanup_expired_repair_plans(
        self, *, now: datetime, owner_id: str | None = None, limit: int = 1000
    ) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("repair plan retention limit must be between 1 and 1000")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            params: list[object] = [_time(now)]
            owner_sql = ""
            if owner_id is not None:
                owner_sql = " AND owner_id=?"
                params.append(owner_id)
            params.append(limit)
            rows = connection.execute(
                f"SELECT repair_plan_id FROM repair_plans WHERE expires_at<=?{owner_sql} AND NOT EXISTS(SELECT 1 FROM repair_plan_commits WHERE repair_plan_id=repair_plans.repair_plan_id) AND NOT EXISTS(SELECT 1 FROM repair_plan_commit_intents WHERE repair_plan_id=repair_plans.repair_plan_id) ORDER BY expires_at,repair_plan_id LIMIT ?",
                params,
            ).fetchall()
            for row in rows:
                connection.execute("DELETE FROM repair_plans WHERE repair_plan_id=?", (row[0],))
            connection.commit()
            return len(rows)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        return self._store._connect()


def _validate_report(report: dict[str, Any]) -> dict[str, Any]:
    required = (
        "diagnostic_id",
        "owner_id",
        "registry_version",
        "subject_uri",
        "classification",
        "rule_id",
        "retryable",
        "evidence",
        "safe_actions",
        "approval_actions",
        "created_at",
        "resource_uri",
    )
    if not isinstance(report, dict) or any(key not in report for key in required):
        raise ValueError("diagnostic report shape is invalid")
    value = copy.deepcopy(report)
    if (
        not isinstance(value["owner_id"], str)
        or not value["owner_id"]
        or not isinstance(value["retryable"], bool)
    ):
        raise ValueError("diagnostic report owner or retryable is invalid")
    if (
        not isinstance(value["evidence"], dict)
        or not isinstance(value["safe_actions"], list)
        or not isinstance(value["approval_actions"], list)
    ):
        raise ValueError("diagnostic report evidence or actions are invalid")
    if len(value["safe_actions"]) > 32 or len(value["approval_actions"]) > 32:
        raise ValueError("diagnostic action cardinality exceeds bound")
    for action, risk in [(item, "safe") for item in value["safe_actions"]] + [
        (item, "approval_required") for item in value["approval_actions"]
    ]:
        if (
            not isinstance(action, dict)
            or any(key not in action for key in ("tool", "name", "required_arguments", "risk"))
            or action["risk"] != risk
            or not isinstance(action["tool"], str)
            or not action["tool"].startswith("comfyui.")
            or not isinstance(action["required_arguments"], dict)
        ):
            raise ValueError("diagnostic action shape is invalid")
        if (
            any(key.lower() in {"command", "shell", "cli", "argv"} for key in action)
            or "comfyui-skill" in _json(action).lower()
        ):
            raise ValueError("diagnostic actions cannot contain CLI commands")
    if (
        len(_json(value["evidence"]).encode()) > 65536
        or len(_json(value["safe_actions"]).encode()) > 32768
        or len(_json(value["approval_actions"]).encode()) > 32768
    ):
        raise ValueError("diagnostic payload exceeds bound")
    if not isinstance(value["subject_uri"], str) or not (
        value["subject_uri"].startswith("comfyui://jobs/")
        or value["subject_uri"].startswith("comfyui://servers/")
    ):
        raise ValueError("diagnostic subject URI is invalid")
    return value


def _validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    required = (
        "repair_plan_id",
        "plan_digest",
        "owner_id",
        "resource_uri",
        "original_job_id",
        "workflow_id",
        "server_id",
        "pinned_plan_id",
        "pinned_revision_id",
        "pinned_deployment_id",
        "pinned_content_digest",
        "original_arguments_snapshot",
        "original_arguments_digest",
        "normalized_changes",
        "resulting_arguments",
        "resulting_arguments_digest",
        "diff",
        "created_at",
        "expires_at",
    )
    if not isinstance(plan, dict) or any(key not in plan for key in required):
        raise ValueError("repair plan shape is invalid")
    value = copy.deepcopy(plan)
    if any(
        not isinstance(value[field], dict)
        for field in ("original_arguments_snapshot", "normalized_changes", "resulting_arguments")
    ) or not isinstance(value["diff"], list):
        raise ValueError("repair plan JSON shapes are invalid")
    original, changes = (
        value["original_arguments_snapshot"],
        {str(key): plan["normalized_changes"][key] for key in sorted(plan["normalized_changes"])},
    )
    resulting = copy.deepcopy(original)
    diff: list[dict[str, Any]] = []
    for key, after in changes.items():
        before = copy.deepcopy(original[key]) if key in original else None
        operation = (
            "unchanged"
            if key in original and _json(original[key]) == _json(after)
            else "replace"
            if key in original
            else "add"
        )
        resulting[key] = copy.deepcopy(after)
        diff.append(
            {
                "path": "/arguments/" + key.replace("~", "~0").replace("/", "~1"),
                "operation": operation,
                "before": before,
                "after": copy.deepcopy(after),
            }
        )
    if (
        _json(resulting) != _json(value["resulting_arguments"])
        or _json(diff) != _json(value["diff"])
        or _json(changes) != _json(value["normalized_changes"])
    ):
        raise ValueError("repair plan diff does not exactly describe resulting snapshot")
    if value["original_arguments_digest"] != _digest(original) or value[
        "resulting_arguments_digest"
    ] != _digest(resulting):
        raise ValueError("repair plan snapshot digest conflicts")
    immutable = {
        key: value[key]
        for key in (
            "owner_id",
            "original_job_id",
            "workflow_id",
            "server_id",
            "pinned_plan_id",
            "pinned_revision_id",
            "pinned_deployment_id",
            "pinned_content_digest",
            "original_arguments_snapshot",
            "original_arguments_digest",
            "normalized_changes",
            "resulting_arguments",
            "resulting_arguments_digest",
            "diff",
            "created_at",
            "expires_at",
        )
    }
    try:
        created_at = _parse_time(str(value["created_at"]))
        expires_at = _parse_time(str(value["expires_at"]))
    except ValueError as exc:
        raise ValueError("repair plan lifetime is invalid") from exc
    if expires_at - created_at != timedelta(hours=1):
        raise ValueError("repair plan lifetime must be exactly one hour")
    if value["plan_digest"] != _digest(immutable):
        raise ValueError("repair plan digest conflicts")
    if value.get("status", "planned") != "planned":
        raise ValueError("new repair plans must be planned")
    if len(_json(original).encode()) > 1_048_576 or len(_json(resulting).encode()) > 1_048_576:
        raise ValueError("repair plan snapshot exceeds bound")
    return value


def _report_subject(value: dict[str, Any]) -> tuple[str, str | None, str | None]:
    uri = str(value["subject_uri"])
    if uri.startswith("comfyui://jobs/"):
        return "job", uri.rsplit("/", 1)[-1], None
    return "server", None, uri.rsplit("/", 1)[-1]


def _event(
    connection: sqlite3.Connection,
    event_type: str,
    subject_uri: str,
    correlation: str,
    owner: str,
    occurred_at: str,
    data: dict[str, Any],
) -> None:
    sequence = int(
        connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM domain_events WHERE subject_uri=?",
            (subject_uri,),
        ).fetchone()[0]
    )
    event_id = _stable_id("event", correlation, str(sequence), event_type)
    connection.execute(
        "INSERT INTO domain_events(event_id,event_type,subject_uri,sequence,occurred_at,principal_id,correlation_id,data_json) VALUES(?,?,?,?,?,?,?,?)",
        (event_id, event_type, subject_uri, sequence, occurred_at, owner, correlation, _json(data)),
    )
    connection.execute(
        "INSERT INTO outbox(outbox_id,event_id,topic,payload_json,status,created_at) VALUES(?,?, 'resources.updated',?,'pending',?)",
        (
            _stable_id("outbox", event_id),
            event_id,
            _json({"uri": subject_uri, "sequence": sequence, "owner_id": owner}),
            occurred_at,
        ),
    )


def _plan_row(value: dict[str, Any]) -> tuple[object, ...]:
    return (
        value["repair_plan_id"],
        value["plan_digest"],
        value["owner_id"],
        value["resource_uri"],
        value["original_job_id"],
        value["workflow_id"],
        value["server_id"],
        value["pinned_plan_id"],
        value["pinned_revision_id"],
        value["pinned_deployment_id"],
        value["pinned_content_digest"],
        _json(value["original_arguments_snapshot"]),
        value["original_arguments_digest"],
        _json(value["normalized_changes"]),
        _json(value["resulting_arguments"]),
        value["resulting_arguments_digest"],
        _json(value["diff"]),
        "planned",
        value["created_at"],
        value["expires_at"],
    )


def _stable_id(kind: str, *parts: str) -> str:
    return (
        f"{kind}_"
        + hashlib.sha256("\0".join(("phase-n-" + kind + "-v1", *parts)).encode()).hexdigest()
    )


def _arguments_from_snapshot(snapshot: str) -> dict[str, Any]:
    raw = json.loads(snapshot)
    if not isinstance(raw, dict) or not isinstance(raw.get("arguments"), dict):
        raise ValueError("execution plan arguments snapshot is invalid")
    return raw["arguments"]
