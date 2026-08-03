"""Narrow persistence boundary for durable Experiment application services."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol


class ExperimentRepository(Protocol):
    def save_plan(
        self, plan: dict[str, Any], variants: Sequence[dict[str, Any]]
    ) -> dict[str, Any]: ...

    def resolve_planning_context(
        self, owner_id: str, workflow_id: str, server_id: str
    ) -> dict[str, Any]: ...

    def commit_plan(
        self,
        plan_id: str,
        plan_digest: str,
        owner_id: str,
        *,
        now: datetime,
    ) -> dict[str, Any]: ...

    def get_experiment(self, experiment_id: str, owner_id: str) -> dict[str, Any] | None: ...

    def cancel_experiment(
        self, experiment_id: str, mode: str, owner_id: str
    ) -> dict[str, Any] | None: ...

    def list_variants(
        self,
        experiment_id: str,
        owner_id: str,
        *,
        limit: int,
        after: tuple[str, str] | None,
    ) -> tuple[list[dict[str, Any]], bool]: ...

    def get_variant(
        self, experiment_id: str, variant_id: str, owner_id: str
    ) -> dict[str, Any] | None: ...

    def save_rating(self, rating: dict[str, Any]) -> dict[str, Any]: ...

    def promote_variant(
        self, experiment_id: str, variant_id: str, target: str, owner_id: str
    ) -> dict[str, Any]: ...
