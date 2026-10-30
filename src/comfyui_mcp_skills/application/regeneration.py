"""Immutable Job regeneration planning and idempotent lineage submission."""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import datetime
from typing import Any

from comfyui_mcp_skills.application.control_plane_ports import ExecutionSubmitter
from comfyui_mcp_skills.application.diagnostic_ports import RetryRepository
from comfyui_mcp_skills.application.retries import RetryService, _public_plan


class RegenerationService(RetryService):
    """Replay persisted Job execution identity, regardless of its terminal status."""

    _idempotency_prefix = "regenerate"
    _operation = "regenerate"

    def __init__(
        self,
        repository: RetryRepository,
        execution: ExecutionSubmitter,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(repository, execution, clock=clock)

    def plan(
        self,
        source_job_id: str,
        owner_id: str,
        changes: dict[str, Any] | None = None,
        *,
        _overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        requested_changes = copy.deepcopy(changes) if changes is not None else {}
        if _overrides is not None:
            if not isinstance(requested_changes, dict):
                raise ValueError("changes must be a JSON object")
            requested_changes["_overrides"] = copy.deepcopy(_overrides)
        return self._plan(source_job_id, owner_id, requested_changes, require_failure=False)

    def get(self, repair_plan_id: str, owner_id: str) -> dict[str, Any]:
        plan = super().get(repair_plan_id, owner_id)
        return self._regeneration_plan(plan, owner_id)

    def commit(self, repair_plan_id: str, plan_digest: str, owner_id: str) -> dict[str, Any]:
        plan = super().commit(repair_plan_id, plan_digest, owner_id)
        return self._regeneration_plan(plan, owner_id)

    def _plan(
        self,
        job_id: str,
        owner_id: str,
        changes: dict[str, Any],
        *,
        require_failure: bool,
    ) -> dict[str, Any]:
        plan = super()._plan(job_id, owner_id, changes, require_failure=False)
        return self._regeneration_plan(plan, owner_id)

    def _regeneration_plan(self, plan: dict[str, Any], owner_id: str) -> dict[str, Any]:
        stored = self._repository.get_repair_plan(plan["repair_plan_id"], owner_id) or plan
        result = _public_plan(stored)
        result.update(
            operation="regenerate",
            source_job_id=result["original_job_id"],
            revision_id=result["pinned_revision_id"],
            deployment_id=result["pinned_deployment_id"],
            content_digest=result["pinned_content_digest"],
            original_arguments=copy.deepcopy(stored["original_arguments_snapshot"]),
            resulting_arguments=copy.deepcopy(stored["resulting_arguments"]),
        )
        resulting = result["resulting_arguments"]
        result["overrides"] = copy.deepcopy(resulting.get("_overrides", {}))
        return result
