"""Durable G5 operation orchestration and the first real Job reconciliation work type."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from comfyui_mcp_skills.application.control_plane_ports import OrchestrationRepository
from comfyui_mcp_skills.application.ports import ComfyUIGateway
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import ServerOffline
from comfyui_mcp_skills.domain.orchestration import (
    ReconcileObservation,
    ReconcileState,
    WorkItem,
    WorkLease,
)

_TERMINAL_STATUSES = frozenset({"completed", "error", "interrupted", "cancelled", "lost"})
logger = logging.getLogger(__name__)


class WorkHandler(Protocol):
    def __call__(self, work: WorkItem, lease: WorkLease, *, now: datetime) -> None: ...


class OperationOrchestrator:
    """Claim one persisted step and dispatch it only while its fencing lease is valid."""

    def __init__(
        self,
        repository: OrchestrationRepository,
        handlers: dict[str, WorkHandler],
        *,
        lease_seconds: int = 30,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._repository = repository
        self._handlers = dict(handlers)
        self._lease_seconds = lease_seconds

    def run_once(self, worker_id: str, *, now: datetime) -> bool:
        lease = self._repository.acquire_next(worker_id, now=now, lease_seconds=self._lease_seconds)
        if lease is None:
            return False
        work = self._repository.get_work_item(lease.work_item_id)
        try:
            handler = self._handlers[work.work_type]
        except KeyError:
            self._repository.checkpoint(
                lease,
                {**work.checkpoint, "last_error": f"unsupported work type: {work.work_type}"},
                now=now,
                delay_seconds=self._lease_seconds,
            )
            return True
        handler(work, lease, now=now)
        return True


class JobReconciler:
    """Conservatively reconcile one non-terminal Job without blind resubmission."""

    def __init__(
        self,
        repository: OrchestrationRepository,
        probe: Callable[[str, str, str], ReconcileObservation],
        *,
        missing_threshold: int = 3,
        grace_seconds: int = 300,
        retry_delay_seconds: int = 30,
    ) -> None:
        if missing_threshold < 2 or grace_seconds < 0 or retry_delay_seconds < 0:
            raise ValueError("invalid reconciliation thresholds")
        self._repository = repository
        self._probe = probe
        self._missing_threshold = missing_threshold
        self._grace_seconds = grace_seconds
        self._retry_delay_seconds = retry_delay_seconds

    def __call__(self, work: WorkItem, lease: WorkLease, *, now: datetime) -> None:
        job_id = str(work.payload["job_id"])
        context = self._repository.job_context(job_id)
        if context.status in _TERMINAL_STATUSES:
            self._repository.apply_reconciliation(
                lease, checkpoint=work.checkpoint, now=now, completed=True
            )
            return
        prompt_id = context.prompt_id
        client_id = context.client_id
        if not prompt_id and not client_id:
            self._defer(work, lease, now, "upstream identities are unavailable")
            return
        try:
            observation = self._probe(context.server_id, prompt_id, client_id)
        except ServerOffline as exc:
            self._defer(work, lease, now, str(exc))
            return
        if not observation.online or observation.state == "unknown":
            self._defer(work, lease, now, observation.error or "server state is unknown")
            return
        if observation.state in {"queued", "running"}:
            checkpoint = {
                **work.checkpoint,
                "consecutive_missing": 0,
                "first_missing_at": "",
                "last_generation": observation.generation,
                "last_error": "",
            }
            self._repository.apply_reconciliation(
                lease,
                checkpoint=checkpoint,
                now=now,
                job_status=observation.state,
                generation=observation.generation,
                upstream_prompt_id=observation.upstream_prompt_id,
                delay_seconds=self._retry_delay_seconds,
            )
            return
        if observation.state in {"completed", "error", "cancelled", "interrupted"}:
            checkpoint = {
                **work.checkpoint,
                "last_generation": observation.generation,
                "last_error": observation.error,
            }
            self._repository.apply_reconciliation(
                lease,
                checkpoint=checkpoint,
                now=now,
                job_status=observation.state,
                completed=True,
                generation=observation.generation,
                upstream_prompt_id=observation.upstream_prompt_id,
                event_type="JOB_RECONCILED_TERMINAL",
                event_data={"status": observation.state},
            )
            return
        self._handle_missing(work, lease, observation, now)

    def _defer(self, work: WorkItem, lease: WorkLease, now: datetime, error: str) -> None:
        self._repository.checkpoint(
            lease,
            {
                **work.checkpoint,
                "consecutive_missing": 0,
                "first_missing_at": "",
                "last_error": error,
            },
            now=now,
            delay_seconds=self._retry_delay_seconds,
        )

    def _handle_missing(
        self,
        work: WorkItem,
        lease: WorkLease,
        observation: ReconcileObservation,
        now: datetime,
    ) -> None:
        previous_generation = str(work.checkpoint.get("last_generation", ""))
        first_missing_at = str(work.checkpoint.get("first_missing_at", "")) or _time(now)
        missing_count = int(work.checkpoint.get("consecutive_missing", 0)) + 1
        generation_changed = bool(
            previous_generation
            and observation.generation
            and previous_generation != observation.generation
        )
        grace_elapsed = now >= _parse_time(first_missing_at) + timedelta(
            seconds=self._grace_seconds
        )
        lost = missing_count >= self._missing_threshold and (generation_changed or grace_elapsed)
        checkpoint = {
            **work.checkpoint,
            "consecutive_missing": missing_count,
            "first_missing_at": first_missing_at,
            "last_generation": observation.generation or previous_generation,
            "last_error": observation.error,
        }
        self._repository.apply_reconciliation(
            lease,
            checkpoint=checkpoint,
            now=now,
            job_status="lost" if lost else None,
            completed=lost,
            generation=observation.generation,
            event_type="UPSTREAM_STATE_LOST" if lost else "",
            event_data={
                "consecutive_missing": missing_count,
                "generation_changed": generation_changed,
                "grace_elapsed": grace_elapsed,
            },
            delay_seconds=0 if lost else self._retry_delay_seconds,
        )


class ComfyUIReconcileProbe:
    """Read authoritative history/queue state while preserving unknown results."""

    def __init__(
        self,
        servers: ServerRegistry,
        gateway_factory: Callable[[dict[str, Any]], ComfyUIGateway],
    ) -> None:
        self._servers = servers
        self._gateway_factory = gateway_factory

    def __call__(self, server_id: str, prompt_id: str, client_id: str) -> ReconcileObservation:
        gateway = self._gateway_factory(self._servers.connection(server_id))
        try:
            stats = gateway.get_system_stats()
            if not isinstance(stats, dict):
                raise ValueError("system stats response must be an object")
            generation = _server_generation(stats)
            scan_budget = [10_000]
            if prompt_id:
                history = gateway.get_history(prompt_id)
                if history:
                    return _history_observation(history, generation, prompt_id)
                queue = gateway.get_queue()
                if not isinstance(queue, dict):
                    raise ValueError("queue response must be an object")
                _validate_queue_shape(queue)
                return ReconcileObservation(True, generation, _queue_state(queue, prompt_id))

            queue = gateway.get_queue()
            if not isinstance(queue, dict):
                raise ValueError("queue response must be an object")
            _validate_queue_shape(queue)
            queued = _find_prompt_by_client(queue, client_id, scan_budget)
            if queued is not None:
                recovered_prompt, state = queued
                return ReconcileObservation(
                    True,
                    generation,
                    state,
                    upstream_prompt_id=recovered_prompt,
                )
            histories = gateway.get_history_list(max_items=100)
            if not isinstance(histories, dict):
                raise ValueError("history list response must be an object")
            if len(histories) > 100:
                raise ValueError("history list exceeds reconciliation item limit")
            for recovered_prompt, history in histories.items():
                if _contains_client_id(history, client_id, scan_budget):
                    return _history_observation(history, generation, str(recovered_prompt))
            return ReconcileObservation(True, generation, "missing")
        except ServerOffline:
            return ReconcileObservation(False, error="server offline")
        except (TypeError, ValueError, RecursionError) as exc:
            logger.warning("Invalid ComfyUI reconciliation response", exc_info=True)
            return ReconcileObservation(True, error=str(exc))
        except Exception as exc:
            logger.warning("ComfyUI reconciliation probe failed", exc_info=True)
            return ReconcileObservation(False, error=str(exc))


def _queue_state(queue: dict[str, Any], prompt_id: str) -> ReconcileState:
    if _queue_contains(queue.get("queue_running", []), prompt_id):
        return "running"
    if _queue_contains(queue.get("queue_pending", []), prompt_id):
        return "queued"
    return "missing"


def _queue_contains(items: object, prompt_id: str) -> bool:
    return isinstance(items, list) and any(
        isinstance(item, list) and len(item) > 1 and item[1] == prompt_id for item in items
    )


def _validate_queue_shape(queue: dict[str, Any]) -> None:
    for key in ("queue_running", "queue_pending"):
        items = queue.get(key, [])
        if not isinstance(items, list):
            raise ValueError(f"{key} must be an array")
        if len(items) > 1_000:
            raise ValueError("queue response exceeds reconciliation item limit")


def _find_prompt_by_client(
    queue: dict[str, Any], client_id: str, budget: list[int]
) -> tuple[str, ReconcileState] | None:
    for key in ("queue_running", "queue_pending"):
        state: ReconcileState = "running" if key == "queue_running" else "queued"
        for item in queue.get(key, []):
            if (
                isinstance(item, list)
                and len(item) > 1
                and _contains_client_id(item, client_id, budget)
            ):
                return str(item[1]), state
    return None


def _contains_client_id(value: object, client_id: str, budget: list[int]) -> bool:
    if not client_id:
        return False
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        budget[0] -= 1
        if budget[0] < 0 or depth > 64:
            raise ValueError("upstream response exceeds reconciliation scan limits")
        if isinstance(current, dict):
            if current.get("client_id") == client_id:
                return True
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return False


def _history_observation(history: object, generation: str, prompt_id: str) -> ReconcileObservation:
    if not isinstance(history, dict):
        return ReconcileObservation(True, generation, "unknown")
    status_info = history.get("status", {})
    status = str(status_info.get("status_str", "")).lower() if isinstance(status_info, dict) else ""
    if status == "error":
        state: ReconcileState = "error"
    elif status == "cancelled":
        state = "cancelled"
    elif status == "interrupted":
        state = "interrupted"
    elif (isinstance(status_info, dict) and status_info.get("completed")) or history.get("outputs"):
        state = "completed"
    else:
        state = "running"
    return ReconcileObservation(True, generation, state, status, upstream_prompt_id=prompt_id)


def _server_generation(stats: dict[str, Any]) -> str:
    candidates = (
        stats.get("generation"),
        stats.get("boot_id"),
        stats.get("instance_id"),
        stats.get("system", {}).get("generation")
        if isinstance(stats.get("system"), dict)
        else None,
    )
    for candidate in candidates:
        if isinstance(candidate, (str, int)) and str(candidate):
            return str(candidate)
    return ""


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("reconciliation timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("persisted reconciliation timestamp has no timezone")
    return parsed.astimezone(timezone.utc)
