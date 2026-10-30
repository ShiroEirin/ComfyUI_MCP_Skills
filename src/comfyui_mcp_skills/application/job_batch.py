"""Synchronous, failure-isolated batch operations for durable Jobs."""

from __future__ import annotations

import copy
import math
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from comfyui_mcp_skills.application.control_plane_ports import ExecutionSubmitter
from comfyui_mcp_skills.application.jobs import JobWaitTimeout
from comfyui_mcp_skills.application.json_codec import canonical_json_bytes
from comfyui_mcp_skills.domain.errors import ComfyUISkillsError, WorkflowArgumentsError
from comfyui_mcp_skills.domain.models import Job

DEFAULT_MAX_BATCH_ITEMS = 100
DEFAULT_MAX_ARGUMENT_BYTES = 1024 * 1024
_MAX_ERROR_MESSAGE_LENGTH = 2048
_MAX_WAIT_SECONDS = 24 * 60 * 60
_FAILED_STATUSES = frozenset({"error", "interrupted", "cancelled", "lost"})
_OPTIONAL_SUBMIT_FIELDS = frozenset(
    {
        "idempotency_key",
        "client_id",
        "revision_id",
        "deployment_id",
        "content_digest",
        "retry_of",
        "priority",
        "targets",
    }
)
_STRING_SUBMIT_FIELDS = frozenset(
    {
        "idempotency_key",
        "client_id",
        "revision_id",
        "deployment_id",
        "content_digest",
        "retry_of",
    }
)


class BatchJobService(Protocol):
    """The synchronous Job operations needed by :class:`BatchService`."""

    def wait(
        self,
        server_id: str,
        prompt_id: str,
        *,
        timeout_seconds: float,
        owner_id: str = "",
    ) -> Job: ...

    def cancel(self, server_id: str, prompt_id: str, *, owner_id: str = "") -> Job: ...


