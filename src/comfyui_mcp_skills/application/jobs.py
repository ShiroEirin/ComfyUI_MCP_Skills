"""Query, wait for, and safely cancel durable ComfyUI jobs."""

from __future__ import annotations

import builtins
import logging
import mimetypes
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from comfyui_mcp_skills.application.assets import MediaType, classify_media_type
from comfyui_mcp_skills.application.pagination import (
    decode_keyset_cursor,
    encode_keyset_cursor,
)
from comfyui_mcp_skills.application.ports import ArtifactRepository, ComfyUIGateway, RunRepository
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.control_plane import (
    canonical_resource_uri,
    derive_legacy_artifact_id,
    derive_legacy_job_id,
    validate_control_plane_id,
)
from comfyui_mcp_skills.domain.errors import (
    JobNotFound,
    ServerOffline,
    UnsafeCancel,
    WorkflowArgumentsError,
)
from comfyui_mcp_skills.domain.identifiers import validate_identifier
from comfyui_mcp_skills.domain.media import validate_media_locator
from comfyui_mcp_skills.domain.models import Job

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"completed", "error", "interrupted", "cancelled", "lost"}
_OUTPUT_MEDIA: tuple[tuple[str, MediaType], ...] = (
    ("images", "image"),
    ("gifs", "video"),
    ("audio", "audio"),
    ("video", "video"),
)
_SAFE_ERROR_FIELD = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_MAX_ERROR_MESSAGES = 8
_MAX_ERROR_LENGTH = 2048
_JOB_LIST_STATUSES = frozenset(
    {
        "reserved",
        "submission_unknown",
        "submitted",
        "queued",
        "running",
        "completed",
        "error",
        "interrupted",
        "cancelled",
        "lost",
    }
)
_MAX_JOB_LIST_LIMIT = 100


