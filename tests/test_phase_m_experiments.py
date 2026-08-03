"""Phase M Experiment domain and application-service behavior contracts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

import pytest

from comfyui_mcp_skills.application.experiments import ExperimentService, get_experiment_variant
from comfyui_mcp_skills.domain.errors import (
    ExperimentInvalidRequest,
    ExperimentNotFound,
    ExperimentPlanConflict,
)

_NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


class MemoryExperimentRepository:
    def __init__(self) -> None:
        self.saved_plans: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        self.plans: dict[str, dict[str, Any]] = {}
        self.experiments: dict[str, dict[str, Any]] = {}
        self.variants: dict[str, list[dict[str, Any]]] = {}
        self.cancel_calls: list[tuple[str, str, str]] = []
        self.ratings: dict[str, dict[str, Any]] = {}
        self.promotions: dict[tuple[str, str], dict[str, Any]] = {}

    def save_plan(self, plan: dict[str, Any], variants: Sequence[dict[str, Any]]) -> dict[str, Any]:
        compact = [dict(variant) for variant in variants]
        self.saved_plans.append((dict(plan), compact))
        self.plans[plan["plan_id"]] = dict(plan)
        self.variants[plan["experiment_id"]] = [
            {
                **variant,
                "variant_id": "variant_" + f"{int(variant['ordinal']):064x}",
                "experiment_id": plan["experiment_id"],
                "owner_id": plan["owner_id"],
                "status": "pending",
                "job_id": "",
                "created_at": plan["created_at"],
                "updated_at": plan["created_at"],
            }
            for variant in compact
        ]
        return dict(plan)

    def resolve_planning_context(
        self, _owner_id: str, _workflow_id: str, _server_id: str
    ) -> dict[str, Any]:
        return {
            "revision_id": "revision_" + "a" * 64,
            "deployment_id": "deployment_" + "b" * 64,
            "content_digest": "c" * 64,
            "parameter_schema": {
                "parameters": {
                    "width": {"type": "integer", "role": "width", "default": 64},
                    "height": {"type": "integer", "role": "height", "default": 64},
                    "batch_size": {"type": "integer", "role": "batch_size", "default": 1},
                    "seed": {"type": "integer", "role": "seed"},
                    "steps": {"type": "integer", "role": "steps"},
                    "cfg": {"type": "number", "role": "guidance"},
                    "prompt": {"type": "string", "role": "prompt"},
                    "host_path": {"type": "string", "role": "metadata"},
                    "estimated_seconds": {"type": "number", "role": "hint"},
                },
                "_output_contract": {
                    "version": 1,
                    "coverage": "complete",
                    "outputs": [{"node_id": "1"}],
                },
            },
            "output_cardinality": 1,
            "trusted_seconds_per_run": 3.0,
            "execution_slots": 1,
            "subject_submission_quota": 10_000,
        }

    def commit_plan(
        self,
        plan_id: str,
        plan_digest: str,
        owner_id: str,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        assert now == _NOW
        plan = self.plans[plan_id]
        if plan["plan_digest"] != plan_digest or plan["owner_id"] != owner_id:
            raise ValueError("plan identity conflict")
        experiment = self.experiments.setdefault(
            plan["experiment_id"],
            {
                **plan,
                "status": "queued",
                "pending_count": plan["variant_count"],
                "submitted_count": 0,
                "running_count": 0,
                "completed_count": 0,
                "failed_count": 0,
                "cancelled_count": 0,
                "lost_count": 0,
                "cancel_mode": "",
                "updated_at": plan["created_at"],
            },
        )
        return dict(experiment)

    def get_experiment(self, experiment_id: str, owner_id: str) -> dict[str, Any] | None:
        experiment = self.experiments.get(experiment_id)
        if experiment is None or experiment["owner_id"] != owner_id:
            return None
        return dict(experiment)

    def cancel_experiment(
        self, experiment_id: str, mode: str, owner_id: str
    ) -> dict[str, Any] | None:
        experiment = self.experiments.get(experiment_id)
        if experiment is None or experiment["owner_id"] != owner_id:
            return None
        self.cancel_calls.append((experiment_id, mode, owner_id))
        experiment.update(
            status="cancelled",
            cancel_mode=mode,
            pending_count=0,
            cancelled_count=experiment["variant_count"],
        )
        return dict(experiment)

    def list_variants(
        self,
        experiment_id: str,
        owner_id: str,
        *,
        limit: int,
        after: tuple[str, str] | None,
    ) -> tuple[list[dict[str, Any]], bool]:
        experiment = self.experiments.get(experiment_id)
        if experiment is None or experiment["owner_id"] != owner_id:
            return [], False
        variants = sorted(
            self.variants[experiment_id],
            key=lambda item: (item["created_at"], item["variant_id"]),
        )
        if after is not None:
            variants = [
                variant
                for variant in variants
                if (variant["created_at"], variant["variant_id"]) > after
            ]
        return [dict(variant) for variant in variants[:limit]], len(variants) > limit

    def get_variant(
        self, experiment_id: str, variant_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        experiment = self.experiments.get(experiment_id)
        if experiment is None or experiment["owner_id"] != owner_id:
            return None
        return next(
            (
                dict(variant)
                for variant in self.variants[experiment_id]
                if variant["variant_id"] == variant_id
            ),
            None,
        )

    def save_rating(self, rating: dict[str, Any]) -> dict[str, Any]:
        self.ratings[rating["rating_id"]] = dict(rating)
        for variant in self.variants[rating["experiment_id"]]:
            if variant["variant_id"] == rating["variant_id"]:
                variant["rating"] = dict(rating)
                break
        return dict(rating)

    def promote_variant(
        self, experiment_id: str, variant_id: str, target: str, owner_id: str
    ) -> dict[str, Any]:
        promotion = {
            "promotion_id": f"promotion_{target}_{variant_id}",
            "experiment_id": experiment_id,
            "variant_id": variant_id,
            "target": target,
            "created_at": _NOW.isoformat(),
        }
        if target == "preset":
            promotion["preset_id"] = f"preset_{variant_id}"
        else:
            promotion["revision_id"] = f"revision_{variant_id}"
            promotion["published"] = False
        self.promotions[(variant_id, target)] = promotion
        for variant in self.variants[experiment_id]:
            if variant["variant_id"] == variant_id:
                variant["promotion"] = dict(promotion)
                break
        return dict(promotion)


def _budgets(**overrides: int | float) -> dict[str, int | float]:
    result: dict[str, int | float] = {
        "max_variants": 100,
        "max_concurrency": 4,
        "max_pixels": 100_000_000,
        "max_outputs": 100,
        "max_seconds": 10_000,
    }
    result.update(overrides)
    return result


def test_matrix_plan_expands_cartesian_product_and_calculates_exact_budgets() -> None:
    repository = MemoryExperimentRepository()
    service = ExperimentService(repository, clock=lambda: _NOW)

    result = service.plan(
        "owner-a",
        "portrait",
        "local",
        {
            "mode": "matrix",
            "parameters": {"seed": [1, 2], "steps": [10, 20]},
        },
        {
            "width": 64,
            "height": 32,
            "batch_size": 2,
            "estimated_seconds": 3,
        },
        _budgets(
            max_variants=4,
            max_concurrency=2,
            max_pixels=16_384,
            max_outputs=8,
            max_seconds=12,
        ),
        "continue",
        1,
        1,
    )

    assert result["variant_count"] == 4
    assert result["budget_totals"] == {
        "variants": 4,
        "concurrency": 1,
        "pixels": 16_384,
        "outputs": 8,
        "seconds": 12.0,
    }
    assert "variants" not in result
    assert "base_arguments" not in result
    assert [variant["overrides"] for variant in repository.saved_plans[0][1]] == [
        {"seed": 1, "steps": 10},
        {"seed": 1, "steps": 20},
        {"seed": 2, "steps": 10},
        {"seed": 2, "steps": 20},
    ]


def test_zip_plan_pairs_equal_length_parameter_lists() -> None:
    repository = MemoryExperimentRepository()
    service = ExperimentService(repository, clock=lambda: _NOW)

    result = service.plan(
        "owner-a",
        "portrait",
        "local",
        {
            "mode": "zip",
            "parameters": {"seed": [7, 8], "prompt": ["warm", "cool"]},
        },
        {"cfg": 6},
        _budgets(),
        "stop_new",
        1,
        0,
    )

    assert result["variant_count"] == 2
    assert [variant["overrides"] for variant in repository.saved_plans[0][1]] == [
        {"prompt": "warm", "seed": 7},
        {"prompt": "cool", "seed": 8},
    ]


def test_zip_rejects_unequal_lengths_before_saving() -> None:
    repository = MemoryExperimentRepository()
    service = ExperimentService(repository, clock=lambda: _NOW)

    with pytest.raises(ValueError, match="equal lengths"):
        service.plan(
            "owner-a",
            "portrait",
            "local",
            {"mode": "zip", "parameters": {"seed": [1, 2], "steps": [10]}},
            {},
            _budgets(),
            "continue",
            1,
            0,
        )

    assert repository.saved_plans == []


def test_sample_is_seeded_deterministic_bounded_and_without_replacement() -> None:
    expansion = {
        "mode": "sample",
        "parameters": {"seed": [1, 2, 3], "steps": [10, 20]},
        "seed": 17,
        "count": 4,
    }
    first_repository = MemoryExperimentRepository()
    second_repository = MemoryExperimentRepository()
    different_repository = MemoryExperimentRepository()

    for repository, selected in (
        (first_repository, expansion),
        (second_repository, expansion),
        (different_repository, {**expansion, "seed": 18}),
    ):
        ExperimentService(repository, clock=lambda: _NOW).plan(
            "owner-a",
            "portrait",
            "local",
            selected,
            {"cfg": 7},
            _budgets(),
            "continue",
            1,
            0,
        )

    first = [variant["overrides"] for variant in first_repository.saved_plans[0][1]]
    second = [variant["overrides"] for variant in second_repository.saved_plans[0][1]]
    different = [variant["overrides"] for variant in different_repository.saved_plans[0][1]]
    assert first == second
    assert first != different
    assert (
        len(first)
        == len({variant["parameter_digest"] for variant in first_repository.saved_plans[0][1]})
        == 4
    )


def test_explicit_plan_preserves_list_order_and_merges_base_arguments() -> None:
    repository = MemoryExperimentRepository()
    service = ExperimentService(repository, clock=lambda: _NOW)

    result = service.plan(
        "owner-a",
        "portrait",
        "local",
        {
            "mode": "explicit",
            "variants": [
                {"seed": 9, "prompt": "first"},
                {"seed": 4, "prompt": "second"},
            ],
        },
        {"steps": 20},
        _budgets(),
        "cancel_queued",
        1,
        2,
    )

    assert result["expansion"] == {"mode": "explicit"}
    assert [variant["overrides"] for variant in repository.saved_plans[0][1]] == [
        {"prompt": "first", "seed": 9},
        {"prompt": "second", "seed": 4},
    ]


def test_commit_is_bound_to_immutable_plan_digest_and_owner() -> None:
    repository = MemoryExperimentRepository()
    service = ExperimentService(repository, clock=lambda: _NOW)
    plan = service.plan(
        "owner-a",
        "portrait",
        "local",
        {"mode": "explicit", "variants": [{"seed": 9}]},
        {},
        _budgets(),
        "continue",
        1,
        0,
    )

    committed = service.commit(plan["plan_id"], plan["plan_digest"], "owner-a")

    assert committed["experiment_id"] == plan["experiment_id"]
    assert committed["status"] == "queued"
    assert committed["resource_uri"] == f"comfyui://experiments/{plan['experiment_id']}"
    assert "owner_id" not in committed
    assert "base_arguments" not in committed
    with pytest.raises(ExperimentPlanConflict):
        service.commit(plan["plan_id"], "0" * 64, "owner-a")


def test_get_and_cancel_are_owner_bound_and_return_stable_public_projections() -> None:
    repository = MemoryExperimentRepository()
    service = ExperimentService(repository, clock=lambda: _NOW)
    plan = service.plan(
        "owner-a",
        "portrait",
        "local",
        {"mode": "explicit", "variants": [{"seed": 9}]},
        {"host_path": "C:/private/workflow.json"},
        _budgets(),
        "continue",
        1,
        0,
    )
    service.commit(plan["plan_id"], plan["plan_digest"], "owner-a")

    fetched = service.get(plan["experiment_id"], "owner-a")
    cancelled = service.cancel(plan["experiment_id"], "cancel_queued", "owner-a")

    assert fetched["status"] == "queued"
    assert "host_path" not in repr(fetched)
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_mode"] == "cancel_queued"
    with pytest.raises(ExperimentNotFound):
        service.get(plan["experiment_id"], "owner-b")
    with pytest.raises(ValueError, match="mode"):
        service.cancel(plan["experiment_id"], "continue", "owner-a")
    assert repository.cancel_calls == [(plan["experiment_id"], "cancel_queued", "owner-a")]


def test_list_variants_is_bounded_keyset_paginated_and_never_exposes_arguments() -> None:
    repository = MemoryExperimentRepository()
    service = ExperimentService(repository, clock=lambda: _NOW)
    plan = service.plan(
        "owner-a",
        "portrait",
        "local",
        {
            "mode": "explicit",
            "variants": [{"seed": 1}, {"seed": 2}, {"seed": 3}],
        },
        {"host_path": "C:/private/input.png"},
        _budgets(),
        "continue",
        1,
        0,
    )
    service.commit(plan["plan_id"], plan["plan_digest"], "owner-a")

    first = service.list_variants(plan["experiment_id"], "owner-a", 2, None)
    second = service.list_variants(plan["experiment_id"], "owner-a", 2, first["next_cursor"])

    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert first["next_cursor"]
    assert len({item["variant_id"] for item in first["items"] + second["items"]}) == 3
    assert second["next_cursor"] == ""
    assert "host_path" not in repr(first["items"] + second["items"])
    with pytest.raises(ValueError, match="limit"):
        service.list_variants(plan["experiment_id"], "owner-a", 101, None)
    with pytest.raises(ExperimentNotFound):
        service.list_variants(plan["experiment_id"], "owner-b", 2, None)


def test_exact_variant_resource_helper_is_owner_bound_bounded_and_safely_projected() -> None:
    repository = MemoryExperimentRepository()
    service = ExperimentService(repository, clock=lambda: _NOW)
    plan = service.plan(
        "owner-a",
        "portrait",
        "local",
        {"mode": "explicit", "variants": [{"seed": 1}]},
        {"host_path": "C:/private/input.png"},
        _budgets(),
        "continue",
        1,
        0,
    )
    service.commit(plan["plan_id"], plan["plan_digest"], "owner-a")
    variant_id = repository.variants[plan["experiment_id"]][0]["variant_id"]

    variant = get_experiment_variant(service, plan["experiment_id"], variant_id, "owner-a")

    assert variant["variant_id"] == variant_id
    assert variant["resource_uri"].endswith(f"/variants/{variant_id}")
    assert "arguments" not in variant
    with pytest.raises(ExperimentNotFound):
        get_experiment_variant(service, plan["experiment_id"], variant_id, "owner-b")


def test_rate_validates_immutable_versioned_rubric_dimensions_and_bounds() -> None:
    repository = MemoryExperimentRepository()
    dimensions = {"composition": (0.0, 5.0), "detail": (0.0, 1.0)}
    service = ExperimentService(
        repository,
        rubrics={"aesthetic-v2": dimensions},
        clock=lambda: _NOW,
    )
    plan = service.plan(
        "owner-a",
        "portrait",
        "local",
        {"mode": "explicit", "variants": [{"seed": 1}]},
        {},
        _budgets(),
        "continue",
        1,
        0,
    )
    service.commit(plan["plan_id"], plan["plan_digest"], "owner-a")
    variant_id = repository.variants[plan["experiment_id"]][0]["variant_id"]
    dimensions["composition"] = (0.0, 10.0)

    rating = service.rate(
        plan["experiment_id"],
        variant_id,
        "aesthetic-v2",
        {"composition": 4.5, "detail": 1},
        "owner-a",
    )

    assert rating["scores"] == {"composition": 4.5, "detail": 1.0}
    stored_rating = next(iter(repository.ratings.values()))
    assert stored_rating["rubric_definition"] == {
        "version": "aesthetic-v2",
        "dimensions": {
            "composition": {"minimum": 0.0, "maximum": 5.0},
            "detail": {"minimum": 0.0, "maximum": 1.0},
        },
    }
    assert "owner_id" not in rating
    rated_variant = get_experiment_variant(service, plan["experiment_id"], variant_id, "owner-a")
    assert rated_variant["ratings"] == [rating]
    with pytest.raises(ExperimentInvalidRequest, match="dimensions"):
        service.rate(
            plan["experiment_id"],
            variant_id,
            "aesthetic-v2",
            {"composition": 4, "unknown": 1},
            "owner-a",
        )
    with pytest.raises(ExperimentInvalidRequest, match="composition"):
        service.rate(
            plan["experiment_id"],
            variant_id,
            "aesthetic-v2",
            {"composition": 6, "detail": 1},
            "owner-a",
        )
    assert len(repository.ratings) == 1


def test_promote_only_accepts_completed_variants_and_creates_unpublished_revision() -> None:
    repository = MemoryExperimentRepository()
    service = ExperimentService(repository, clock=lambda: _NOW)
    plan = service.plan(
        "owner-a",
        "portrait",
        "local",
        {"mode": "explicit", "variants": [{"seed": 1}]},
        {},
        _budgets(),
        "continue",
        1,
        0,
    )
    service.commit(plan["plan_id"], plan["plan_digest"], "owner-a")
    variant = repository.variants[plan["experiment_id"]][0]

    with pytest.raises(ExperimentInvalidRequest, match="completed"):
        service.promote(plan["experiment_id"], variant["variant_id"], "revision", "owner-a")
    variant["status"] = "completed"
    promoted = service.promote(plan["experiment_id"], variant["variant_id"], "revision", "owner-a")

    assert promoted["target"] == "revision"
    assert promoted["revision_id"].startswith("revision_")
    assert promoted["published"] is False
    assert "owner_id" not in promoted
    preset = service.promote(plan["experiment_id"], variant["variant_id"], "preset", "owner-a")
    assert preset["preset_id"].startswith("preset_")
    promoted_variant = get_experiment_variant(
        service, plan["experiment_id"], variant["variant_id"], "owner-a"
    )
    assert promoted_variant["promotions"] == [preset]
    with pytest.raises(ValueError, match="target"):
        service.promote(plan["experiment_id"], variant["variant_id"], "artifact", "owner-a")


def test_plan_rejects_unbounded_max_variants_before_saving() -> None:
    repository = MemoryExperimentRepository()
    service = ExperimentService(repository, clock=lambda: _NOW)

    with pytest.raises(ValueError, match="max_variants"):
        service.plan(
            "owner-a",
            "portrait",
            "local",
            {"mode": "explicit", "variants": [{"seed": 1}]},
            {},
            _budgets(max_variants=10_001),
            "continue",
            1,
            0,
        )

    assert repository.saved_plans == []


def test_budget_and_failure_policy_violations_are_rejected_before_saving() -> None:
    repository = MemoryExperimentRepository()
    service = ExperimentService(repository, clock=lambda: _NOW)

    with pytest.raises(ValueError, match="pixels budget exceeded"):
        service.plan(
            "owner-a",
            "portrait",
            "local",
            {"mode": "explicit", "variants": [{"seed": 1}]},
            {"width": 4, "height": 4},
            _budgets(max_pixels=15),
            "continue",
            1,
            0,
        )
    with pytest.raises(ValueError, match="execution slots"):
        service.plan(
            "owner-a",
            "portrait",
            "local",
            {"mode": "explicit", "variants": [{"seed": 1}]},
            {},
            _budgets(max_concurrency=1),
            "continue",
            2,
            0,
        )
    with pytest.raises(ValueError, match="failure_policy"):
        service.plan(
            "owner-a",
            "portrait",
            "local",
            {"mode": "explicit", "variants": [{"seed": 1}]},
            {},
            _budgets(),
            "best_effort",
            1,
            0,
        )

    assert repository.saved_plans == []


class PinnedPlanningRepository(MemoryExperimentRepository):
    def __init__(self, context: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.context = context or {
            "revision_id": "revision_" + "a" * 64,
            "deployment_id": "deployment_" + "b" * 64,
            "content_digest": "c" * 64,
            "parameter_schema": {
                "parameters": {
                    "width": {"type": "integer", "role": "width", "default": 1024},
                    "height": {"type": "integer", "role": "height", "default": 1024},
                    "batch_size": {"type": "integer", "role": "batch_size", "default": 1},
                    "seed": {"type": "integer", "role": "seed"},
                    "prompt": {"type": "string", "role": "prompt"},
                    "host_path": {"type": "string", "role": "metadata"},
                },
                "_output_contract": {
                    "version": 1,
                    "coverage": "complete",
                    "outputs": [{"node_id": "1"}, {"node_id": "2"}],
                },
            },
            "output_cardinality": 2,
            "trusted_seconds_per_run": 7.0,
            "execution_slots": 1,
            "subject_submission_quota": 3,
        }

    def resolve_planning_context(
        self, _owner_id: str, _workflow_id: str, _server_id: str
    ) -> dict[str, Any]:
        return dict(self.context)


def test_plan_resolves_schema_defaults_and_pinned_semantic_costs() -> None:
    repository = PinnedPlanningRepository()
    result = ExperimentService(repository, clock=lambda: _NOW).plan(
        "owner-a",
        "portrait",
        "local",
        {"mode": "explicit", "variants": [{"seed": 1}]},
        {},
        _budgets(
            max_variants=1,
            max_concurrency=1,
            max_pixels=1_048_576,
            max_outputs=2,
            max_seconds=7,
        ),
        "continue",
        1,
        0,
    )

    assert result["budget_totals"] == {
        "variants": 1,
        "concurrency": 1,
        "pixels": 1_048_576,
        "outputs": 2,
        "seconds": 7.0,
    }
    saved_plan, variants = repository.saved_plans[0]
    assert saved_plan["pinned_revision_id"].startswith("revision_")
    assert saved_plan["pinned_deployment_id"].startswith("deployment_")
    assert saved_plan["pinned_content_digest"] == "c" * 64
    assert saved_plan["execution_slots"] == 1
    assert saved_plan["subject_submission_quota"] == 3
    assert saved_plan["output_cardinality"] == 2
    assert saved_plan["trusted_seconds_per_run"] == 7.0
    assert variants[0]["overrides"] == {"seed": 1}
    assert "arguments" not in variants[0]
    assert result["expires_at"]
    assert result["retained_plan_bytes"] > 0


def test_plan_digest_changes_with_pinned_deployment_and_capacity_facts() -> None:
    first_repository = PinnedPlanningRepository()
    second_repository = PinnedPlanningRepository()
    second_repository.context["deployment_id"] = "deployment_" + "d" * 64
    second_repository.context["subject_submission_quota"] = 4

    plans = [
        ExperimentService(repository, clock=lambda: _NOW).plan(
            "owner-a",
            "portrait",
            "local",
            {"mode": "explicit", "variants": [{"seed": 1}]},
            {},
            _budgets(max_variants=1, max_concurrency=1),
            "continue",
            1,
            0,
        )
        for repository in (first_repository, second_repository)
    ]

    assert plans[0]["plan_digest"] != plans[1]["plan_digest"]


def test_plan_rejects_unknown_trusted_runtime_before_save() -> None:
    repository = PinnedPlanningRepository()
    repository.context["trusted_seconds_per_run"] = None

    with pytest.raises(ValueError, match="trusted"):
        ExperimentService(repository, clock=lambda: _NOW).plan(
            "owner-a",
            "portrait",
            "local",
            {"mode": "explicit", "variants": [{"seed": 1}]},
            {},
            _budgets(max_variants=1, max_concurrency=1),
            "continue",
            1,
            0,
        )
    assert repository.saved_plans == []


def test_plan_rejects_single_core_concurrency_mismatch_and_nested_budget_bypass() -> None:
    repository = PinnedPlanningRepository()
    service = ExperimentService(repository, clock=lambda: _NOW)

    with pytest.raises(ValueError, match="execution slots"):
        service.plan(
            "owner-a",
            "portrait",
            "local",
            {"mode": "explicit", "variants": [{"seed": 1}]},
            {},
            _budgets(max_concurrency=64),
            "continue",
            2,
            0,
        )
    with pytest.raises(ValueError, match="max_pixels"):
        service.plan(
            "owner-a",
            "portrait",
            "local",
            {"mode": "explicit", "variants": [{"seed": 1}]},
            {},
            _budgets(max_concurrency=1, max_pixels=10**15 + 1),
            "continue",
            1,
            0,
        )
    with pytest.raises(ValueError, match="max_seconds"):
        service.plan(
            "owner-a",
            "portrait",
            "local",
            {"mode": "explicit", "variants": [{"seed": 1}]},
            {},
            _budgets(max_concurrency=1, max_seconds=10**1000),
            "continue",
            1,
            0,
        )
    assert repository.saved_plans == []


def test_plan_rejects_invalid_expanded_variant_and_retained_bytes_before_save() -> None:
    repository = PinnedPlanningRepository()
    service = ExperimentService(repository, clock=lambda: _NOW)

    with pytest.raises(ValueError, match="schema"):
        service.plan(
            "owner-a",
            "portrait",
            "local",
            {"mode": "explicit", "variants": [{"seed": "not-an-integer"}]},
            {},
            _budgets(max_variants=1, max_concurrency=1),
            "continue",
            1,
            0,
        )
    huge = "x" * (9 * 1024 * 1024)
    with pytest.raises(ValueError, match="retained"):
        service.plan(
            "owner-a",
            "portrait",
            "local",
            {"mode": "explicit", "variants": [{"seed": 1}]},
            {"prompt": huge},
            _budgets(max_variants=1, max_concurrency=1),
            "continue",
            1,
            0,
        )
    assert repository.saved_plans == []


@pytest.mark.parametrize("missing_fact", ["pixels", "outputs"])
def test_plan_rejects_unknown_pinned_cost_facts(missing_fact: str) -> None:
    repository = PinnedPlanningRepository()
    if missing_fact == "pixels":
        parameters = repository.context["parameter_schema"]["parameters"]
        parameters["width"] = {"type": "integer", "default": 1024}
    else:
        repository.context["output_cardinality"] = None

    with pytest.raises(ValueError, match="unknown|output_cardinality"):
        ExperimentService(repository, clock=lambda: _NOW).plan(
            "owner-a",
            "portrait",
            "local",
            {"mode": "explicit", "variants": [{"seed": 1}]},
            {},
            _budgets(max_variants=1, max_concurrency=1),
            "continue",
            1,
            0,
        )
    assert repository.saved_plans == []
