"""Explicit queue, interrupt, and host runtime control boundaries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any, Protocol

from comfyui_mcp_skills.application.ports import ComfyUIGateway, RunRepository
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.control_plane import canonical_resource_uri
from comfyui_mcp_skills.domain.identifiers import validate_identifier
from comfyui_mcp_skills.domain.models import Job


class RuntimeController(Protocol):
    def restart(self, server_id: str) -> dict[str, Any]: ...


class RuntimeControlService:
    """Keep owner-safe queue actions distinct from explicit global controls."""

    def __init__(
        self,
        servers: ServerRegistry,
        runs: RunRepository,
        gateway_factory: Callable[[dict[str, Any]], ComfyUIGateway],
        *,
        controller: RuntimeController | None = None,
        controller_provider: Callable[[str], RuntimeController | None] | None = None,
    ) -> None:
        self._servers = servers
        self._runs = runs
        self._gateway_factory = gateway_factory
        self._controller = controller
        self._controller_provider = controller_provider

    def queue_remove(
        self,
        server_id: str,
        prompt_ids: list[str],
        owner_id: str,
        *,
        execute: bool,
        allow_cross_owner: bool = False,
    ) -> dict[str, Any]:
        server_id = validate_identifier(server_id, field="server_id")
        prompt_ids = _prompt_ids(prompt_ids)
        gateway = self._gateway_factory(self._servers.connection(server_id))
        pending = _queue_prompt_ids({"queue_pending": gateway.get_queue().get("queue_pending", [])})
        if any(prompt_id not in pending for prompt_id in prompt_ids):
            raise ValueError("queue.remove targets must currently be pending")
        affected = self._affected(server_id, prompt_ids, owner_id, allow_cross_owner)
        if execute:
            gateway.queue_delete(prompt_ids)
        return {
            "operation": "queue.remove",
            "server_id": server_id,
            "executed": execute,
            "affected_jobs": [_job_summary(job) for job in affected],
            "affected_prompt_ids": prompt_ids,
        }

    def queue_clear(
        self,
        server_id: str,
        owner_id: str,
        *,
        execute: bool,
        allow_cross_owner: bool = False,
    ) -> dict[str, Any]:
        server_id = validate_identifier(server_id, field="server_id")
        gateway = self._gateway_factory(self._servers.connection(server_id))
        queue = gateway.get_queue()
        prompt_ids = _queue_prompt_ids({"queue_pending": queue.get("queue_pending", [])})
        affected = self._affected(server_id, prompt_ids, owner_id, allow_cross_owner)
        if execute:
            if not allow_cross_owner:
                raise PermissionError("Global queue clear requires management permission")
            gateway.queue_clear()
        return {
            "operation": "queue.clear",
            "server_id": server_id,
            "executed": execute,
            "affected_jobs": [_job_summary(job) for job in affected],
            "affected_prompt_ids": prompt_ids,
        }

    def interrupt(
        self,
        server_id: str,
        owner_id: str,
        *,
        execute: bool,
        allow_cross_owner: bool = False,
    ) -> dict[str, Any]:
        server_id = validate_identifier(server_id, field="server_id")
        gateway = self._gateway_factory(self._servers.connection(server_id))
        queue = gateway.get_queue()
        running = _queue_prompt_ids({"queue_running": queue.get("queue_running", [])})
        affected = self._affected(server_id, running, owner_id, allow_cross_owner)
        if execute:
            if not allow_cross_owner:
                raise PermissionError("Global interrupt requires management permission")
            gateway.interrupt()
        return {
            "operation": "server.interrupt",
            "server_id": server_id,
            "executed": execute,
            "affected_jobs": [_job_summary(job) for job in affected],
            "affected_prompt_ids": running,
        }

    def _controller_for(self, server_id: str) -> RuntimeController | None:
        if self._controller_provider is not None:
            return self._controller_provider(server_id)
        return self._controller

    def restart_plan(self, server_id: str, owner_id: str) -> dict[str, Any]:
        server_id = validate_identifier(server_id, field="server_id")
        self._servers.connection(server_id)
        try:
            active = self._runs.list_jobs(
                owner_id,
                limit=100,
                status="",
                workflow_id="",
                server_id=server_id,
                created_after="",
                after_created_at="",
                after_job_id="",
            )
            impact_coverage = "owner_jobs"
        except NotImplementedError:
            active = []
            impact_coverage = "unavailable"
        affected = [row for row in active if row.get("status") not in _TERMINAL]
        payload = {
            "server_id": server_id,
            "owner_id": owner_id,
            "affected_jobs": affected,
            "impact_coverage": impact_coverage,
            "approval_required": True,
            "operation_requirement": (
                "Global impact enumeration and management approval are required before restart"
            ),
        }
        digest = hashlib.sha256(_canonical_json(payload).encode()).hexdigest()
        return {
            **payload,
            "plan_id": "runtime_plan_" + digest,
            "plan_digest": digest,
            "resource_uri": "comfyui://plans/runtime_plan_" + digest,
            "status": "operation_required",
            "runtime_controller_available": self._controller_for(server_id) is not None,
        }

    def _affected(
        self,
        server_id: str,
        prompt_ids: list[str],
        owner_id: str,
        allow_cross_owner: bool,
    ) -> list[Job]:
        affected: list[Job] = []
        for prompt_id in prompt_ids:
            job = self._runs.get(server_id, prompt_id)
            if job is None:
                if not allow_cross_owner:
                    raise PermissionError("Queue contains an unowned or unknown Job")
                continue
            if job.owner_id != owner_id and not allow_cross_owner:
                raise PermissionError(
                    "Cross-owner runtime operation requires management permission"
                )
            affected.append(job)
        return affected


_TERMINAL = frozenset({"completed", "error", "interrupted", "cancelled", "lost"})


def _prompt_ids(value: object) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 100:
        raise ValueError("prompt_ids must contain between 1 and 100 values")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 256 or item in result:
            raise ValueError("prompt_ids must contain unique bounded strings")
        result.append(item)
    return result


def _queue_prompt_ids(queue: object) -> list[str]:
    if not isinstance(queue, dict):
        raise ValueError("ComfyUI queue response is invalid")
    result: list[str] = []
    for name in ("queue_running", "queue_pending"):
        entries = queue.get(name, [])
        if not isinstance(entries, list):
            raise ValueError("ComfyUI queue response is invalid")
        for entry in entries:
            prompt_id = ""
            if isinstance(entry, (list, tuple)) and len(entry) > 1:
                prompt_id = str(entry[1])
            elif isinstance(entry, dict):
                prompt_id = str(entry.get("prompt_id", ""))
            if prompt_id and prompt_id not in result:
                result.append(prompt_id)
    return result


def _job_summary(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "prompt_id": job.prompt_id,
        "owner_id": job.owner_id,
        "status": job.status,
        "job_uri": canonical_resource_uri("job", job.job_id),
    }


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


__all__ = ["RuntimeController", "RuntimeControlService"]
