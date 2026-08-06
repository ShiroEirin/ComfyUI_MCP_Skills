"""Explicitly isolated workflow administration service."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock

from comfyui_mcp_skills.application.ports import WorkflowRepository
from comfyui_mcp_skills.domain.errors import (
    ComfyUISkillsError,
    IdempotencyConflict,
    WorkflowNotFound,
)
from comfyui_mcp_skills.domain.identifiers import validate_identifier
from comfyui_mcp_skills.infrastructure.persistence.migration_lock import (
    project_migration_lock,
)

MAX_ADMIN_REQUEST_ID_LENGTH = 128


class AdminAuditError(ComfyUISkillsError):
    """Raised when a dangerous action cannot be durably audited."""

    code = "ADMIN_AUDIT_UNAVAILABLE"


def _valid_utc_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


class JsonlAuditLog:
    """Append bounded administrative events as durable JSON lines."""

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()

    def append(self, event: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(f"{self._path}.lock", timeout=10):
            with self._path.open("a", encoding="utf-8") as file:
                json.dump(
                    event,
                    file,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())

    def events_for(self, request_id: str) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        events: list[dict[str, Any]] = []
        with FileLock(f"{self._path}.lock", timeout=10):
            with self._path.open("r", encoding="utf-8") as file:
                for line in file:
                    event = json.loads(line)
                    if event.get("request_id") == request_id:
                        events.append(event)
        return events

    def export_events(
        self,
        *,
        actor: str = "",
        action: str = "",
        outcomes: frozenset[str] = frozenset(),
        after: str = "",
        limit: int = 100,
        cursor: int = 0,
    ) -> tuple[list[dict[str, Any]], str]:
        """Read a bounded, filtered slice of the append-only audit trail.

        ``cursor`` is the line index of the last returned event (1-based);
        pass it back to continue from the following line. Events are returned
        in append order. ``after`` is an ISO-8601 instant with a timezone; it is
        parsed once, normalized to UTC, and compared as an instant (inclusive),
        so offsets never skew the filter. A corrupt line or timestamp raises
        instead of being silently skipped because the audit trail must stay
        trustworthy. The trail has no rotation; each page rescans from line 0
        (O(n) per page).
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ValueError("cursor must be a non-negative integer")
        after_instant = None
        if after:
            try:
                parsed_after = datetime.fromisoformat(after.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(
                    "after must be an ISO-8601 timestamp with a timezone"
                ) from exc
            if parsed_after.tzinfo is None:
                raise ValueError(
                    "after must be an ISO-8601 timestamp with a timezone"
                )
            after_instant = parsed_after.astimezone(timezone.utc)
        if not self._path.exists():
            return [], ""
        events: list[dict[str, Any]] = []
        with FileLock(f"{self._path}.lock", timeout=10):
            with self._path.open("r", encoding="utf-8") as file:
                for line_index, line in enumerate(file, start=1):
                    if line_index <= cursor:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(
                            f"audit trail is corrupt at line {line_index}"
                        ) from exc
                    if not isinstance(event, dict):
                        raise ValueError(
                            f"audit trail is corrupt at line {line_index}: expected an object"
                        )
                    raw_timestamp = event.get("timestamp")
                    try:
                        event_instant = datetime.fromisoformat(
                            str(raw_timestamp).replace("Z", "+00:00")
                        )
                    except ValueError as exc:
                        raise ValueError(
                            f"audit trail is corrupt at line {line_index}: "
                            "invalid event timestamp"
                        ) from exc
                    if event_instant.tzinfo is None:
                        raise ValueError(
                            f"audit trail is corrupt at line {line_index}: "
                            "event timestamp has no timezone"
                        )
                    event_instant = event_instant.astimezone(timezone.utc)
                    if actor and event.get("actor") != actor:
                        continue
                    if action and event.get("action") != action:
                        continue
                    if outcomes and event.get("outcome") not in outcomes:
                        continue
                    if after_instant is not None and event_instant < after_instant:
                        continue
                    events.append(event)
                    if len(events) >= limit:
                        return events, str(line_index)
        return events, ""


class WorkflowAdmin:
    def __init__(
        self,
        base_dir: Path,
        repository: WorkflowRepository | None,
        *,
        actor: str,
        audit_log: JsonlAuditLog | None = None,
    ) -> None:
        if not actor or len(actor) > 128:
            raise ValueError("actor must be between 1 and 128 characters")
        self._base_dir = base_dir.resolve()
        self._repository = repository
        self._actor = actor
        self._audit_log = audit_log or JsonlAuditLog(self._base_dir / "data" / "admin-audit.jsonl")
        self._transactions_dir = self._base_dir / ".admin-transactions"
        self._transaction_cache: dict[str, dict[str, Any]] = {}
        self._migration_lock = project_migration_lock(self._base_dir)

    def set_enabled(
        self,
        server_id: str,
        workflow_id: str,
        enabled: bool,
        *,
        request_id: str = "",
    ) -> dict[str, object]:
        repository = self._repository
        if repository is None:
            raise ValueError("file workflow store is fenced after the workflow cutover")
        directory = self._safe_directory(server_id, workflow_id)
        target = {"server_id": server_id, "workflow_id": workflow_id}

        def change() -> dict[str, object]:
            with self._migration_lock, FileLock(f"{directory}.admin.lock", timeout=10):
                workflow = repository.get(server_id, workflow_id)
                if workflow is None:
                    raise WorkflowNotFound(f"Workflow not found: {server_id}/{workflow_id}")
                metadata_path = directory / "schema.json"
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata["enabled"] = enabled
                self._atomic_write(metadata_path, metadata)
            return {
                "server_id": server_id,
                "workflow_id": workflow_id,
                "enabled": enabled,
            }

        return self._execute(
            request_id=request_id,
            action="workflow.set_enabled",
            target=target,
            operation_parameters={"enabled": enabled},
            change=change,
        )

    def delete(
        self,
        server_id: str,
        workflow_id: str,
        confirmation: str,
        *,
        request_id: str = "",
    ) -> dict[str, object]:
        repository = self._repository
        if repository is None:
            raise ValueError("file workflow store is fenced after the workflow cutover")
        directory = self._safe_directory(server_id, workflow_id)
        target = {"server_id": server_id, "workflow_id": workflow_id}

        def change() -> dict[str, object]:
            expected = f"delete:{server_id}/{workflow_id}"
            if confirmation != expected:
                raise ValueError(f"confirmation must equal {expected}")
            with self._migration_lock, FileLock(f"{directory}.admin.lock", timeout=10):
                if repository.get(server_id, workflow_id) is None:
                    raise WorkflowNotFound(f"Workflow not found: {server_id}/{workflow_id}")
                shutil.rmtree(directory)
            return {
                "server_id": server_id,
                "workflow_id": workflow_id,
                "deleted": True,
            }

        return self._execute(
            request_id=request_id,
            action="workflow.delete",
            target=target,
            operation_parameters={},
            change=change,
        )

    def get_audit_status(self, request_id: str) -> dict[str, object]:
        request_id = self._normalize_request_id(request_id, generate=False)
        with FileLock(str(self._transaction_lock_path(request_id)), timeout=10):
            transaction = self._load_transaction(request_id)
            if transaction is not None:
                self._synchronize_audit_status(transaction)
                return self._status_result(transaction)
            events = self._events_for(request_id)
        if not events:
            raise ValueError(f"Unknown administrative request_id: {request_id}")
        terminal = next(
            (event for event in reversed(events) if event.get("outcome") != "intent"),
            None,
        )
        reference = terminal or events[0]
        outcome = reference.get("outcome")
        return {
            "request_id": request_id,
            "action": reference["action"],
            "target": reference["target"],
            "committed": (
                True if outcome == "success" else False if outcome == "failure" else None
            ),
            "audit_status": "audited" if terminal is not None else "pending",
        }

    def retry_audit(self, request_id: str) -> dict[str, object]:
        request_id = self._normalize_request_id(request_id, generate=False)
        with FileLock(str(self._transaction_lock_path(request_id)), timeout=10):
            transaction = self._load_transaction(request_id)
            if transaction is not None:
                self._synchronize_audit_status(transaction, append_pending=True)
                return self._status_result(transaction)
        return self.get_audit_status(request_id)

    def export_audit(
        self,
        *,
        actor: str = "",
        action: str = "",
        outcomes: list[str] | None = None,
        after: str = "",
        limit: int = 100,
        cursor: str = "",
    ) -> dict[str, object]:
        """Export a bounded, filterable slice of the durable admin audit trail."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        outcome_set = frozenset(outcomes or [])
        for outcome in outcome_set:
            if outcome not in {"intent", "success", "failure"}:
                raise ValueError(f"outcome {outcome} is not recognized")
        if after and not _valid_utc_timestamp(after):
            raise ValueError("after must be an ISO-8601 timestamp with a timezone")
        start = 0
        if cursor:
            try:
                start = int(cursor)
            except (TypeError, ValueError) as exc:
                raise ValueError("cursor is invalid") from exc
            if start < 0:
                raise ValueError("cursor must be non-negative")
        try:
            events, next_cursor = self._audit_log.export_events(
                actor=actor,
                action=action,
                outcomes=outcome_set,
                after=after,
                limit=limit,
                cursor=start,
            )
        except Exception as exc:
            raise AdminAuditError("Administrative audit records could not be exported") from exc
        return {
            "events": events,
            "count": len(events),
            "limit": limit,
            "next_cursor": next_cursor,
            "filters": {
                "actor": actor,
                "action": action,
                "outcomes": sorted(outcome_set),
                "after": after,
            },
        }

    def _execute(
        self,
        *,
        request_id: str,
        action: str,
        target: dict[str, str],
        operation_parameters: dict[str, object],
        change: Callable[[], dict[str, object]],
    ) -> dict[str, object]:
        request_id = self._normalize_request_id(request_id, generate=True)
        operation_key = self._operation_key(action, target, operation_parameters)
        with FileLock(str(self._transaction_lock_path(request_id)), timeout=10):
            transaction = self._load_transaction(request_id)
            if transaction is not None:
                self._validate_transaction(
                    transaction,
                    action=action,
                    target=target,
                    operation_key=operation_key,
                )
                if transaction["committed"] is not True:
                    return self._status_result(transaction)
                self._synchronize_audit_status(transaction, append_pending=True)
                return self._transaction_result(transaction)

            recovered = self._recover_transaction_from_audit(
                request_id=request_id,
                action=action,
                target=target,
                operation_key=operation_key,
                operation_parameters=operation_parameters,
            )
            if recovered is not None:
                if recovered["committed"] is True:
                    return self._transaction_result(recovered)
                return self._status_result(recovered)
            self._record(request_id, action, target, "intent", operation_key=operation_key)
            transaction = {
                "request_id": request_id,
                "action": action,
                "target": target,
                "operation_key": operation_key,
                "committed": None,
                "audit_status": "pending",
            }
            self._store_transaction(transaction)
            self._transaction_cache[request_id] = transaction
            try:
                result = change()
            except Exception as exc:
                error_code = self._error_code(exc)
                try:
                    self._record(
                        request_id,
                        action,
                        target,
                        "failure",
                        error_code=error_code,
                        operation_key=operation_key,
                    )
                except AdminAuditError:
                    raise
                transaction["committed"] = False
                transaction["audit_status"] = "audited"
                transaction["error_code"] = error_code
                self._store_transaction_safely(transaction)
                raise

            result.update({"request_id": request_id, "committed": True})
            outcome_event = self._event(
                request_id,
                action,
                target,
                "success",
                operation_key=operation_key,
            )
            transaction.update(
                {
                    "committed": True,
                    "result": result,
                    "outcome_event": outcome_event,
                }
            )
            self._transaction_cache[request_id] = transaction
            self._store_transaction_safely(transaction)
            try:
                self._append_event(outcome_event)
            except AdminAuditError:
                pass
            else:
                transaction["audit_status"] = "audited"
                self._store_transaction_safely(transaction)
            return self._transaction_result(transaction)

    def _synchronize_audit_status(
        self,
        transaction: dict[str, Any],
        *,
        append_pending: bool = False,
    ) -> None:
        request_id = str(transaction["request_id"])
        terminal = next(
            (
                event
                for event in reversed(self._events_for(request_id))
                if event.get("outcome") in {"success", "failure"}
            ),
            None,
        )
        if terminal is not None:
            committed = terminal.get("outcome") == "success"
            transaction["committed"] = committed
            transaction["audit_status"] = "audited"
            if committed and not isinstance(transaction.get("result"), dict):
                transaction["result"] = {
                    **dict(transaction.get("target", {})),
                    "request_id": request_id,
                    "committed": True,
                }
            if not committed and terminal.get("error_code"):
                transaction["error_code"] = terminal["error_code"]
            self._store_transaction_safely(transaction)
            return
        if transaction.get("audit_status") == "audited":
            return
        if transaction.get("committed") is not True or not append_pending:
            return
        try:
            self._append_event(dict(transaction["outcome_event"]))
        except AdminAuditError:
            return
        transaction["audit_status"] = "audited"
        self._store_transaction_safely(transaction)

    def _record(
        self,
        request_id: str,
        action: str,
        target: dict[str, str],
        outcome: str,
        *,
        operation_key: str,
        error_code: str | None = None,
    ) -> None:
        self._append_event(
            self._event(
                request_id,
                action,
                target,
                outcome,
                operation_key=operation_key,
                error_code=error_code,
            )
        )

    def _append_event(self, event: dict[str, Any]) -> None:
        try:
            self._audit_log.append(event)
        except Exception as exc:
            raise AdminAuditError("Administrative audit record could not be written") from exc

    def _events_for(self, request_id: str) -> list[dict[str, Any]]:
        try:
            return self._audit_log.events_for(request_id)
        except Exception as exc:
            raise AdminAuditError("Administrative audit records could not be queried") from exc

    def _recover_transaction_from_audit(
        self,
        *,
        request_id: str,
        action: str,
        target: dict[str, str],
        operation_key: str,
        operation_parameters: dict[str, object],
    ) -> dict[str, Any] | None:
        events = self._events_for(request_id)
        if not events:
            return None
        if any(
            event.get("action") != action
            or event.get("target") != target
            or event.get("operation_key") != operation_key
            for event in events
        ):
            raise IdempotencyConflict(
                "request_id was already used for a different administrative operation"
            )
        terminal = next(
            (event for event in reversed(events) if event.get("outcome") in {"success", "failure"}),
            None,
        )
        committed = None if terminal is None else terminal.get("outcome") == "success"
        transaction: dict[str, Any] = {
            "request_id": request_id,
            "action": action,
            "target": target,
            "operation_key": operation_key,
            "committed": committed,
            "audit_status": "audited" if terminal is not None else "pending",
        }
        if committed:
            result: dict[str, object] = {
                **target,
                "request_id": request_id,
                "committed": True,
            }
            if action == "workflow.set_enabled":
                result["enabled"] = operation_parameters["enabled"]
            elif action == "workflow.delete":
                result["deleted"] = True
            transaction["result"] = result
        elif terminal is not None and terminal.get("error_code"):
            transaction["error_code"] = terminal["error_code"]
        self._transaction_cache[request_id] = transaction
        self._store_transaction_safely(transaction)
        return transaction

    def _load_transaction(self, request_id: str) -> dict[str, Any] | None:
        cached = self._transaction_cache.get(request_id)
        if cached is not None:
            return cached
        path = self._transaction_path(request_id)
        try:
            transaction = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, TypeError, ValueError) as exc:
            raise AdminAuditError("Administrative transaction state could not be read") from exc
        if transaction.get("request_id") != request_id:
            raise AdminAuditError("Administrative transaction state is inconsistent")
        self._transaction_cache[request_id] = transaction
        return transaction

    def _store_transaction(self, transaction: dict[str, Any]) -> None:
        try:
            path = self._transaction_path(str(transaction["request_id"]))
            path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(path, transaction)
        except OSError as exc:
            raise AdminAuditError("Administrative transaction state could not be written") from exc

    def _store_transaction_safely(self, transaction: dict[str, Any]) -> None:
        try:
            path = self._transaction_path(str(transaction["request_id"]))
            path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(path, transaction)
        except OSError:
            return

    def _transaction_path(self, request_id: str) -> Path:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return self._transactions_dir / f"{digest}.json"

    def _transaction_lock_path(self, request_id: str) -> Path:
        path = self._transaction_path(request_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.with_suffix(".lock")

    @staticmethod
    def _normalize_request_id(request_id: str, *, generate: bool) -> str:
        if not request_id and generate:
            return uuid.uuid4().hex
        if (
            not isinstance(request_id, str)
            or not request_id
            or len(request_id) > MAX_ADMIN_REQUEST_ID_LENGTH
        ):
            raise ValueError(
                f"request_id must be between 1 and {MAX_ADMIN_REQUEST_ID_LENGTH} characters"
            )
        return request_id

    @staticmethod
    def _operation_key(
        action: str,
        target: dict[str, str],
        parameters: dict[str, object],
    ) -> str:
        encoded = json.dumps(
            {"action": action, "target": target, "parameters": parameters},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _validate_transaction(
        transaction: dict[str, Any],
        *,
        action: str,
        target: dict[str, str],
        operation_key: str,
    ) -> None:
        if (
            transaction.get("action") != action
            or transaction.get("target") != target
            or transaction.get("operation_key") != operation_key
        ):
            raise IdempotencyConflict(
                "request_id was already used for a different administrative operation"
            )

    @staticmethod
    def _transaction_result(transaction: dict[str, Any]) -> dict[str, object]:
        result = dict(transaction.get("result") or transaction.get("target") or {})
        result["request_id"] = transaction["request_id"]
        result["committed"] = transaction["committed"]
        result["audit_status"] = transaction["audit_status"]
        return result

    @staticmethod
    def _status_result(transaction: dict[str, Any]) -> dict[str, object]:
        return {
            "request_id": transaction["request_id"],
            "action": transaction["action"],
            "target": transaction["target"],
            "committed": transaction["committed"],
            "audit_status": transaction["audit_status"],
        }

    def _event(
        self,
        request_id: str,
        action: str,
        target: dict[str, str],
        outcome: str,
        *,
        operation_key: str,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id,
            "actor": self._actor,
            "action": action,
            "target": target,
            "operation_key": operation_key,
            "outcome": outcome,
            "error_code": error_code,
        }

    @staticmethod
    def _error_code(exc: Exception) -> str:
        if isinstance(exc, ComfyUISkillsError):
            return exc.code
        if isinstance(exc, (KeyError, TypeError, ValueError)):
            return "INVALID_ARGUMENTS"
        return "INTERNAL_ERROR"

    def _safe_directory(self, server_id: str, workflow_id: str) -> Path:
        server_id = validate_identifier(server_id, field="server_id")
        workflow_id = validate_identifier(workflow_id, field="workflow_id")
        directory = (self._base_dir / "data" / server_id / workflow_id).resolve()
        root = (self._base_dir / "data").resolve()
        try:
            directory.relative_to(root)
        except ValueError as exc:
            raise ValueError("Unsafe workflow path") from exc
        return directory

    @staticmethod
    def _atomic_write(path: Path, value: dict[str, object]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as file:
                json.dump(value, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
