"""Plan and manage durable, owner-bound Experiments."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from comfyui_mcp_skills.application.experiment_ports import ExperimentRepository
from comfyui_mcp_skills.application.experiment_projections import (
    _public_experiment,
    _public_plan,
    _public_promotion,
    _public_rating,
    _public_variant,
)
from comfyui_mcp_skills.application.pagination import decode_keyset_cursor, encode_keyset_cursor
from comfyui_mcp_skills.domain.errors import (
    ExperimentInvalidRequest,
    ExperimentNotFound,
    ExperimentPlanConflict,
)
from comfyui_mcp_skills.domain.experiments import (
    canonical_digest,
    enforce_budgets,
    expand_variants,
    normalize_rubrics,
    resolve_variants,
    validate_budgets,
    validate_cancel_mode,
    validate_failure_policy,
    validate_rating_scores,
)
from comfyui_mcp_skills.domain.identifiers import validate_identifier

_MAX_RETAINED_PLAN_BYTES = 8 * 1024 * 1024
_PLAN_TTL = timedelta(hours=1)


class ExperimentService:
    def __init__(
        self,
        repository: ExperimentRepository,
        *,
        rubrics: Mapping[str, Mapping[str, tuple[float, float]]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rubrics = normalize_rubrics(rubrics)

    def plan(
        self,
        owner_id: str,
        workflow_id: str,
        server_id: str,
        expansion: dict[str, Any],
        base_arguments: dict[str, Any],
        budgets: dict[str, Any],
        failure_policy: str,
        concurrency: int,
        submission_window: int,
    ) -> dict[str, Any]:
        owner_id = _owner(owner_id)
        workflow_id = validate_identifier(workflow_id, field="workflow_id")
        server_id = validate_identifier(server_id, field="server_id")
        normalized_budgets = validate_budgets(budgets)
        policy = validate_failure_policy(failure_policy)
        concurrency = _positive_integer(concurrency, "concurrency")
        submission_window = _non_negative_integer(submission_window, "submission_window")
        if submission_window > 10_000:
            raise ValueError("submission_window must not exceed 10000")
        try:
            context = self._repository.resolve_planning_context(owner_id, workflow_id, server_id)
        except LookupError as exc:
            raise ExperimentInvalidRequest("Published Workflow deployment is unavailable") from exc
        if not isinstance(context, Mapping):
            raise ExperimentInvalidRequest("Repository planning context is invalid")
        slots = context.get("execution_slots")
        quota = context.get("subject_submission_quota")
        if concurrency != slots:
            raise ValueError("concurrency must match pinned server execution slots")
        if not isinstance(quota, int) or isinstance(quota, bool) or submission_window > quota:
            raise ValueError("submission_window exceeds subject submission quota")
        expanded = expand_variants(
            expansion,
            base_arguments,
            max_variants=int(normalized_budgets["max_variants"]),
        )
        if _compact_request_bytes(base_arguments, expanded.arguments) > _MAX_RETAINED_PLAN_BYTES:
            raise ValueError("retained plan bytes exceed 8 MiB")
        resolved = resolve_variants(expanded, base_arguments, context)
        totals = resolved.totals(concurrency=concurrency)
        enforce_budgets(totals, normalized_budgets)
        now = self._clock()
        created_at = _time(now)
        expires_at = _time(_add_time(now, _PLAN_TTL))
        compact_variants = [
            {
                "ordinal": ordinal,
                "overrides": item.overrides,
                "parameter_digest": item.parameter_digest,
            }
            for ordinal, item in enumerate(resolved.variants)
        ]
        retained_bytes = _retained_bytes(resolved.base_arguments, compact_variants)
        if retained_bytes > _MAX_RETAINED_PLAN_BYTES:
            raise ValueError("retained plan bytes exceed 8 MiB")
        immutable = {
            "owner_id": owner_id,
            "workflow_id": workflow_id,
            "server_id": server_id,
            "expansion": {"mode": expanded.mode},
            "base_arguments": resolved.base_arguments,
            "budgets": normalized_budgets,
            "budget_totals": totals.to_dict(),
            "failure_policy": policy,
            "concurrency": concurrency,
            "submission_window": submission_window,
            "pinned_revision_id": str(context["revision_id"]),
            "output_cardinality": int(context["output_cardinality"]),
            "trusted_seconds_per_run": float(context["trusted_seconds_per_run"]),
            "pinned_deployment_id": str(context["deployment_id"]),
            "pinned_content_digest": str(context["content_digest"]),
            "execution_slots": int(context["execution_slots"]),
            "subject_submission_quota": int(context["subject_submission_quota"]),
            "variant_digests_digest": canonical_digest(
                [item.parameter_digest for item in resolved.variants]
            ),
            "retained_plan_bytes": retained_bytes,
            "created_at": created_at,
            "expires_at": expires_at,
        }
        plan_digest = canonical_digest(immutable)
        plan_id = "experiment_plan_" + plan_digest
        experiment_id = "experiment_" + plan_digest
        variants = compact_variants
        plan = {
            "plan_id": plan_id,
            "plan_digest": plan_digest,
            "experiment_id": experiment_id,
            **immutable,
            "variant_count": totals.variants,
            "status": "planned",
        }
        self._repository.save_plan(plan, variants)
        return _public_plan(plan, expanded.mode)

    def commit(self, plan_id: str, plan_digest: str, owner_id: str) -> dict[str, Any]:
        owner_id = _owner(owner_id)
        plan_id = validate_identifier(plan_id, field="plan_id")
        plan_digest = _digest(plan_digest, "plan_digest")
        try:
            experiment = self._repository.commit_plan(
                plan_id, plan_digest, owner_id, now=self._clock()
            )
        except LookupError as exc:
            raise ExperimentNotFound(
                "Experiment Plan was not found",
                details={"plan_id": plan_id},
            ) from exc
        except ValueError as exc:
            raise ExperimentPlanConflict(
                "Experiment Plan digest or owner does not match",
                details={"plan_id": plan_id},
            ) from exc
        return _public_experiment(experiment)

    def get(self, experiment_id: str, owner_id: str) -> dict[str, Any]:
        experiment_id = validate_identifier(experiment_id, field="experiment_id")
        owner_id = _owner(owner_id)
        experiment = self._repository.get_experiment(experiment_id, owner_id)
        if experiment is None:
            raise ExperimentNotFound(
                "Experiment was not found",
                details={"experiment_id": experiment_id},
            )
        return _public_experiment(experiment)

    def cancel(self, experiment_id: str, mode: str, owner_id: str) -> dict[str, Any]:
        experiment_id = validate_identifier(experiment_id, field="experiment_id")
        cancel_mode = validate_cancel_mode(mode)
        owner_id = _owner(owner_id)
        experiment = self._repository.cancel_experiment(experiment_id, cancel_mode, owner_id)
        if experiment is None:
            raise ExperimentNotFound(
                "Experiment was not found",
                details={"experiment_id": experiment_id},
            )
        return _public_experiment(experiment)

    def list_variants(
        self,
        experiment_id: str,
        owner_id: str,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        experiment_id = validate_identifier(experiment_id, field="experiment_id")
        owner_id = _owner(owner_id)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must contain between 1 and 100 items")
        if self._repository.get_experiment(experiment_id, owner_id) is None:
            raise ExperimentNotFound(
                "Experiment was not found",
                details={"experiment_id": experiment_id},
            )
        filters = {"experiment_id": experiment_id, "owner_id": owner_id}
        after = decode_keyset_cursor(cursor, filters=filters) if cursor else None
        variants, has_more = self._repository.list_variants(
            experiment_id,
            owner_id,
            limit=limit,
            after=after,
        )
        items = [_public_variant(variant) for variant in variants]
        if has_more and not variants:
            raise RuntimeError("Variant repository returned an empty non-terminal page")
        next_cursor = ""
        if has_more:
            last = variants[-1]
            next_cursor = encode_keyset_cursor(
                str(last["created_at"]),
                str(last["variant_id"]),
                filters=filters,
            )
        return {"items": items, "next_cursor": next_cursor}

    def rate(
        self,
        experiment_id: str,
        variant_id: str,
        rubric_version: str,
        scores: dict[str, Any],
        owner_id: str,
    ) -> dict[str, Any]:
        experiment_id = validate_identifier(experiment_id, field="experiment_id")
        variant_id = validate_identifier(variant_id, field="variant_id")
        owner_id = _owner(owner_id)
        if self._repository.get_variant(experiment_id, variant_id, owner_id) is None:
            raise ExperimentNotFound(
                "Experiment Variant was not found",
                details={"experiment_id": experiment_id, "variant_id": variant_id},
            )
        try:
            version, normalized_scores = validate_rating_scores(
                self._rubrics, rubric_version, scores
            )
        except ValueError as exc:
            raise ExperimentInvalidRequest(str(exc)) from exc
        rating_id = "rating_" + canonical_digest(
            ["experiment-rating-v1", owner_id, experiment_id, variant_id, version]
        )
        rubric = self._rubrics[version]
        rating = {
            "rating_id": rating_id,
            "owner_id": owner_id,
            "experiment_id": experiment_id,
            "variant_id": variant_id,
            "rubric_version": version,
            "rubric_definition": {
                "version": version,
                "dimensions": {
                    name: {
                        "minimum": rubric.dimensions[name].minimum,
                        "maximum": rubric.dimensions[name].maximum,
                    }
                    for name in sorted(rubric.dimensions)
                },
            },
            "scores": normalized_scores,
            "created_at": _time(self._clock()),
        }
        try:
            stored = self._repository.save_rating(rating)
        except LookupError as exc:
            raise ExperimentNotFound(
                "Experiment Variant was not found",
                details={"experiment_id": experiment_id, "variant_id": variant_id},
            ) from exc
        return _public_rating(stored)

    def promote(
        self,
        experiment_id: str,
        variant_id: str,
        target: str,
        owner_id: str,
    ) -> dict[str, Any]:
        experiment_id = validate_identifier(experiment_id, field="experiment_id")
        variant_id = validate_identifier(variant_id, field="variant_id")
        if target not in {"preset", "revision"}:
            raise ValueError("promotion target must be preset or revision")
        owner_id = _owner(owner_id)
        variant = self._repository.get_variant(experiment_id, variant_id, owner_id)
        if variant is None:
            raise ExperimentNotFound(
                "Experiment Variant was not found",
                details={"experiment_id": experiment_id, "variant_id": variant_id},
            )
        if variant.get("status") != "completed":
            raise ExperimentInvalidRequest("only completed Experiment Variants can be promoted")
        try:
            promotion = self._repository.promote_variant(
                experiment_id, variant_id, target, owner_id
            )
        except LookupError as exc:
            raise ExperimentNotFound(
                "Experiment Variant was not found",
                details={"experiment_id": experiment_id, "variant_id": variant_id},
            ) from exc
        if target == "revision" and promotion.get("published") is not False:
            raise RuntimeError("promoted Workflow Revision must be immutable and unpublished")
        return _public_promotion(promotion)


def get_experiment_variant(
    service: ExperimentService,
    experiment_id: str,
    variant_id: str,
    owner_id: str,
) -> dict[str, Any]:
    """Read one owner-bound Variant for its canonical Resource without scanning a collection."""
    experiment_id = validate_identifier(experiment_id, field="experiment_id")
    variant_id = validate_identifier(variant_id, field="variant_id")
    owner_id = _owner(owner_id)
    variant = service._repository.get_variant(experiment_id, variant_id, owner_id)
    if variant is None:
        raise ExperimentNotFound(
            "Experiment Variant was not found",
            details={"experiment_id": experiment_id, "variant_id": variant_id},
        )
    return _public_variant(variant)


def _compact_request_bytes(
    base_arguments: dict[str, Any], overrides: tuple[dict[str, Any], ...]
) -> int:
    encoded = json.dumps(
        {"base_arguments": base_arguments, "overrides": overrides},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(encoded)


def _retained_bytes(base_arguments: dict[str, Any], variants: list[dict[str, Any]]) -> int:
    payload = {
        "base_arguments": base_arguments,
        "variants": [
            {
                "ordinal": variant["ordinal"],
                "overrides": variant["overrides"],
                "parameter_digest": variant["parameter_digest"],
            }
            for variant in variants
        ],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return len(encoded)


def _add_time(value: datetime, delta: timedelta) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Experiment clock must return a timezone-aware datetime")
    return value + delta


def _owner(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("owner_id must contain between 1 and 256 characters")
    return value


def _positive_integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _non_negative_integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _digest(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Experiment clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat()
