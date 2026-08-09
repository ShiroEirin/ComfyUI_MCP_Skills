"""Approved runtime restart execution with atomic drain/fence coordination."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from comfyui_mcp_skills.application.runtime_control import RuntimeController
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import (
    RestartApprovalInvalid,
    RestartExecutionFailed,
    RestartPlanConflict,
    RestartPlanNotFound,
)
from comfyui_mcp_skills.infrastructure.persistence.runtime_restart import (
    SQLiteRuntimeRestartRepository,
)

_TERMINAL_JOBS = frozenset({"completed", "error", "interrupted", "cancelled", "lost"})
_KNOWN_JOB_STATUSES = _TERMINAL_JOBS | {"queued", "submitted", "running"}
_PLAN_TTL = timedelta(hours=1)
_APPROVAL_TTL = timedelta(hours=1)
_DRAIN_MAX_JOBS = 10000
_DRAIN_WAIT_SECONDS = 10.0
_DRAIN_POLL_SECONDS = 0.1


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _controller_binding(config: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Normalize the server runtime binding for approval pinning; an absent
    binding is a first-class value (controller_available=false)."""
    runtime = config.get("runtime")
    if not isinstance(runtime, dict) or not isinstance(runtime.get("adapter"), str):
        binding: dict[str, Any] = {"adapter": None}
        return binding, _digest(binding)
    adapter = runtime["adapter"]
    if adapter == "systemd":
        binding = {"adapter": "systemd", "unit": runtime.get("unit")}
    elif adapter == "docker":
        binding = {"adapter": "docker", "container": runtime.get("container")}
    elif adapter == "windows_service":
        binding = {"adapter": "windows_service", "service": runtime.get("service")}
    else:
        binding = {"adapter": adapter}
    return binding, _digest(binding)


