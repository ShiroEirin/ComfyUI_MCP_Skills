"""Stable public projections for Experiment resources and tool results."""

from __future__ import annotations

from typing import Any

from comfyui_mcp_skills.domain.experiments import json_copy


def _public_plan(plan: dict[str, Any], mode: str) -> dict[str, Any]:
    fields = (
        "plan_id",
        "plan_digest",
        "experiment_id",
        "workflow_id",
        "server_id",
        "status",
        "budgets",
        "budget_totals",
        "variant_count",
        "failure_policy",
        "concurrency",
        "submission_window",
        "pinned_revision_id",
        "pinned_deployment_id",
        "pinned_content_digest",
        "execution_slots",
        "subject_submission_quota",
        "retained_plan_bytes",
        "expires_at",
        "created_at",
    )
    result = {field: plan[field] for field in fields}
    result["expansion"] = {"mode": mode}
    result["resource_uri"] = f"comfyui://experiments/{plan['experiment_id']}"
    return result


def _public_experiment(experiment: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "experiment_id",
        "plan_id",
        "plan_digest",
        "workflow_id",
        "server_id",
        "status",
        "failure_policy",
        "concurrency",
        "submission_window",
        "pinned_revision_id",
        "pinned_deployment_id",
        "pinned_content_digest",
        "execution_slots",
        "subject_submission_quota",
        "variant_count",
        "pending_count",
        "submitted_count",
        "running_count",
        "completed_count",
        "failed_count",
        "cancelled_count",
        "lost_count",
        "cancel_mode",
        "created_at",
        "updated_at",
    )
    result = {field: experiment[field] for field in fields if field in experiment}
    experiment_id = str(experiment["experiment_id"])
    result["resource_uri"] = f"comfyui://experiments/{experiment_id}"
    return result


def _public_variant(variant: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "variant_id",
        "experiment_id",
        "ordinal",
        "parameter_digest",
        "status",
        "job_id",
        "job_uri",
        "artifact_uris",
        "measured_pixels",
        "measured_outputs",
        "measured_seconds",
        "error_code",
        "created_at",
        "updated_at",
        "completed_at",
    )
    result = {field: variant[field] for field in fields if field in variant}
    job_id = str(result.get("job_id", ""))
    if job_id and not result.get("job_uri"):
        result["job_uri"] = f"comfyui://jobs/{job_id}"
    artifacts = result.get("artifact_uris")
    if isinstance(artifacts, list):
        result["artifact_uris"] = [item for item in artifacts[:100] if isinstance(item, str)]
    ratings = variant.get("ratings")
    if isinstance(ratings, list):
        result["ratings"] = [
            _public_rating(item) for item in ratings[:32] if isinstance(item, dict)
        ]
    elif isinstance(variant.get("rating"), dict):
        result["ratings"] = [_public_rating(variant["rating"])]
        if result["ratings"]:
            result["rating"] = result["ratings"][-1]
    promotions = variant.get("promotions")
    if isinstance(promotions, list):
        result["promotions"] = [
            _public_promotion(item) for item in promotions[:2] if isinstance(item, dict)
        ]
    elif isinstance(variant.get("promotion"), dict):
        result["promotions"] = [_public_promotion(variant["promotion"])]
        if result["promotions"]:
            result["promotion"] = result["promotions"][-1]
    if result.get("ratings"):
        result["rating"] = result["ratings"][-1]
    if result.get("promotions"):
        result["promotion"] = result["promotions"][-1]
    experiment_id = str(variant["experiment_id"])
    variant_id = str(variant["variant_id"])
    result["resource_uri"] = f"comfyui://experiments/{experiment_id}/variants/{variant_id}"
    return result


def _public_rating(rating: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "rating_id",
        "experiment_id",
        "variant_id",
        "rubric_version",
        "rubric_definition",
        "scores",
        "created_at",
        "updated_at",
    )
    result = {field: rating[field] for field in fields if field in rating}
    if "scores" in result:
        result["scores"] = json_copy(result["scores"], "rating scores")
    if "rubric_definition" in result:
        result["rubric_definition"] = json_copy(result["rubric_definition"], "rubric definition")
    return result


def _public_promotion(promotion: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "promotion_id",
        "experiment_id",
        "variant_id",
        "target",
        "preset_id",
        "revision_id",
        "published",
        "created_at",
    )
    return {field: promotion[field] for field in fields if field in promotion}
