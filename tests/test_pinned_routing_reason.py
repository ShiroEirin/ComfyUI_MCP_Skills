"""Pinned routing must fail with a pinned-specific message, not a generic one."""

from __future__ import annotations

from typing import Any

import pytest

from comfyui_mcp_skills.application.routing import RoutingService


class _EmptyRepository:
    def get_routing_plan(self, plan_id: str, owner_id: str) -> dict[str, Any] | None:
        return None

    def list_routing_contexts(self, owner_id: str, workflow_id: str) -> list[dict[str, Any]]:
        return []


class _Executor:
    def submit(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("commit must not run")


def test_pinned_routing_reports_a_pinned_specific_failure() -> None:
    service = RoutingService(_EmptyRepository(), _Executor())

    with pytest.raises(LookupError, match="Pinned Revision is not executable"):
        service.plan("owner-a", "portrait", {"steps": 20}, server_id="idle", pinned=True)

    with pytest.raises(LookupError, match="No compatible published Deployment"):
        service.plan("owner-a", "portrait", {"steps": 20})