class RuntimeRestartService:
    """Own the restart plan/approve/commit state machine and the drain window."""

    def __init__(
        self,
        servers: ServerRegistry,
        repository: SQLiteRuntimeRestartRepository,
        *,
        controller_provider: Callable[[str], RuntimeController | None] | None = None,
        controller: RuntimeController | None = None,
        clock: Callable[[], datetime] | None = None,
        drain_wait_seconds: float = _DRAIN_WAIT_SECONDS,
        drain_poll_seconds: float = _DRAIN_POLL_SECONDS,
    ) -> None:
        self._servers = servers
        self._repository = repository
        self._controller = controller
        self._controller_provider = controller_provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._drain_wait = drain_wait_seconds
        self._drain_poll = drain_poll_seconds

    # -- plan -------------------------------------------------------------

    def plan(self, server_id: str, owner_id: str) -> dict[str, Any]:
        server_id = self._validate_identifier(server_id)
        owner_id = self._validate_owner(owner_id)
        config = self._servers.connection(server_id)
        binding, binding_digest = _controller_binding(config)
        controller = self._controller_for(server_id)
        now = self._clock()
        impact = self._repository.server_active_jobs(server_id, limit=_DRAIN_MAX_JOBS)
        plan_digest = _digest(["runtime_restart", server_id, impact])
        existing = self._repository.find_reusable_plan(owner_id, server_id, plan_digest, now)
        if existing is not None:
            return self._plan_view(existing)
        plan_id = "runtime_plan_" + hashlib.sha256(
            _canonical(
                [owner_id, server_id, plan_digest, now.isoformat(), uuid.uuid4().hex]
            ).encode()
        ).hexdigest()[:32]
        approval_id = "runtime_approval_" + hashlib.sha256(
            _canonical(["runtime_restart_approval", plan_id]).encode()
        ).hexdigest()[:32]
        approval_expires_at = (now + _APPROVAL_TTL).isoformat()
        expires_at = (now + _PLAN_TTL).isoformat()
        self._repository.save_plan(
            plan_id=plan_id,
            approval_id=approval_id,
            owner_id=owner_id,
            server_id=server_id,
            plan_digest=plan_digest,
            approved_summary={"job_count": len(impact)},
            impact_rows=[(job_id, job_owner, status) for job_id, job_owner, status in impact],
            controller_binding=binding,
            controller_binding_digest=binding_digest,
            controller_available=controller is not None,
            approval_expires_at=approval_expires_at,
            expires_at=expires_at,
            now=now,
        )
        return self._plan_view(
            self._repository.get_plan(plan_id, owner_id)  # type: ignore[arg-type]
        )

    def approve(
        self, plan_id: str, decision: str, owner_id: str, reason: str = ""
    ) -> dict[str, Any]:
        plan_id = self._validate_identifier(plan_id)
        owner_id = self._validate_owner(owner_id)
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        if not isinstance(reason, str) or len(reason) > 512:
            raise ValueError("reason must be a string up to 512 characters")
        return self._plan_view(
            self._repository.approve(plan_id, decision, owner_id, reason, self._clock())
        )

    # -- commit -----------------------------------------------------------

    def commit(
        self,
        plan_id: str,
        plan_digest: str,
        approval_id: str,
        owner_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        plan_id = self._validate_identifier(plan_id)
        owner_id = self._validate_owner(owner_id)
        if not isinstance(plan_digest, str) or len(plan_digest) != 64:
            raise ValueError("plan_digest must be a sha256 hex digest")
        if not isinstance(approval_id, str) or len(approval_id) != 49:
            raise ValueError("approval_id is invalid")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError("request_id must be between 1 and 128 characters")
        now = self._clock()

        # Receipt-first: a settled commit replays its original outcome,
        # including re-raising the original domain error on failure.
        plan = self._repository.get_plan(plan_id, owner_id)
        if plan is None:
            raise RestartPlanNotFound("Restart plan was not found")
        if plan["plan_digest"] != plan_digest:
            raise RestartPlanConflict("Restart plan digest mismatch")
        if plan["approval_id"] != approval_id:
            raise RestartApprovalInvalid("Approval does not belong to this restart plan")
        receipt = self._repository.receipt(plan_id, request_id)
        if receipt is not None and receipt["status"] in ("completed", "failed"):
            return self._replay(receipt)
        if plan["status"] in ("draining", "restarting"):
            return self._in_progress(plan)
        if plan["status"] != "approved":
            raise RestartPlanConflict(
                f"Restart plan is not approved for execution (status={plan['status']})"
            )
        if self._is_expired(plan["approval_expires_at"], now):
            raise RestartApprovalInvalid("Restart approval has expired")
        if not plan["controller_available"]:
            raise RestartApprovalInvalid("No runtime controller is configured for this server")
        config = self._servers.connection(plan["server_id"])
        _binding, binding_digest = _controller_binding(config)
        if binding_digest != plan["controller_binding_digest"]:
            raise RestartPlanConflict(
                "Runtime controller binding changed since approval; create a new plan"
            )
        controller = self._controller_for(plan["server_id"])
        if controller is None:
            raise RestartApprovalInvalid("Runtime controller is no longer available")

        # Drain: fence ON with the execution intent enumerated and persisted
        # inside the same transaction (no gap between fence and snapshot).
        self._repository.begin_drain(plan_id, request_id, now)
        try:
            if not self._drain_settled(plan["server_id"]):
                raise RestartExecutionFailed(
                    "submissions did not settle within the drain window",
                    details={
                        "message": "submissions did not settle within the drain window",
                        "error_code": "RESTART_DRAIN_TIMEOUT",
                        "retryable": True,
                    },
                )
            final = self._repository.refresh_execution_intent(plan_id, now)
            outcome = controller.restart(plan["server_id"])
            terminal_now = self._clock()
            result = {
                "status": "completed",
                "execution_impact_summary": final["execution_impact_summary"],
                "execution_impact_digest": final["execution_impact_digest"],
                "controller_outcome": outcome,
                "committed_at": terminal_now.isoformat(),
            }
            self._repository.complete(plan_id, result, terminal_now)
            return self._plan_view(self._repository.get_plan(plan_id, owner_id))  # type: ignore[arg-type]
        except RestartExecutionFailed as exc:
            terminal_now = self._clock()
            normalized = RestartExecutionFailed(
                exc.message,
                details={
                    "message": exc.message,
                    "error_code": (exc.details or {}).get("error_code", exc.code),
                    "retryable": (exc.details or {}).get("retryable", exc.retryable),
                    **{
                        key: value
                        for key, value in (exc.details or {}).items()
                        if key not in ("message", "error_code", "retryable")
                    },
                },
            )
            self._fail_with_receipt(plan_id, owner_id, normalized, terminal_now)
            raise normalized from exc
        except Exception as exc:
            failure = RestartExecutionFailed(
                str(exc),
                details={
                    "message": str(exc),
                    "error_code": "RESTART_EXECUTION_FAILED",
                    "retryable": True,
                },
            )
            terminal_now = self._clock()
            self._fail_with_receipt(plan_id, owner_id, failure, terminal_now)
            raise failure from exc

    def _fail_with_receipt(
        self,
        plan_id: str,
        owner_id: str,
        failure: RestartExecutionFailed,
        now: datetime,
    ) -> None:
        """Persist a failure receipt built from the SAME current execution
        intent (summary+digest from one read) and a single committed_at shared
        with the terminal column, so a replay reconstructs an identical error."""
        current = self._repository.get_plan(plan_id, owner_id)
        summary = (current or {}).get("execution_impact_summary")
        digest = (current or {}).get("execution_impact_digest")
        if summary is None or digest is None:
            summary = {"job_count": 0, "source": "execution"}
            digest = None
        details = dict(failure.details or {})
        details.setdefault("message", failure.message)
        result = {
            "status": "failed",
            "execution_impact_summary": summary,
            "execution_impact_digest": digest,
            "error_code": details.get("error_code", failure.code),
            "retryable": details.get("retryable", failure.retryable),
            "details": details,
            "committed_at": now.isoformat(),
        }
        self._repository.fail(plan_id, result, error="restart_execution_failed", now=now)

    def get(
        self,
        plan_id: str,
        owner_id: str,
        *,
        limit: int = 200,
        cursor: int = 0,
    ) -> dict[str, Any]:
        plan_id = self._validate_identifier(plan_id)
        owner_id = self._validate_owner(owner_id)
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if not isinstance(cursor, int) or cursor < 0:
            raise ValueError("cursor must be a non-negative integer")
        plan = self._repository.get_plan(plan_id, owner_id)
        if plan is None:
            raise RestartPlanNotFound("Restart plan was not found")
        view = self._plan_view(plan)
        jobs = self._repository.impact_jobs(plan_id, limit=limit + 1, cursor=cursor)
        has_more = len(jobs) > limit
        view["impact_jobs"] = jobs[:limit]
        view["impact_total"] = self._repository.impact_count(plan_id)
        view["next_cursor"] = jobs[limit]["ordinal"] if has_more else None
        return view

    # -- recovery ---------------------------------------------------------

    def recover(self) -> dict[str, Any]:
        """Startup recovery: clear stale admissions, then fail orphaned
        draining/restarting sessions without auto-retrying the restart."""
        cleared = self._repository.clear_admissions()
        recovered = self._repository.recover_orphaned(self._clock())
        return {"cleared_admissions": cleared, "recovered_plans": len(recovered)}

    # -- internals --------------------------------------------------------

    def _drain_settled(self, server_id: str) -> bool:
        deadline = time.monotonic() + self._drain_wait
        while time.monotonic() < deadline:
            if self._repository.pending_admissions(server_id) == 0:
                return True
            time.sleep(self._drain_poll)
        return self._repository.pending_admissions(server_id) == 0

    def _replay(self, plan: dict[str, Any]) -> dict[str, Any]:
        result = plan["commit_result"] or {}
        error_code = result.get("error_code")
        if error_code:
            from comfyui_mcp_skills.domain.errors import RestartExecutionFailed

            details = dict(result.get("details", {}))
            raise RestartExecutionFailed(
                str(details.get("message", error_code)),
                details=details,
            )
        return self._plan_view(plan)

    @staticmethod
    def _in_progress(plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "plan_id": plan["plan_id"],
            "status": plan["status"],
            "execution_impact_summary": plan["execution_impact_summary"],
            "in_progress": True,
        }

    def _controller_for(self, server_id: str) -> RuntimeController | None:
        if self._controller_provider is not None:
            return self._controller_provider(server_id)
        return self._controller

    def _plan_view(self, plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "plan_id": plan["plan_id"],
            "approval_id": plan["approval_id"],
            "approval_uri": f"comfyui://approvals/{plan['approval_id']}",
            "server_id": plan["server_id"],
            "owner_id": plan["owner_id"],
            "plan_digest": plan["plan_digest"],
            "status": plan["status"],
            "approval_actor": plan["approval_actor"],
            "approval_reason": plan["approval_reason"],
            "approval_decided_at": plan["approval_decided_at"],
            "approved_impact_summary": plan["approved_impact_summary"],
            "execution_impact_summary": plan["execution_impact_summary"],
            "execution_impact_digest": plan["execution_impact_digest"],
            "execution_intent_committed_at": plan["execution_intent_committed_at"],
            "controller_binding": plan["controller_binding"],
            "controller_binding_digest": plan["controller_binding_digest"],
            "controller_available": plan["controller_available"],
            "approval_expires_at": plan["approval_expires_at"],
            "expires_at": plan["expires_at"],
            "resource_uri": plan["resource_uri"],
            "error": plan["error"],
            "commit_result": plan["commit_result"],
        }

    @staticmethod
    def _validate_identifier(value: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError("plan_id must be a bounded string")
        return value

    @staticmethod
    def _validate_owner(value: str) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= 256:
            raise ValueError("owner_id must be a bounded string")
        return value

    @staticmethod
    def _is_expired(expires_at: str, now: datetime) -> bool:
        try:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        return parsed <= now
