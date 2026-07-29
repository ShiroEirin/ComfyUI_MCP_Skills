"""Explicitly isolated workflow administration service."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock

from comfyui_mcp_skills.application.ports import WorkflowRepository
from comfyui_mcp_skills.domain.errors import ComfyUISkillsError, WorkflowNotFound
from comfyui_mcp_skills.domain.identifiers import validate_identifier


class AdminAuditError(ComfyUISkillsError):
    """Raised when a dangerous action cannot be durably audited."""

    code = "ADMIN_AUDIT_UNAVAILABLE"


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


class WorkflowAdmin:
    def __init__(
        self,
        base_dir: Path,
        repository: WorkflowRepository,
        *,
        actor: str,
        audit_log: JsonlAuditLog | None = None,
    ) -> None:
        if not actor or len(actor) > 128:
            raise ValueError("actor must be between 1 and 128 characters")
        self._base_dir = base_dir.resolve()
        self._repository = repository
        self._actor = actor
        self._audit_log = audit_log or JsonlAuditLog(
            self._base_dir / "data" / "admin-audit.jsonl"
        )

    def set_enabled(
        self,
        server_id: str,
        workflow_id: str,
        enabled: bool,
        *,
        request_id: str = "",
    ) -> dict[str, object]:
        directory = self._safe_directory(server_id, workflow_id)
        request_id = request_id or uuid.uuid4().hex
        target = {"server_id": server_id, "workflow_id": workflow_id}
        self._record(request_id, "workflow.set_enabled", target, "intent")
        try:
            with FileLock(f"{directory}.admin.lock", timeout=10):
                workflow = self._repository.get(server_id, workflow_id)
                if workflow is None:
                    raise WorkflowNotFound(
                        f"Workflow not found: {server_id}/{workflow_id}"
                    )
                metadata_path = directory / "schema.json"
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata["enabled"] = enabled
                self._atomic_write(metadata_path, metadata)
        except Exception as exc:
            self._record(
                request_id,
                "workflow.set_enabled",
                target,
                "failure",
                error_code=self._error_code(exc),
            )
            raise
        self._record(request_id, "workflow.set_enabled", target, "success")
        return {
            "server_id": server_id,
            "workflow_id": workflow_id,
            "enabled": enabled,
        }

    def delete(
        self,
        server_id: str,
        workflow_id: str,
        confirmation: str,
        *,
        request_id: str = "",
    ) -> dict[str, object]:
        directory = self._safe_directory(server_id, workflow_id)
        request_id = request_id or uuid.uuid4().hex
        target = {"server_id": server_id, "workflow_id": workflow_id}
        self._record(request_id, "workflow.delete", target, "intent")
        try:
            expected = f"delete:{server_id}/{workflow_id}"
            if confirmation != expected:
                raise ValueError(f"confirmation must equal {expected}")
            with FileLock(f"{directory}.admin.lock", timeout=10):
                if self._repository.get(server_id, workflow_id) is None:
                    raise WorkflowNotFound(
                        f"Workflow not found: {server_id}/{workflow_id}"
                    )
                shutil.rmtree(directory)
        except Exception as exc:
            self._record(
                request_id,
                "workflow.delete",
                target,
                "failure",
                error_code=self._error_code(exc),
            )
            raise
        self._record(request_id, "workflow.delete", target, "success")
        return {
            "server_id": server_id,
            "workflow_id": workflow_id,
            "deleted": True,
        }

    def _record(
        self,
        request_id: str,
        action: str,
        target: dict[str, str],
        outcome: str,
        *,
        error_code: str | None = None,
    ) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id,
            "actor": self._actor,
            "action": action,
            "target": target,
            "outcome": outcome,
            "error_code": error_code,
        }
        try:
            self._audit_log.append(event)
        except Exception as exc:
            raise AdminAuditError(
                "Administrative action refused because audit logging is unavailable"
            ) from exc

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