class BatchService:
    """Keep batch handles in memory while isolating each item's failures.

    Transport adapters own thread offloading. This service deliberately performs
    ordinary synchronous port calls and never registers MCP tools or makes HTTP
    requests itself.
    """

    def __init__(
        self,
        execution: ExecutionSubmitter,
        jobs: BatchJobService,
        *,
        max_items: int = DEFAULT_MAX_BATCH_ITEMS,
        max_argument_bytes: int = DEFAULT_MAX_ARGUMENT_BYTES,
    ) -> None:
        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
            raise ValueError("max_items must be a positive integer")
        if (
            isinstance(max_argument_bytes, bool)
            or not isinstance(max_argument_bytes, int)
            or max_argument_bytes < 1
        ):
            raise ValueError("max_argument_bytes must be a positive integer")
        self._execution = execution
        self._jobs = jobs
        self._max_items = max_items
        self._max_argument_bytes = max_argument_bytes
        self._batches: dict[str, list[dict[str, Any]]] = {}

    def submit(
        self,
        descriptors: Sequence[Mapping[str, Any]],
        *,
        owner_id: str = "",
    ) -> dict[str, Any]:
        """Submit every valid descriptor and return one stable batch handle."""
        if not isinstance(descriptors, Sequence) or isinstance(
            descriptors, (str, bytes, bytearray)
        ):
            raise WorkflowArgumentsError("jobs must be a sequence of Job descriptors")
        if not descriptors:
            raise WorkflowArgumentsError("jobs must contain at least one Job descriptor")
        if len(descriptors) > self._max_items:
            raise WorkflowArgumentsError(f"jobs must contain at most {self._max_items} items")
        if not isinstance(owner_id, str):
            raise WorkflowArgumentsError("owner_id must be a string")

        batch_id = f"batch_{uuid.uuid4().hex}"
        items: list[dict[str, Any]] = []
        for index, descriptor in enumerate(descriptors):
            item = _empty_item(index)
            items.append(item)
            try:
                server_id, workflow_id, arguments, options = self._validate_descriptor(
                    descriptor
                )
                item.update(server_id=server_id, workflow_id=workflow_id)
                job = self._execution.submit(
                    server_id,
                    workflow_id,
                    arguments,
                    owner_id=owner_id,
                    **options,
                )
                _apply_job(item, job)
            except Exception as exc:
                item.update(status="failed", error=_structured_error(exc))

        self._batches[batch_id] = items
        return _result(batch_id, items)

    def wait(
        self,
        batch_id: str,
        *,
        timeout_seconds: float = 60.0,
        owner_id: str = "",
    ) -> dict[str, Any]:
        """Wait for each submitted item without cancelling any remaining work."""
        items = self._batch(batch_id)
        timeout = _validate_timeout(timeout_seconds)
        for item in items:
            if not item["prompt_id"]:
                continue
            try:
                job = self._jobs.wait(
                    item["server_id"],
                    item["prompt_id"],
                    timeout_seconds=timeout,
                    owner_id=owner_id,
                )
                _apply_job(item, job)
            except JobWaitTimeout as exc:
                _apply_job(item, exc.job)
                item.update(status="timed_out", last_status=exc.job.status, error=None)
            except TimeoutError:
                item.update(status="timed_out", error=None)
            except Exception as exc:
                item.update(status="failed", error=_structured_error(exc))
        return _result(batch_id, items)

    def cancel(self, batch_id: str, *, owner_id: str = "") -> dict[str, Any]:
        """Cancel every submitted item, retaining errors and continuing on failure."""
        items = self._batch(batch_id)
        for item in items:
            if not item["prompt_id"]:
                continue
            try:
                job = self._jobs.cancel(
                    item["server_id"],
                    item["prompt_id"],
                    owner_id=owner_id,
                )
                _apply_job(item, job)
            except Exception as exc:
                item.update(status="failed", error=_structured_error(exc))
        return _result(batch_id, items)

    def _batch(self, batch_id: str) -> list[dict[str, Any]]:
        if not isinstance(batch_id, str) or not batch_id:
            raise WorkflowArgumentsError("batch_id must be a non-empty string")
        try:
            return self._batches[batch_id]
        except KeyError as exc:
            raise WorkflowArgumentsError(f"Batch not found: {batch_id}") from exc

    def _validate_descriptor(
        self,
        descriptor: object,
    ) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
        if not isinstance(descriptor, Mapping):
            raise WorkflowArgumentsError("Job descriptor must be an object")
        allowed = {"server_id", "workflow_id", "arguments", *_OPTIONAL_SUBMIT_FIELDS}
        unknown = sorted(set(descriptor) - allowed, key=str)
        if unknown:
            raise WorkflowArgumentsError(f"Unknown Job descriptor fields: {unknown}")
        server_id = _identifier(descriptor.get("server_id"), "server_id")
        workflow_id = _identifier(descriptor.get("workflow_id"), "workflow_id")
        arguments = descriptor.get("arguments")
        if not isinstance(arguments, dict):
            raise WorkflowArgumentsError("arguments must be an object")
        try:
            size = len(canonical_json_bytes(arguments))
        except (TypeError, ValueError) as exc:
            raise WorkflowArgumentsError("arguments must contain finite JSON values") from exc
        if size > self._max_argument_bytes:
            raise WorkflowArgumentsError(
                f"arguments must be at most {self._max_argument_bytes} UTF-8 JSON bytes"
            )

        options: dict[str, Any] = {}
        for field in _STRING_SUBMIT_FIELDS:
            value = descriptor.get(field, "")
            if not isinstance(value, str):
                raise WorkflowArgumentsError(f"{field} must be a string")
            options[field] = value
        priority = descriptor.get("priority")
        if priority is not None and (
            isinstance(priority, bool)
            or not isinstance(priority, (int, float))
            or not math.isfinite(priority)
        ):
            raise WorkflowArgumentsError("priority must be a finite number")
        options["priority"] = priority
        targets = descriptor.get("targets", ())
        if not isinstance(targets, (list, tuple)) or len(targets) > 100:
            raise WorkflowArgumentsError("targets must be an array of at most 100 node ids")
        if any(
            not isinstance(target, str) or not target or len(target) > 256
            for target in targets
        ):
            raise WorkflowArgumentsError("targets must contain non-empty node ids")
        options["targets"] = tuple(targets)
        return server_id, workflow_id, dict(arguments), options


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
        raise WorkflowArgumentsError(
            f"{field} must be a non-empty string of at most 256 characters"
        )
    return value


def _validate_timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= _MAX_WAIT_SECONDS
    ):
        raise WorkflowArgumentsError(
            f"timeout_seconds must be a finite number between 0 and {_MAX_WAIT_SECONDS}"
        )
    return float(value)


def _empty_item(index: int) -> dict[str, Any]:
    return {
        "index": index,
        "status": "failed",
        "last_status": "",
        "server_id": "",
        "workflow_id": "",
        "prompt_id": "",
        "job_id": "",
        "error": None,
    }


def _apply_job(item: dict[str, Any], job: Job) -> None:
    item.update(
        status=job.status,
        last_status=job.status,
        server_id=job.server_id,
        workflow_id=job.workflow_id,
        prompt_id=job.prompt_id,
        job_id=job.job_id,
        error=(
            {"code": "JOB_FAILED", "message": job.error, "retryable": False, "details": {}}
            if job.status in _FAILED_STATUSES
            else None
        ),
    )


def _structured_error(exc: Exception) -> dict[str, Any]:
    code = exc.code if isinstance(exc, ComfyUISkillsError) else type(exc).__name__
    return {
        "code": str(code)[:128],
        "message": str(exc)[:_MAX_ERROR_MESSAGE_LENGTH],
        "retryable": bool(getattr(exc, "retryable", False)),
        "details": dict(exc.details) if isinstance(exc, ComfyUISkillsError) else {},
    }


def _result(batch_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "items": copy.deepcopy(items),
        "counts": {
            "completed": sum(item["status"] == "completed" for item in items),
            "failed": sum(item["status"] in _FAILED_STATUSES | {"failed"} for item in items),
            "timed_out": sum(item["status"] == "timed_out" for item in items),
        },
    }