class JobService:
    def __init__(
        self,
        servers: ServerRegistry,
        runs: RunRepository,
        gateway_factory: Callable[[dict[str, Any]], ComfyUIGateway],
        artifacts: ArtifactRepository | None = None,
    ) -> None:
        self._servers = servers
        self._runs = runs
        self._gateway_factory = gateway_factory
        self._artifacts = artifacts

    def list(
        self,
        *,
        owner_id: str,
        limit: int = 50,
        status: str = "",
        workflow_id: str = "",
        server_id: str = "",
        created_after: str = "",
        cursor: str = "",
    ) -> dict[str, Any]:
        """List one owner's canonical jobs using filter-bound keyset pagination."""
        if not isinstance(owner_id, str) or not owner_id or "\x00" in owner_id:
            raise WorkflowArgumentsError("owner_id must be a non-empty string")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_JOB_LIST_LIMIT
        ):
            raise WorkflowArgumentsError(
                f"limit must be an integer between 1 and {_MAX_JOB_LIST_LIMIT}"
            )
        if not isinstance(status, str) or (status and status not in _JOB_LIST_STATUSES):
            raise WorkflowArgumentsError("status must be a supported job status")
        if not isinstance(cursor, str):
            raise WorkflowArgumentsError("cursor must be an opaque string")
        workflow_id = self._list_identifier(workflow_id, field="workflow_id")
        server_id = self._list_identifier(server_id, field="server_id")
        created_after = self._list_timestamp(created_after, field="created_after")
        filters = {
            "owner_id": owner_id,
            "status": status,
            "workflow_id": workflow_id,
            "server_id": server_id,
            "created_after": created_after,
        }
        after_created_at = ""
        after_job_id = ""
        if cursor:
            try:
                after_created_at, after_job_id = decode_keyset_cursor(cursor, filters=filters)
                after_created_at = self._list_timestamp(after_created_at, field="cursor created_at")
                validate_control_plane_id("job", after_job_id)
            except ValueError as exc:
                raise WorkflowArgumentsError(str(exc)) from exc
        rows = self._runs.list_jobs(
            owner_id,
            limit=limit + 1,
            status=status,
            workflow_id=workflow_id,
            server_id=server_id,
            created_after=created_after,
            after_created_at=after_created_at,
            after_job_id=after_job_id,
        )
        page_rows = rows[:limit]
        items = [
            {
                "job_uri": canonical_resource_uri("job", row["job_id"]),
                "job_id": row["job_id"],
                "workflow_id": row["workflow_id"],
                "revision_id": row["revision_id"],
                "deployment_id": row["deployment_id"],
                "server_id": row["server_id"],
                "status": row["status"],
                "created_at": row["created_at"],
            }
            for row in page_rows
        ]
        next_cursor = ""
        if len(rows) > limit:
            last = page_rows[-1]
            next_cursor = encode_keyset_cursor(last["created_at"], last["job_id"], filters=filters)
        return {"items": items, "next_cursor": next_cursor}

    @staticmethod
    def _list_identifier(value: object, *, field: str) -> str:
        if value == "":
            return ""
        try:
            return validate_identifier(value, field=field)
        except ValueError as exc:
            raise WorkflowArgumentsError(str(exc)) from exc

    @staticmethod
    def _list_timestamp(value: object, *, field: str) -> str:
        if value == "":
            return ""
        if not isinstance(value, str):
            raise WorkflowArgumentsError(f"{field} must be an ISO-8601 timestamp")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise WorkflowArgumentsError(f"{field} must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise WorkflowArgumentsError(f"{field} must include a timezone")
        return parsed.astimezone(timezone.utc).isoformat()

    def get(self, server_id: str, prompt_id: str, *, owner_id: str = "") -> Job:
        return self._get_until(server_id, prompt_id, owner_id=owner_id)

    def _get_until(
        self,
        server_id: str,
        prompt_id: str,
        *,
        owner_id: str,
        deadline: float | None = None,
    ) -> Job:
        if not prompt_id:
            raise JobNotFound("prompt_id must not be empty")
        saved = self._runs.get(server_id, prompt_id)
        self._authorize_owner(saved, prompt_id, owner_id)
        if saved is not None and saved.status == "lost":
            return saved
        gateway = self._gateway_factory(self._servers.connection(server_id))
        if deadline is None:
            history = gateway.get_history(prompt_id)
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._persisted_or_raise(saved, prompt_id)
            history = gateway.get_history(prompt_id, timeout_seconds=remaining)
        if history:
            status_info = history.get("status", {})
            status = str(status_info.get("status_str", "")).lower()
            if status in {"error", "interrupted", "cancelled"}:
                job = self._copy(
                    saved,
                    server_id,
                    prompt_id,
                    status,
                    error=self._format_errors(history),
                    owner_id=owner_id,
                )
            elif status_info.get("completed", False) or history.get("outputs"):
                job_id = (
                    saved.job_id
                    if saved and saved.job_id
                    else derive_legacy_job_id(server_id, prompt_id)
                )
                outputs: tuple[dict[str, Any], ...] = tuple(
                    self._outputs(server_id, prompt_id, job_id, history.get("outputs", {}))
                )
                if self._artifacts is None:
                    outputs = tuple(
                        {
                            **output,
                            "canonical_uri": output["resource_uri"],
                            "resource_uri": output["legacy_uri"],
                        }
                        for output in outputs
                    )
                job = self._copy(
                    saved,
                    server_id,
                    prompt_id,
                    "completed",
                    outputs=outputs,
                    owner_id=owner_id,
                )
            else:
                job = self._copy(saved, server_id, prompt_id, "running", owner_id=owner_id)
            if self._artifacts is not None and job.status == "completed":
                self._artifacts.terminalize(job, job.outputs)
            else:
                self._runs.save(job)
            return job
        if deadline is None:
            queue = gateway.get_queue()
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._persisted_or_raise(saved, prompt_id)
            queue = gateway.get_queue(timeout_seconds=remaining)
        if self._in_queue(queue.get("queue_running", []), prompt_id):
            job = self._copy(saved, server_id, prompt_id, "running", owner_id=owner_id)
            self._runs.save(job)
            return job
        if self._in_queue(queue.get("queue_pending", []), prompt_id):
            job = self._copy(saved, server_id, prompt_id, "queued", owner_id=owner_id)
            self._runs.save(job)
            return job
        return self._persisted_or_raise(saved, prompt_id)

    def wait(
        self,
        server_id: str,
        prompt_id: str,
        *,
        timeout_seconds: float,
        progress: Callable[[dict[str, Any]], None] | None = None,
        cancel_check: Callable[[], None] | None = None,
        owner_id: str = "",
    ) -> Job:
        """Wait within an absolute deadline while preserving the prompt handle."""
        deadline = time.monotonic() + max(timeout_seconds, 0)
        saved = self._runs.get(server_id, prompt_id)
        self._authorize_owner(saved, prompt_id, owner_id)
        gateway = self._gateway_factory(self._servers.connection(server_id))
        if cancel_check is not None:
            cancel_check()
        if saved and saved.client_id and time.monotonic() < deadline:
            try:
                for event in gateway.ws_events(
                    saved.client_id,
                    prompt_id,
                    max(0.0, deadline - time.monotonic()),
                    cancel_check,
                ):
                    if time.monotonic() >= deadline:
                        return self._persisted_or_raise(saved, prompt_id)
                    if cancel_check is not None:
                        cancel_check()
                    if progress is not None:
                        progress(event)
                    event_type = str(event.get("type", ""))
                    data = event.get("data", {})
                    if event_type in {"execution_error", "execution_interrupted"}:
                        break
                    if event_type == "executing" and data.get("node") is None:
                        break
                if time.monotonic() >= deadline:
                    return self._persisted_or_raise(saved, prompt_id)
                job = self._get_until(
                    server_id,
                    prompt_id,
                    owner_id=owner_id,
                    deadline=deadline,
                )
                if job.status in _TERMINAL_STATUSES:
                    return job
            except ServerOffline:
                logger.warning(
                    "ComfyUI WebSocket wait failed; falling back to polling",
                    exc_info=True,
                )
        while time.monotonic() < deadline:
            if cancel_check is not None:
                cancel_check()
            job = self._get_until(
                server_id,
                prompt_id,
                owner_id=owner_id,
                deadline=deadline,
            )
            if job.status in _TERMINAL_STATUSES:
                return job
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        return self._persisted_or_raise(saved, prompt_id)

    def cancel(self, server_id: str, prompt_id: str, *, owner_id: str = "") -> Job:
        if not prompt_id:
            raise JobNotFound("prompt_id must not be empty")
        existing = self._runs.get(server_id, prompt_id)
        self._authorize_owner(existing, prompt_id, owner_id)
        if existing is None:
            raise JobNotFound(f"Job not found: {prompt_id}")
        current = self.get(server_id, prompt_id, owner_id=owner_id)
        if current.status in _TERMINAL_STATUSES:
            return current
        gateway = self._gateway_factory(self._servers.connection(server_id))
        if current.status == "running":
            raise UnsafeCancel(
                "Safe targeted cancellation is unavailable for a running ComfyUI job"
            )
        gateway.queue_delete([prompt_id])
        history = gateway.get_history(prompt_id)
        if history:
            return self.get(server_id, prompt_id, owner_id=owner_id)
        queue = gateway.get_queue()
        if self._in_queue(queue.get("queue_running", []), prompt_id):
            return self._copy(current, server_id, prompt_id, "running", owner_id=owner_id)
        if self._in_queue(queue.get("queue_pending", []), prompt_id):
            return current
        job = self._copy(current, server_id, prompt_id, "cancelled", owner_id=owner_id)
        self._runs.save(job)
        return job

    def _persisted_or_raise(self, fallback: Job | None, prompt_id: str) -> Job:
        persisted = self._runs.get(fallback.server_id, prompt_id) if fallback is not None else None
        if persisted is not None:
            return persisted
        raise JobNotFound(f"Job not found: {prompt_id}")

    @staticmethod
    def _authorize_owner(saved: Job | None, prompt_id: str, owner_id: str) -> None:
        if owner_id and (saved is None or saved.owner_id != owner_id):
            raise JobNotFound(f"Job not found: {prompt_id}")

    @staticmethod
    def _copy(
        saved: Job | None,
        server_id: str,
        prompt_id: str,
        status: str,
        *,
        outputs: tuple[dict[str, Any], ...] | None = None,
        error: str | None = None,
        owner_id: str = "",
    ) -> Job:
        return Job(
            prompt_id=prompt_id,
            server_id=server_id,
            workflow_id=saved.workflow_id if saved else "",
            status=status,
            outputs=saved.outputs if outputs is None and saved else (outputs or ()),
            error=saved.error if error is None and saved else (error or ""),
            idempotency_key=saved.idempotency_key if saved else "",
            client_id=saved.client_id if saved else "",
            request_digest=saved.request_digest if saved else "",
            owner_id=saved.owner_id if saved else owner_id,
            job_id=saved.job_id if saved else "",
            plan_id=saved.plan_id if saved else "",
            revision_id=saved.revision_id if saved else "",
            deployment_id=saved.deployment_id if saved else "",
            plan_digest=saved.plan_digest if saved else "",
        )

    @staticmethod
    def _in_queue(items: builtins.list[Any], prompt_id: str) -> bool:
        return any(len(item) > 1 and item[1] == prompt_id for item in items)

    @staticmethod
    def _outputs(
        server_id: str,
        prompt_id: str,
        job_id: str,
        outputs: dict[str, Any],
    ) -> builtins.list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if not isinstance(outputs, dict):
            return result
        for raw_node_id, node_output in outputs.items():
            if not isinstance(node_output, dict):
                continue
            try:
                node_id = validate_identifier(str(raw_node_id), field="upstream_node_id")
            except ValueError:
                continue
            for key, fallback in _OUTPUT_MEDIA:
                items = node_output.get(key, [])
                if not isinstance(items, list):
                    continue
                for output_index, item in enumerate(items):
                    if not isinstance(item, dict):
                        continue
                    storage_type = str(item.get("type", "output"))
                    if storage_type != "output":
                        continue
                    try:
                        filename, subfolder = validate_media_locator(
                            item.get("filename"), item.get("subfolder", "")
                        )
                    except ValueError:
                        continue
                    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                    media_type = classify_media_type(mime_type, fallback)
                    artifact_id = derive_legacy_artifact_id(
                        job_id,
                        node_id,
                        key,
                        output_index,
                        filename,
                        subfolder,
                        storage_type,
                    )
                    legacy_index = len(result)
                    result.append(
                        {
                            "artifact_id": artifact_id,
                            "upstream_node_id": node_id,
                            "output_key": key,
                            "upstream_output_index": output_index,
                            "legacy_index": legacy_index,
                            "filename": filename,
                            "subfolder": subfolder,
                            "type": storage_type,
                            "storage_type": storage_type,
                            "media_type": media_type,
                            "mime_type": mime_type,
                            "resource_uri": canonical_resource_uri("artifact", artifact_id),
                            "legacy_uri": (
                                f"comfyui://outputs/{server_id}/{prompt_id}/{legacy_index}"
                            ),
                        }
                    )
        return result

    @staticmethod
    def _format_errors(history: dict[str, Any]) -> str:
        messages = history.get("status", {}).get("messages", [])
        if not isinstance(messages, list):
            return "Workflow execution failed"
        parts: list[str] = []
        length = 0
        for message in messages[:_MAX_ERROR_MESSAGES]:
            if not isinstance(message, list) or not message:
                continue
            event = str(message[0])
            if _SAFE_ERROR_FIELD.fullmatch(event) is None:
                event = "execution_error"
            fields = [event]
            payload = message[1] if len(message) >= 2 else None
            if isinstance(payload, dict):
                for key in ("node_id", "node_type", "exception_type"):
                    value = payload.get(key)
                    if value is not None and _SAFE_ERROR_FIELD.fullmatch(str(value)):
                        fields.append(f"{key}={value}")
                if payload.get("exception_message") is not None:
                    fields.append("message=redacted_upstream_error")
            part = " ".join(fields)
            separator = 2 if parts else 0
            remaining = _MAX_ERROR_LENGTH - length - separator
            if remaining <= 0:
                break
            parts.append(part[:remaining])
            length += separator + len(parts[-1])
        return "; ".join(parts) if parts else "Workflow execution failed"
