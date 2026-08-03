"""Immutable retry planning and exactly-once canonical Job submission."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from comfyui_mcp_skills.application.control_plane_ports import ExecutionSubmitter
from comfyui_mcp_skills.application.diagnostic_ports import RetryRepository
from comfyui_mcp_skills.domain.control_plane import validate_control_plane_id
from comfyui_mcp_skills.domain.errors import (
    RepairPlanConflict,
    RepairPlanNotFound,
    RetryNotAllowed,
)
from comfyui_mcp_skills.domain.identifiers import validate_identifier
from comfyui_mcp_skills.domain.models import Job

_PLAN_TTL = timedelta(hours=1)
_FAILURE_STATUSES = frozenset({"error", "interrupted", "cancelled", "lost"})


def _owner(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
        raise ValueError("owner_id must contain between 1 and 256 characters")
    return value


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat()


def _json_copy(value: object, field: str) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
        copied = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} must contain finite JSON") from exc
    if len(encoded.encode("utf-8")) > 1_048_576:
        raise ValueError(f"{field} exceeds bounded JSON size")
    return copied


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_equal(left: object, right: object) -> bool:
    return _digest(left) == _digest(right)


def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    public_keys = (
        "repair_plan_id",
        "plan_digest",
        "resource_uri",
        "original_job_id",
        "workflow_id",
        "server_id",
        "pinned_plan_id",
        "pinned_revision_id",
        "pinned_deployment_id",
        "normalized_changes",
        "diff",
        "original_arguments_digest",
        "resulting_arguments_digest",
        "status",
        "created_at",
        "expires_at",
        "result_job_id",
        "result_job_uri",
        "retry_of",
        "committed_at",
    )
    return {key: copy.deepcopy(plan[key]) for key in public_keys if key in plan}


class RetryService:
    def __init__(
        self,
        repository: RetryRepository,
        execution: ExecutionSubmitter,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._execution = execution
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def plan(self, job_id: str, owner_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        job_id = validate_control_plane_id("job", job_id)
        owner_id = _owner(owner_id)
        context = self._repository.get_retry_context(job_id, owner_id)
        if context is None:
            raise RepairPlanNotFound("Retry subject was not found", details={"job_id": job_id})
        if context.get("status") not in _FAILURE_STATUSES:
            raise RetryNotAllowed("Only failed or interrupted Jobs can be retried")
        if context.get("legacy_migrated"):
            raise RetryNotAllowed("Legacy Jobs without execution pins cannot be retried")
        pins = (
            context.get("plan_id"),
            context.get("revision_id"),
            context.get("deployment_id"),
            context.get("content_digest"),
        )
        if any(not isinstance(value, str) or not value for value in pins):
            raise RetryNotAllowed("Retry requires complete immutable execution pins")
        original = _json_copy(context.get("raw_arguments"), "original_arguments_snapshot")
        if not isinstance(original, dict):
            raise RetryNotAllowed("Original arguments snapshot is unavailable")
        normalized = _json_copy(changes, "changes")
        if not isinstance(normalized, dict):
            raise ValueError("changes must be a JSON object")
        normalized = {str(key): normalized[key] for key in sorted(normalized)}
        resulting = copy.deepcopy(original)
        diff: list[dict[str, Any]] = []
        for key, after in normalized.items():
            pointer = "/arguments/" + key.replace("~", "~0").replace("/", "~1")
            if key in original:
                operation = "unchanged" if _json_equal(original[key], after) else "replace"
                before = copy.deepcopy(original[key])
            else:
                operation, before = "add", None
            resulting[key] = copy.deepcopy(after)
            diff.append(
                {
                    "path": pointer,
                    "operation": operation,
                    "before": before,
                    "after": copy.deepcopy(after),
                }
            )
        now = self._clock()
        created_at = _time(now)
        expires_at = _time(now + _PLAN_TTL)
        immutable = {
            "owner_id": owner_id,
            "original_job_id": job_id,
            "workflow_id": str(context["workflow_id"]),
            "server_id": str(context["server_id"]),
            "pinned_plan_id": str(context["plan_id"]),
            "pinned_revision_id": str(context["revision_id"]),
            "pinned_deployment_id": str(context["deployment_id"]),
            "pinned_content_digest": str(context["content_digest"]),
            "original_arguments_snapshot": original,
            "original_arguments_digest": _digest(original),
            "normalized_changes": normalized,
            "resulting_arguments": resulting,
            "resulting_arguments_digest": _digest(resulting),
            "diff": diff,
            "created_at": created_at,
            "expires_at": expires_at,
        }
        plan_digest = _digest(immutable)
        plan_id = "repair_plan_" + plan_digest
        plan = {
            "repair_plan_id": plan_id,
            "plan_digest": plan_digest,
            "resource_uri": f"comfyui://plans/{plan_id}",
            **immutable,
            "status": "planned",
        }
        return _public_plan(self._repository.save_repair_plan(plan))

    def get(self, repair_plan_id: str, owner_id: str) -> dict[str, Any]:
        repair_plan_id = validate_identifier(repair_plan_id, field="repair_plan_id")
        owner_id = _owner(owner_id)
        plan = self._repository.get_repair_plan(repair_plan_id, owner_id)
        if plan is None:
            raise RepairPlanNotFound(
                "Repair plan was not found", details={"repair_plan_id": repair_plan_id}
            )
        return _public_plan(plan)

    def commit(self, repair_plan_id: str, plan_digest: str, owner_id: str) -> dict[str, Any]:
        repair_plan_id = validate_identifier(repair_plan_id, field="repair_plan_id")
        owner_id = _owner(owner_id)
        if (
            not isinstance(plan_digest, str)
            or len(plan_digest) != 64
            or any(character not in "0123456789abcdef" for character in plan_digest)
        ):
            raise RepairPlanConflict("Repair plan digest is invalid")
        plan = self._repository.get_repair_plan(repair_plan_id, owner_id)
        if plan is None:
            raise RepairPlanNotFound(
                "Repair plan was not found", details={"repair_plan_id": repair_plan_id}
            )
        if plan.get("plan_digest") != plan_digest:
            raise RepairPlanConflict("Repair plan digest conflicts")
        if plan.get("status") == "committed":
            return _public_plan(plan)
        commit_started_at = self._clock()
        try:
            plan = self._repository.reserve_repair_plan_commit(
                repair_plan_id,
                plan_digest,
                owner_id,
                now=commit_started_at,
            )
        except (LookupError, ValueError) as exc:
            raise RepairPlanConflict("Repair plan is expired or conflicts") from exc
        try:
            job = self._execution.submit(
                str(plan["server_id"]),
                str(plan["workflow_id"]),
                _json_copy(plan["resulting_arguments"], "resulting_arguments"),
                idempotency_key=f"repair:{repair_plan_id}",
                owner_id=owner_id,
                client_id=repair_plan_id,
                revision_id=str(plan["pinned_revision_id"]),
                deployment_id=str(plan["pinned_deployment_id"]),
                content_digest=str(plan["pinned_content_digest"]),
                retry_of=str(plan["original_job_id"]),
            )
        except Exception as exc:
            raise RetryNotAllowed("Canonical retry Job could not be submitted") from exc
        retry_job_id = job.job_id if isinstance(job, Job) else str(getattr(job, "job_id", ""))
        if not retry_job_id:
            raise RetryNotAllowed("Canonical retry Job did not return an ID")
        try:
            committed = self._repository.mark_repair_plan_committed(
                repair_plan_id,
                plan_digest,
                owner_id,
                retry_job_id,
                now=commit_started_at,
            )
        except (LookupError, ValueError) as exc:
            raise RepairPlanConflict("Repair plan commit conflicts") from exc
        return _public_plan(committed)
