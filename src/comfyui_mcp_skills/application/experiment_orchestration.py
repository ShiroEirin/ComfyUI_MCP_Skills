"""Fenced, resumable advancement of durable Experiments."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from comfyui_mcp_skills.application.control_plane_ports import (
    ExecutionSubmitter,
    ExperimentAdvanceRepository,
    JobCanceller,
    RunLookup,
)
from comfyui_mcp_skills.domain.errors import ExecutionInProgress, ServerOffline, UnsafeCancel
from comfyui_mcp_skills.domain.models import Job
from comfyui_mcp_skills.domain.orchestration import WorkItem, WorkLease

_IN_FLIGHT_VARIANTS = frozenset({"submitted", "running"})

# Persistent reconcile failures reach this many consecutive attempts before the
# variant is failed with a diagnosable error instead of retrying silently.
_RECONCILE_ERROR_LIMIT = 3
_JOB_TO_VARIANT = {
    "reserved": "submitted",
    "submission_unknown": "submitted",
    "submitted": "submitted",
    "queued": "submitted",
    "running": "running",
    "completed": "completed",
    "error": "failed",
    "interrupted": "failed",
    "cancelled": "cancelled",
    "lost": "lost",
}


class ExperimentAdvanceHandler:
    """Advance a bounded Experiment slice without weakening Job idempotency."""

    def __init__(
        self,
        repository: ExperimentAdvanceRepository,
        runs: RunLookup,
        execution: ExecutionSubmitter,
        jobs: JobCanceller,
        *,
        retry_delay_seconds: int = 30,
        batch_limit: int = 100,
    ) -> None:
        if retry_delay_seconds < 0 or not 1 <= batch_limit <= 100:
            raise ValueError("invalid Experiment worker bounds")
        self._repository = repository
        self._runs = runs
        self._execution = execution
        self._jobs = jobs
        self._retry_delay_seconds = retry_delay_seconds
        self._batch_limit = batch_limit

    def __call__(self, work: WorkItem, lease: WorkLease, *, now: datetime) -> None:
        experiment_id = str(work.payload["experiment_id"])
        owner_id = str(work.payload["owner_id"])
        context = self._repository.get_experiment(experiment_id, owner_id)
        if context is None:
            raise RuntimeError("leased Experiment disappeared")
        variants = self._repository.list_for_advance(
            experiment_id, owner_id, limit=self._batch_limit
        )
        counts = _counts(context)
        checkpoint = self._reconcile_accepted(
            lease, context, variants, counts, dict(work.checkpoint), owner_id, now
        )
        mode = _stop_mode(context, counts, checkpoint)
        checkpoint = self._apply_stop_mode(
            lease, context, variants, counts, checkpoint, owner_id, mode, now
        )
        concurrency = _positive_int(context.get("concurrency"), default=1)
        submission_window = _nonnegative_int(context.get("submission_window"), default=0)
        capacity = (
            0
            if mode == "pause"
            else max(
                0,
                concurrency + submission_window - counts["submitted"] - counts["running"],
            )
        )
        checkpoint = self._submit_pending(
            lease, context, variants, counts, checkpoint, owner_id, capacity, now
        )
        refreshed = self._repository.get_experiment(experiment_id, owner_id)
        if refreshed is None:
            raise RuntimeError("leased Experiment disappeared")
        refreshed_counts = _counts(refreshed)
        refreshed_mode = _stop_mode(refreshed, refreshed_counts, checkpoint)
        if refreshed_mode:
            refreshed_variants = self._repository.list_for_advance(
                experiment_id, owner_id, limit=self._batch_limit
            )
            checkpoint = self._apply_stop_mode(
                lease,
                refreshed,
                refreshed_variants,
                refreshed_counts,
                checkpoint,
                owner_id,
                refreshed_mode,
                now,
            )
            refreshed = self._repository.get_experiment(experiment_id, owner_id)
            if refreshed is None:
                raise RuntimeError("leased Experiment disappeared")
            refreshed_counts = _counts(refreshed)
        terminal = _terminal_status(refreshed, refreshed_counts)
        self._repository.finish_advance(
            lease,
            experiment_id=experiment_id,
            owner_id=owner_id,
            checkpoint=checkpoint,
            now=now,
            completed=terminal is not None,
            delay_seconds=0 if terminal is not None else self._retry_delay_seconds,
            status=terminal or "running",
        )

    def _reconcile_accepted(
        self,
        lease: WorkLease,
        context: dict[str, Any],
        variants: list[dict[str, Any]],
        counts: dict[str, int],
        checkpoint: dict[str, Any],
        owner_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        for variant in variants:
            old_status = str(variant.get("status", ""))
            if old_status not in _IN_FLIGHT_VARIANTS:
                continue
            try:
                job = self._reconcile_variant(context, variant, owner_id)
            except (ExecutionInProgress, ServerOffline):
                continue
            except Exception as exc:
                variant_id = str(variant.get("variant_id", ""))
                errors = dict(checkpoint.get("reconcile_errors") or {})
                prior = errors.get(variant_id, 0)
                errors[variant_id] = prior + 1
                checkpoint = {**checkpoint, "reconcile_errors": errors}
                if prior + 1 < _RECONCILE_ERROR_LIMIT:
                    continue
                checkpoint = self._persist_transition(
                    lease,
                    context,
                    variant,
                    counts,
                    checkpoint,
                    owner_id,
                    "failed",
                    "",
                    now,
                    event_type="EXPERIMENT_VARIANT_FAILED",
                    error_code=f"RECONCILE_ERROR:{type(exc).__name__}",
                )
                continue
            if job is None:
                continue
            status = _variant_status(job.status)
            job_id = job.job_id
            if status == old_status and (not job_id or job_id == variant.get("job_id")):
                continue
            terminal = status in {"completed", "failed", "cancelled", "lost"}
            checkpoint = self._persist_transition(
                lease,
                context,
                variant,
                counts,
                checkpoint,
                owner_id,
                status,
                job_id,
                now,
                event_type=(
                    "EXPERIMENT_VARIANT_LOST" if status == "lost" else "EXPERIMENT_VARIANT_UPDATED"
                ),
                error_code=("UPSTREAM_STATE_LOST" if job is None else job.error),
                measurements=(
                    _measurements(job, claimed_at=_claim_time(variant, checkpoint), now=now)
                    if job is not None and terminal
                    else {}
                ),
                result_links={} if job is None else _result_links(job),
            )
        return checkpoint

    def _apply_stop_mode(
        self,
        lease: WorkLease,
        context: dict[str, Any],
        variants: list[dict[str, Any]],
        counts: dict[str, int],
        checkpoint: dict[str, Any],
        owner_id: str,
        mode: str,
        now: datetime,
    ) -> dict[str, Any]:
        if mode == "cancel_queued":
            checkpoint = self._cancel_submitted(
                lease, context, variants, counts, checkpoint, owner_id, now
            )
        if not mode or mode == "pause":
            return checkpoint
        for variant in variants:
            if variant.get("status") != "pending":
                continue
            checkpoint = self._persist_transition(
                lease,
                context,
                variant,
                counts,
                checkpoint,
                owner_id,
                "cancelled",
                "",
                now,
                event_type="EXPERIMENT_VARIANT_UPDATED",
            )
        return checkpoint

    def _cancel_submitted(
        self,
        lease: WorkLease,
        context: dict[str, Any],
        variants: list[dict[str, Any]],
        counts: dict[str, int],
        checkpoint: dict[str, Any],
        owner_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        for variant in variants:
            if variant.get("status") != "submitted":
                continue
            try:
                job = self._reconcile_variant(context, variant, owner_id)
            except (ExecutionInProgress, ServerOffline):
                continue
            if job is None or not job.prompt_id:
                continue
            try:
                job = self._jobs.cancel(str(context["server_id"]), job.prompt_id, owner_id=owner_id)
            except (ServerOffline, UnsafeCancel):
                continue
            status = _variant_status(job.status)
            if status == variant.get("status"):
                continue
            terminal = status in {"completed", "failed", "cancelled", "lost"}
            checkpoint = self._persist_transition(
                lease,
                context,
                variant,
                counts,
                checkpoint,
                owner_id,
                status,
                job.job_id,
                now,
                event_type="EXPERIMENT_VARIANT_UPDATED",
                error_code=job.error,
                measurements=(
                    _measurements(job, claimed_at=_claim_time(variant, checkpoint), now=now)
                    if terminal
                    else {}
                ),
                result_links=_result_links(job),
            )
        return checkpoint

    def _submit_pending(
        self,
        lease: WorkLease,
        context: dict[str, Any],
        variants: list[dict[str, Any]],
        counts: dict[str, int],
        checkpoint: dict[str, Any],
        owner_id: str,
        capacity: int,
        now: datetime,
    ) -> dict[str, Any]:
        for variant in variants:
            if capacity <= 0 or variant.get("status") != "pending":
                continue
            claim = self._claim_submission(lease, context, variant, owner_id, now)
            if claim is None:
                continue
            variant["status"] = "submitted"
            variant["claimed_at"] = str(claim.get("claimed_at", ""))
            _record_transition(counts, "pending", "submitted")
            claimed_client = str(claim.get("client_id", ""))
            if claimed_client:
                variant["client_id"] = claimed_client
            claim_arguments = claim.get("arguments")
            if isinstance(claim_arguments, dict):
                variant["arguments"] = dict(claim_arguments)
            variant["_execution_pins"] = {
                "revision_id": str(claim.get("revision_id", "")),
                "deployment_id": str(claim.get("deployment_id", "")),
                "content_digest": str(claim.get("content_digest", "")),
            }
            error_code = ""
            measurements: dict[str, Any] = {}
            result_links: dict[str, Any] = {}
            try:
                job = self._submit_variant(context, variant, owner_id, claim=claim)
            except (ExecutionInProgress, ServerOffline):
                status, job_id = "submitted", ""
            except Exception as exc:
                accepted, accepted_job = self._accepted_submission(context, variant, owner_id)
                if accepted:
                    status = "submitted"
                    job_id = "" if accepted_job is None else accepted_job.job_id
                    error_code = ""
                else:
                    status, job_id = "failed", ""
                    error_code = type(exc).__name__
            else:
                status, job_id = _variant_status(job.status), job.job_id
                if status in {"completed", "failed", "cancelled", "lost"}:
                    measurements = _measurements(job, claimed_at=variant["claimed_at"], now=now)
                result_links = _result_links(job)
                if job.client_id:
                    variant["client_id"] = job.client_id
            checkpoint = self._persist_transition(
                lease,
                context,
                variant,
                counts,
                checkpoint,
                owner_id,
                status,
                job_id,
                now,
                event_type="EXPERIMENT_VARIANT_UPDATED",
                error_code=error_code,
                measurements=measurements,
                result_links=result_links,
            )
            if checkpoint.get("pause_reason"):
                break
            capacity -= status in _IN_FLIGHT_VARIANTS
            if status in {"failed", "lost"} and context.get("failure_policy") != "continue":
                break
        return checkpoint

    def _claim_submission(
        self,
        lease: WorkLease,
        context: dict[str, Any],
        variant: dict[str, Any],
        owner_id: str,
        now: datetime,
    ) -> dict[str, Any] | None:
        method = getattr(self._repository, "claim_variant_for_submission", None)
        if not callable(method):
            raise RuntimeError("atomic Experiment submission claims are unavailable")
        return method(
            lease,
            experiment_id=str(context["experiment_id"]),
            owner_id=owner_id,
            variant_id=str(variant["variant_id"]),
            now=now,
        )

    def _persist_transition(
        self,
        lease: WorkLease,
        context: dict[str, Any],
        variant: dict[str, Any],
        counts: dict[str, int],
        checkpoint: dict[str, Any],
        owner_id: str,
        status: str,
        job_id: str,
        now: datetime,
        *,
        event_type: str,
        error_code: str = "",
        measurements: dict[str, Any] | None = None,
        result_links: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _record_transition(counts, str(variant.get("status", "")), status)
        variant["status"] = status
        if job_id:
            variant["job_id"] = job_id
        measurements = measurements or {}
        result_links = result_links or {}
        claim_times = dict(checkpoint.get("claim_times", {}))
        claimed_at = str(variant.get("claimed_at", ""))
        if claimed_at:
            claim_times[str(variant["variant_id"])] = claimed_at
        checkpoint = {
            **checkpoint,
            "last_variant_id": str(variant["variant_id"]),
            "last_job_id": job_id,
            "last_error": error_code,
            "last_measurements": measurements,
            "last_result_links": result_links,
            "claim_times": claim_times,
        }
        self._repository.apply_transition(
            lease,
            experiment_id=str(context["experiment_id"]),
            owner_id=owner_id,
            variant_id=str(variant["variant_id"]),
            status=status,
            job_id=job_id,
            checkpoint=checkpoint,
            now=now,
            event_type=event_type,
            event_data={
                "variant_id": str(variant["variant_id"]),
                "status": status,
                "error_code": error_code,
                **measurements,
                **result_links,
            },
        )
        return checkpoint

    def _reconcile_variant(
        self, context: dict[str, Any], variant: dict[str, Any], owner_id: str
    ) -> Job | None:
        server_id = str(context["server_id"])
        key = _persisted_variant_key(context, variant)
        existing = self._runs.get_by_idempotency(server_id, key, owner_id)
        claim = self._runs.get_claim(server_id, key, owner_id)
        if existing is not None:
            return existing
        if claim is not None or variant.get("status") == "submitted":
            pins = self._execution_pins(context, variant)
            return self._execution.submit(
                server_id,
                str(context["workflow_id"]),
                dict(variant["arguments"]),
                idempotency_key=key,
                owner_id=owner_id,
                client_id=str(variant.get("client_id", "")),
                revision_id=pins[0],
                deployment_id=pins[1],
                content_digest=pins[2],
            )
        return None

    def _submit_variant(
        self,
        context: dict[str, Any],
        variant: dict[str, Any],
        owner_id: str,
        *,
        claim: dict[str, Any] | None = None,
    ) -> Job:
        if claim is not None:
            variant["_execution_pins"] = {
                "revision_id": str(claim.get("revision_id", "")),
                "deployment_id": str(claim.get("deployment_id", "")),
                "content_digest": str(claim.get("content_digest", "")),
            }
        reconciled = self._reconcile_variant(context, variant, owner_id)
        if reconciled is not None:
            return reconciled
        key = _persisted_variant_key(context, variant)
        pins = self._execution_pins(context, variant)
        return self._execution.submit(
            str(context["server_id"]),
            str(context["workflow_id"]),
            dict(variant["arguments"]),
            idempotency_key=key,
            owner_id=owner_id,
            client_id=str(variant.get("client_id", "")),
            revision_id=pins[0],
            deployment_id=pins[1],
            content_digest=pins[2],
        )

    @staticmethod
    def _execution_pins(context: dict[str, Any], variant: dict[str, Any]) -> tuple[str, str, str]:
        claim = variant.get("_execution_pins")
        if not isinstance(claim, dict):
            claim = {}
        pins = (
            str(claim.get("revision_id") or context.get("pinned_revision_id", "")),
            str(claim.get("deployment_id") or context.get("pinned_deployment_id", "")),
            str(claim.get("content_digest") or context.get("pinned_content_digest", "")),
        )
        if not all(pins):
            raise ExecutionInProgress("Experiment execution pins are unavailable")
        return pins

    def _accepted_submission(
        self, context: dict[str, Any], variant: dict[str, Any], owner_id: str
    ) -> tuple[bool, Job | None]:
        try:
            key = _persisted_variant_key(context, variant)
            job = self._runs.get_by_idempotency(str(context["server_id"]), key, owner_id)
            if job is not None:
                return True, job
            claim = self._runs.get_claim(str(context["server_id"]), key, owner_id)
            return claim is not None, None
        except Exception:
            return True, None


def _persisted_variant_key(context: dict[str, Any], variant: dict[str, Any]) -> str:
    expected = _variant_idempotency_key(str(context["experiment_id"]), str(variant["variant_id"]))
    persisted = str(variant.get("idempotency_key", ""))
    if persisted and persisted != expected:
        raise RuntimeError("persisted Variant idempotency identity conflicts")
    return persisted or expected


def _variant_idempotency_key(experiment_id: str, variant_id: str) -> str:
    return f"experiment:{experiment_id}:variant:{variant_id}"


def _variant_status(job_status: str) -> str:
    try:
        return _JOB_TO_VARIANT[job_status]
    except KeyError as exc:
        raise RuntimeError(f"unsupported Job status for Experiment Variant: {job_status}") from exc


def _positive_int(value: object, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError("persisted Experiment concurrency is invalid")
    return value


def _nonnegative_int(value: object, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("persisted Experiment submission_window is invalid")
    return value


def _counts(context: dict[str, Any]) -> dict[str, int]:
    return {
        status: max(0, int(context.get(f"{status}_count", 0)))
        for status in (
            "pending",
            "submitted",
            "running",
            "completed",
            "failed",
            "cancelled",
            "lost",
        )
    }


def _record_transition(counts: dict[str, int], old: str, new: str) -> None:
    if old in counts:
        counts[old] = max(0, counts[old] - 1)
    if new in counts:
        counts[new] += 1


def _terminal_status(context: dict[str, Any], counts: dict[str, int]) -> str | None:
    if counts["pending"] or counts["submitted"] or counts["running"]:
        return None
    if str(context.get("status", "")) == "cancelled" or context.get("cancel_mode"):
        return "cancelled"
    if counts["failed"] or counts["cancelled"] or counts["lost"]:
        return "completed_with_errors"
    return "completed"


def _stop_mode(context: dict[str, Any], counts: dict[str, int], checkpoint: dict[str, Any]) -> str:
    if checkpoint.get("pause_reason"):
        return "pause"
    if counts["lost"] and str(context.get("failure_policy", "continue")) != "continue":
        return "pause"
    if str(context.get("status", "")) == "cancelled":
        return "stop_new"
    cancel_mode = str(context.get("cancel_mode", ""))
    if cancel_mode in {"stop_new", "cancel_queued"}:
        return cancel_mode
    if not (counts["failed"] or counts["cancelled"]):
        return ""
    failure_policy = str(context.get("failure_policy", "continue"))
    return failure_policy if failure_policy in {"stop_new", "cancel_queued"} else ""


def _measurements(
    job: Job, *, claimed_at: object = None, now: datetime | None = None
) -> dict[str, Any]:
    """Extract nonnegative terminal facts; missing facts stay explicitly unavailable."""
    result: dict[str, Any] = {}
    if job.outputs:
        pixels = 0
        known_pixels = True
        outputs = 0
        for output in job.outputs:
            if not isinstance(output, dict):
                continue
            outputs += 1
            value = output.get("pixels", output.get("pixel_count"))
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                pixels += value
                continue
            width, height = output.get("width"), output.get("height")
            if (
                isinstance(width, int)
                and not isinstance(width, bool)
                and width > 0
                and isinstance(height, int)
                and not isinstance(height, bool)
                and height > 0
            ):
                pixels += width * height
            else:
                known_pixels = False
        result["measured_outputs"] = outputs
        if known_pixels:
            result["measured_pixels"] = pixels
    if claimed_at is not None and now is not None:
        claimed = _persisted_time(claimed_at)
        elapsed = (now.astimezone(claimed.tzinfo) - claimed).total_seconds()
        result["measured_seconds"] = max(0.0, elapsed)
    return result


def _persisted_time(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise RuntimeError("persisted Variant claim time is unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("persisted Variant claim time is invalid") from exc
    if parsed.tzinfo is None:
        raise RuntimeError("persisted Variant claim time is invalid")
    return parsed


def _claim_time(variant: dict[str, Any], checkpoint: dict[str, Any]) -> object:
    claimed_at = variant.get("claimed_at")
    if claimed_at:
        return claimed_at
    claim_times = checkpoint.get("claim_times", {})
    if isinstance(claim_times, dict):
        claimed_at = claim_times.get(str(variant["variant_id"]))
        if claimed_at:
            return claimed_at
    return variant.get("updated_at")


def _result_links(job: Job) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if job.job_id:
        result["result_job_uri"] = f"comfyui://jobs/{job.job_id}"
    artifact_uris = [
        str(output.get("resource_uri") or f"comfyui://artifacts/{output['artifact_id']}")
        for output in job.outputs
        if isinstance(output, dict) and (output.get("resource_uri") or output.get("artifact_id"))
    ]
    if artifact_uris:
        result["artifact_uris"] = artifact_uris
    return result
