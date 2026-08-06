"""Phase M durable Experiment worker recovery and scheduling contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from comfyui_mcp_skills.application.experiment_orchestration import ExperimentAdvanceHandler
from comfyui_mcp_skills.domain.errors import ExecutionInProgress
from comfyui_mcp_skills.domain.models import Job
from comfyui_mcp_skills.domain.orchestration import WorkItem, WorkLease

_NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)
_OWNER = "principal"
_EXPERIMENT_ID = "experiment_" + "a" * 64


class _Runs:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.claims: dict[str, dict[str, Any]] = {}
        self.events: list[str] = []

    def get_by_idempotency(self, server_id: str, key: str, owner_id: str = "") -> Job | None:
        assert server_id == "local" and owner_id == _OWNER
        self.events.append("job")
        return self.jobs.get(key)

    def get_claim(self, server_id: str, key: str, owner_id: str = "") -> dict[str, Any] | None:
        assert server_id == "local" and owner_id == _OWNER
        self.events.append("claim")
        return self.claims.get(key)


class _Execution:
    def __init__(self, runs: _Runs) -> None:
        self.runs = runs
        self.queue_submissions: list[str] = []

    def submit(
        self,
        server_id: str,
        workflow_id: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str = "",
        owner_id: str = "",
        client_id: str = "",
        revision_id: str = "",
        deployment_id: str = "",
        content_digest: str = "",
    ) -> Job:
        self.runs.events.append("submit")
        assert (server_id, workflow_id, owner_id) == ("local", "portrait", _OWNER)
        del arguments, revision_id, deployment_id, content_digest
        self.queue_submissions.append(idempotency_key)
        job = Job(
            prompt_id=f"prompt-{len(self.queue_submissions)}",
            server_id=server_id,
            workflow_id=workflow_id,
            status="submitted",
            idempotency_key=idempotency_key,
            client_id=client_id or f"client-{len(self.queue_submissions)}",
            owner_id=owner_id,
            job_id="job_" + f"{len(self.queue_submissions):064x}",
        )
        self.runs.jobs[idempotency_key] = job
        return job


class _Jobs:
    def cancel(self, server_id: str, prompt_id: str, *, owner_id: str = "") -> Job:
        raise AssertionError("cancel was not expected")


class _Repository:
    def __init__(
        self,
        variants: list[dict[str, Any]],
        *,
        concurrency: int | None = 1,
        submission_window: int = 0,
        failure_policy: str = "continue",
        cancel_mode: str = "",
        measured_max_outputs: int | None = None,
    ) -> None:
        self.measured_max_outputs = measured_max_outputs
        self.variants = [dict(variant) for variant in variants]
        self.experiment: dict[str, Any] = {
            "experiment_id": _EXPERIMENT_ID,
            "owner_id": _OWNER,
            "workflow_id": "portrait",
            "server_id": "local",
            "status": "queued",
            "failure_policy": failure_policy,
            "submission_window": submission_window,
            "cancel_mode": cancel_mode,
            "pinned_revision_id": "revision_" + "1" * 64,
            "pinned_deployment_id": "deployment_" + "2" * 64,
            "pinned_content_digest": "3" * 64,
        }
        if concurrency is not None:
            self.experiment["concurrency"] = concurrency
        self.transitions: list[tuple[str, str, str]] = []
        self.claims: list[str] = []
        self.finishes: list[dict[str, Any]] = []
        self.list_limits: list[int] = []
        self.active_fence = 1
        self._refresh_counts()

    def claim_variant_for_submission(
        self,
        lease: WorkLease,
        *,
        experiment_id: str,
        owner_id: str,
        variant_id: str,
        now: datetime,
    ) -> dict[str, Any] | None:
        del now
        self._require_fence(lease)
        assert (experiment_id, owner_id) == (_EXPERIMENT_ID, _OWNER)
        self.claims.append(variant_id)
        if self.experiment.get("cancel_mode") or self.experiment.get("status") == "cancelled":
            return None
        variant = next(item for item in self.variants if item["variant_id"] == variant_id)
        if variant["status"] != "pending":
            return None
        variant["status"] = "submitted"
        self._refresh_counts()
        return {
            "variant_id": variant_id,
            "owner_id": owner_id,
            "workflow_id": self.experiment["workflow_id"],
            "server_id": self.experiment["server_id"],
            "revision_id": self.experiment["pinned_revision_id"],
            "deployment_id": self.experiment["pinned_deployment_id"],
            "content_digest": self.experiment["pinned_content_digest"],
            "arguments": dict(variant["arguments"]),
            "claim_token": str(lease.fencing_token),
            "claimed_at": _NOW.isoformat(),
        }

    def get_experiment(self, experiment_id: str, owner_id: str) -> dict[str, Any]:
        assert (experiment_id, owner_id) == (_EXPERIMENT_ID, _OWNER)
        return dict(self.experiment)

    def list_for_advance(
        self, experiment_id: str, owner_id: str, *, limit: int
    ) -> list[dict[str, Any]]:
        assert (experiment_id, owner_id) == (_EXPERIMENT_ID, _OWNER)
        self.list_limits.append(limit)
        return [dict(variant) for variant in self.variants[:limit]]

    def apply_transition(
        self,
        lease: WorkLease,
        *,
        experiment_id: str,
        owner_id: str,
        variant_id: str,
        status: str,
        job_id: str,
        checkpoint: dict[str, Any],
        now: datetime,
        event_type: str,
        event_data: dict[str, Any],
    ) -> None:
        del now, event_type
        self._require_fence(lease)
        assert (experiment_id, owner_id) == (_EXPERIMENT_ID, _OWNER)
        variant = next(item for item in self.variants if item["variant_id"] == variant_id)
        variant["status"] = status
        if job_id:
            variant["job_id"] = job_id
        self.transitions.append((variant_id, status, job_id))
        self._refresh_counts()
        self.transition_data = event_data
        if (
            self.measured_max_outputs is not None
            and event_data.get("measured_outputs", 0) > self.measured_max_outputs
        ):
            checkpoint["pause_reason"] = "MEASURED_BUDGET_EXCEEDED"

    def finish_advance(
        self,
        lease: WorkLease,
        *,
        experiment_id: str,
        owner_id: str,
        checkpoint: dict[str, Any],
        now: datetime,
        completed: bool,
        delay_seconds: int,
        status: str,
    ) -> None:
        self._require_fence(lease)
        assert (experiment_id, owner_id) == (_EXPERIMENT_ID, _OWNER)
        self.finishes.append(
            {
                "checkpoint": checkpoint,
                "now": now,
                "completed": completed,
                "delay_seconds": delay_seconds,
                "status": status,
            }
        )
        if status:
            self.experiment["status"] = status

    def _refresh_counts(self) -> None:
        self.experiment["variant_count"] = len(self.variants)
        for status in (
            "pending",
            "submitted",
            "running",
            "completed",
            "failed",
            "cancelled",
            "lost",
        ):
            self.experiment[f"{status}_count"] = sum(
                variant["status"] == status for variant in self.variants
            )

    def _require_fence(self, lease: WorkLease) -> None:
        if lease.fencing_token != self.active_fence:
            raise RuntimeError("work lease is expired or fenced")


def _variant(ordinal: int, status: str = "pending", *, job_id: str = "") -> dict[str, Any]:
    return {
        "variant_id": "variant_" + f"{ordinal:064x}",
        "ordinal": ordinal,
        "arguments": {"seed": ordinal},
        "status": status,
        "job_id": job_id,
    }


def _work() -> WorkItem:
    return WorkItem(
        "work_" + "b" * 64,
        f"comfyui://experiments/{_EXPERIMENT_ID}",
        "experiment.advance",
        {"experiment_id": _EXPERIMENT_ID, "owner_id": _OWNER},
        {},
        "running",
    )


def _lease(fence: int = 1) -> WorkLease:
    return WorkLease(
        _work().work_item_id, f"worker-{fence}", fence, (_NOW + timedelta(seconds=30)).isoformat()
    )


def test_single_server_defaults_to_one_execution_slot_without_using_submission_window() -> None:
    repository = _Repository([_variant(1), _variant(2)], concurrency=None, submission_window=0)
    runs = _Runs()
    execution = _Execution(runs)
    handler = ExperimentAdvanceHandler(repository, runs, execution, _Jobs())

    handler(_work(), _lease(), now=_NOW)

    assert len(execution.queue_submissions) == 1
    assert [variant["status"] for variant in repository.variants] == ["submitted", "pending"]
    assert repository.finishes[-1]["completed"] is False


class _AcceptedThenCrash(BaseException):
    pass


class _CrashExecution(_Execution):
    def submit(
        self,
        server_id: str,
        workflow_id: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str = "",
        owner_id: str = "",
        client_id: str = "",
        revision_id: str = "",
        deployment_id: str = "",
        content_digest: str = "",
    ) -> Job:
        self.runs.events.append("submit")
        del arguments, revision_id, deployment_id, content_digest
        if not self.runs.claims:
            self.queue_submissions.append(idempotency_key)
            self.runs.claims[idempotency_key] = {
                "client_id": "stable-client",
                "state": "submission_unknown",
            }
            raise _AcceptedThenCrash()
        claim = self.runs.claims[idempotency_key]
        assert claim["client_id"] == "stable-client"
        job = Job(
            prompt_id="prompt-recovered",
            server_id=server_id,
            workflow_id=workflow_id,
            status="submitted",
            idempotency_key=idempotency_key,
            client_id="stable-client",
            owner_id=owner_id,
            job_id="job_recovered",
        )
        self.runs.jobs[idempotency_key] = job
        return job


class _AcceptedThenFailure(_Execution):
    def submit(
        self,
        server_id: str,
        workflow_id: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str = "",
        owner_id: str = "",
        client_id: str = "",
        revision_id: str = "",
        deployment_id: str = "",
        content_digest: str = "",
    ) -> Job:
        job = super().submit(
            server_id,
            workflow_id,
            arguments,
            idempotency_key=idempotency_key,
            owner_id=owner_id,
            client_id=client_id,
            revision_id=revision_id,
            deployment_id=deployment_id,
            content_digest=content_digest,
        )
        self.runs.claims[idempotency_key] = {
            "client_id": job.client_id,
            "state": "submission_unknown",
        }
        raise RuntimeError("finalization failed after upstream acceptance")


def test_post_acceptance_failure_keeps_variant_submitted_for_reconciliation() -> None:
    repository = _Repository([_variant(1)])
    runs = _Runs()
    execution = _AcceptedThenFailure(runs)

    ExperimentAdvanceHandler(repository, runs, execution, _Jobs())(_work(), _lease(), now=_NOW)

    assert repository.variants[0]["status"] == "submitted"
    assert repository.variants[0]["job_id"].startswith("job_")
    assert repository.finishes[-1]["completed"] is False


def test_takeover_reconciles_accepted_prompt_without_duplicate_queue_submission() -> None:
    repository = _Repository([_variant(1)])
    runs = _Runs()
    execution = _CrashExecution(runs)
    handler = ExperimentAdvanceHandler(repository, runs, execution, _Jobs())

    try:
        handler(_work(), _lease(), now=_NOW)
    except _AcceptedThenCrash:
        pass
    else:
        raise AssertionError("injected worker crash did not interrupt the first lease")

    repository.active_fence = 2
    handler(_work(), _lease(2), now=_NOW + timedelta(seconds=31))
    assert runs.events[-3:] == ["job", "claim", "submit"]
    assert execution.queue_submissions == [
        "experiment:experiment_" + "a" * 64 + ":variant:variant_" + f"{1:064x}"
    ]
    assert repository.variants[0]["status"] == "submitted"
    assert repository.variants[0]["job_id"] == "job_recovered"


def test_lost_variant_is_terminal_and_never_auto_resubmitted() -> None:
    repository = _Repository([_variant(1, "lost")])
    runs = _Runs()
    execution = _Execution(runs)
    handler = ExperimentAdvanceHandler(repository, runs, execution, _Jobs())

    handler(_work(), _lease(), now=_NOW)

    assert execution.queue_submissions == []
    assert repository.finishes[-1]["completed"] is True
    assert repository.finishes[-1]["status"] == "completed_with_errors"


def test_running_existing_variant_consumes_execution_slot_before_new_submission() -> None:
    submitted = _variant(1, "submitted", job_id="job-existing")
    pending = _variant(2)
    repository = _Repository([submitted, pending], concurrency=1, submission_window=0)
    runs = _Runs()
    key = "experiment:" + _EXPERIMENT_ID + ":variant:" + submitted["variant_id"]
    runs.jobs[key] = Job(
        prompt_id="prompt-existing",
        server_id="local",
        workflow_id="portrait",
        status="running",
        idempotency_key=key,
        client_id="stable-existing",
        owner_id=_OWNER,
        job_id="job-existing",
    )
    execution = _Execution(runs)
    handler = ExperimentAdvanceHandler(repository, runs, execution, _Jobs())

    handler(_work(), _lease(), now=_NOW)

    assert execution.queue_submissions == []
    assert repository.variants[0]["status"] == "running"
    assert repository.variants[1]["status"] == "pending"


def test_stop_new_failure_policy_cancels_pending_without_touching_accepted_work() -> None:
    repository = _Repository(
        [_variant(1, "failed", job_id="job-failed"), _variant(2)],
        failure_policy="stop_new",
    )
    runs = _Runs()
    execution = _Execution(runs)
    handler = ExperimentAdvanceHandler(repository, runs, execution, _Jobs())

    handler(_work(), _lease(), now=_NOW)

    assert execution.queue_submissions == []
    assert [variant["status"] for variant in repository.variants] == ["failed", "cancelled"]
    assert repository.finishes[-1]["status"] == "completed_with_errors"


class _CancellingJobs:
    def __init__(self, runs: _Runs) -> None:
        self.runs = runs
        self.cancelled_prompts: list[str] = []

    def cancel(self, server_id: str, prompt_id: str, *, owner_id: str = "") -> Job:
        assert (server_id, owner_id) == ("local", _OWNER)
        key, current = next(
            (key, job) for key, job in self.runs.jobs.items() if job.prompt_id == prompt_id
        )
        assert current.status in {"submitted", "queued"}
        self.cancelled_prompts.append(prompt_id)
        cancelled = Job(
            prompt_id=current.prompt_id,
            server_id=current.server_id,
            workflow_id=current.workflow_id,
            status="cancelled",
            idempotency_key=current.idempotency_key,
            client_id=current.client_id,
            owner_id=current.owner_id,
            job_id=current.job_id,
        )
        self.runs.jobs[key] = cancelled
        return cancelled


def test_cancel_queued_policy_cancels_pending_and_queued_but_never_running() -> None:
    queued = _variant(2, "submitted", job_id="job-queued")
    running = _variant(3, "running", job_id="job-running")
    repository = _Repository(
        [_variant(1, "failed", job_id="job-failed"), queued, running, _variant(4)],
        failure_policy="cancel_queued",
        concurrency=1,
        submission_window=2,
    )
    runs = _Runs()
    for variant, status in ((queued, "queued"), (running, "running")):
        key = "experiment:" + _EXPERIMENT_ID + ":variant:" + variant["variant_id"]
        runs.jobs[key] = Job(
            prompt_id="prompt-" + status,
            server_id="local",
            workflow_id="portrait",
            status=status,
            idempotency_key=key,
            client_id="client-" + status,
            owner_id=_OWNER,
            job_id=str(variant["job_id"]),
        )
    jobs = _CancellingJobs(runs)
    execution = _Execution(runs)
    handler = ExperimentAdvanceHandler(repository, runs, execution, jobs)

    handler(_work(), _lease(), now=_NOW)

    assert execution.queue_submissions == []
    assert jobs.cancelled_prompts == ["prompt-queued"]
    assert [variant["status"] for variant in repository.variants] == [
        "failed",
        "cancelled",
        "running",
        "cancelled",
    ]
    assert repository.finishes[-1]["completed"] is False


class _UnknownExecution(_Execution):
    def submit(
        self,
        server_id: str,
        workflow_id: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str = "",
        owner_id: str = "",
        client_id: str = "",
        revision_id: str = "",
        deployment_id: str = "",
        content_digest: str = "",
    ) -> Job:
        del (
            server_id,
            workflow_id,
            arguments,
            idempotency_key,
            owner_id,
            client_id,
            revision_id,
            deployment_id,
            content_digest,
        )
        raise ExecutionInProgress("Submission outcome is unknown; retry status reconciliation")


def test_submission_unknown_is_checkpointed_as_submitted_and_deferred() -> None:
    repository = _Repository([_variant(1)])
    runs = _Runs()
    key = "experiment:" + _EXPERIMENT_ID + ":variant:" + _variant(1)["variant_id"]
    runs.claims[key] = {"client_id": "stable-client", "state": "submission_unknown"}
    handler = ExperimentAdvanceHandler(repository, runs, _UnknownExecution(runs), _Jobs())

    handler(_work(), _lease(), now=_NOW)

    assert repository.variants[0]["status"] == "submitted"
    assert repository.variants[0]["job_id"] == ""
    assert repository.finishes[-1]["completed"] is False
    assert repository.finishes[-1]["delay_seconds"] == 30


def test_continue_policy_submits_after_a_terminal_failure() -> None:
    repository = _Repository(
        [_variant(1, "failed", job_id="job-failed"), _variant(2)],
        failure_policy="continue",
    )
    runs = _Runs()
    execution = _Execution(runs)

    ExperimentAdvanceHandler(repository, runs, execution, _Jobs())(_work(), _lease(), now=_NOW)

    assert len(execution.queue_submissions) == 1
    assert [variant["status"] for variant in repository.variants] == ["failed", "submitted"]


def test_submission_window_is_bounded_separately_from_running_slots() -> None:
    running = _variant(1, "running", job_id="job-running")
    repository = _Repository(
        [running, _variant(2), _variant(3), _variant(4)],
        concurrency=1,
        submission_window=2,
    )
    runs = _Runs()
    key = "experiment:" + _EXPERIMENT_ID + ":variant:" + running["variant_id"]
    runs.jobs[key] = Job(
        prompt_id="prompt-running",
        server_id="local",
        workflow_id="portrait",
        status="running",
        idempotency_key=key,
        client_id="client-running",
        owner_id=_OWNER,
        job_id="job-running",
    )
    execution = _Execution(runs)

    ExperimentAdvanceHandler(repository, runs, execution, _Jobs())(_work(), _lease(), now=_NOW)

    assert len(execution.queue_submissions) == 2
    assert [variant["status"] for variant in repository.variants] == [
        "running",
        "submitted",
        "submitted",
        "pending",
    ]
    assert repository.list_limits == [100]


def test_fence_takeover_resumes_only_unfinished_variants() -> None:
    repository = _Repository([_variant(1), _variant(2)], concurrency=1)
    runs = _Runs()
    execution = _Execution(runs)
    handler = ExperimentAdvanceHandler(repository, runs, execution, _Jobs())

    handler(_work(), _lease(), now=_NOW)
    first_key = execution.queue_submissions[0]
    first = runs.jobs[first_key]
    runs.jobs[first_key] = Job(
        prompt_id=first.prompt_id,
        server_id=first.server_id,
        workflow_id=first.workflow_id,
        status="completed",
        idempotency_key=first.idempotency_key,
        client_id=first.client_id,
        owner_id=first.owner_id,
        job_id=first.job_id,
    )

    repository.active_fence = 2
    handler(_work(), _lease(2), now=_NOW + timedelta(seconds=31))

    assert len(execution.queue_submissions) == 2
    assert [variant["status"] for variant in repository.variants] == [
        "completed",
        "submitted",
    ]
    with pytest.raises(RuntimeError, match="fenced"):
        handler(_work(), _lease(1), now=_NOW + timedelta(seconds=32))
    assert len(execution.queue_submissions) == 2


class _FailingExecution(_Execution):
    def submit(
        self,
        server_id: str,
        workflow_id: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str = "",
        owner_id: str = "",
        client_id: str = "",
        revision_id: str = "",
        deployment_id: str = "",
        content_digest: str = "",
    ) -> Job:
        del (
            server_id,
            workflow_id,
            arguments,
            idempotency_key,
            owner_id,
            client_id,
            revision_id,
            deployment_id,
            content_digest,
        )
        raise RuntimeError("gateway failure")


def test_submission_error_becomes_failed_variant_and_continue_advances_next() -> None:
    repository = _Repository([_variant(1), _variant(2)], failure_policy="continue")
    runs = _Runs()
    handler = ExperimentAdvanceHandler(repository, runs, _FailingExecution(runs), _Jobs())

    handler(_work(), _lease(), now=_NOW)

    assert [variant["status"] for variant in repository.variants] == ["failed", "failed"]
    assert repository.transition_data["error_code"] == "RuntimeError"
    assert repository.finishes[-1]["status"] == "completed_with_errors"


class _IdentityExecution(_Execution):
    def __init__(self, runs: _Runs) -> None:
        super().__init__(runs)
        self.client_ids: list[str] = []
        self.pins: list[tuple[str, str, str]] = []

    def submit(
        self,
        server_id: str,
        workflow_id: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str = "",
        owner_id: str = "",
        client_id: str = "",
        revision_id: str = "",
        deployment_id: str = "",
        content_digest: str = "",
    ) -> Job:
        self.client_ids.append(client_id)
        self.pins.append((revision_id, deployment_id, content_digest))
        return super().submit(
            server_id,
            workflow_id,
            arguments,
            idempotency_key=idempotency_key,
            owner_id=owner_id,
            client_id=client_id,
            revision_id=revision_id,
            deployment_id=deployment_id,
            content_digest=content_digest,
        )


def test_persisted_variant_client_identity_is_passed_through_execution_service() -> None:
    variant = _variant(1)
    variant["client_id"] = "experiment-stable-client"
    repository = _Repository([variant])
    runs = _Runs()
    execution = _IdentityExecution(runs)

    ExperimentAdvanceHandler(repository, runs, execution, _Jobs())(_work(), _lease(), now=_NOW)

    assert execution.client_ids == ["experiment-stable-client"]
    assert execution.pins == [("revision_" + "1" * 64, "deployment_" + "2" * 64, "3" * 64)]


def test_cancellation_winning_atomic_claim_skips_external_submit() -> None:
    repository = _Repository([_variant(1)], cancel_mode="stop_new")
    runs = _Runs()
    execution = _Execution(runs)
    handler = ExperimentAdvanceHandler(repository, runs, execution, _Jobs())

    handler(_work(), _lease(), now=_NOW)

    assert repository.claims == []
    assert execution.queue_submissions == []
    assert repository.variants[0]["status"] == "cancelled"


def test_claim_rejects_stale_fence_before_external_submit() -> None:
    repository = _Repository([_variant(1)])
    runs = _Runs()
    execution = _Execution(runs)
    handler = ExperimentAdvanceHandler(repository, runs, execution, _Jobs())

    with pytest.raises(RuntimeError, match="fenced"):
        handler(_work(), _lease(2), now=_NOW)

    assert execution.queue_submissions == []


def test_missing_atomic_claim_capability_fails_closed_before_submit() -> None:
    repository = _Repository([_variant(1)])
    repository.claim_variant_for_submission = None  # type: ignore[method-assign]
    runs = _Runs()
    execution = _Execution(runs)

    with pytest.raises(RuntimeError, match="atomic Experiment submission claims"):
        ExperimentAdvanceHandler(repository, runs, execution, _Jobs())(_work(), _lease(), now=_NOW)

    assert execution.queue_submissions == []


class _TerminalExecution(_Execution):
    def submit(
        self,
        server_id: str,
        workflow_id: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str = "",
        owner_id: str = "",
        client_id: str = "",
        revision_id: str = "",
        deployment_id: str = "",
        content_digest: str = "",
    ) -> Job:
        submitted = super().submit(
            server_id,
            workflow_id,
            arguments,
            idempotency_key=idempotency_key,
            owner_id=owner_id,
            client_id=client_id,
            revision_id=revision_id,
            deployment_id=deployment_id,
            content_digest=content_digest,
        )
        return Job(
            prompt_id=submitted.prompt_id,
            server_id=server_id,
            workflow_id=workflow_id,
            status="completed",
            outputs=(
                {
                    "artifact_id": "artifact_" + "f" * 64,
                    "resource_uri": "comfyui://artifacts/artifact_" + "f" * 64,
                    "pixels": 4096,
                },
            ),
            idempotency_key=idempotency_key,
            client_id=submitted.client_id,
            owner_id=owner_id,
            job_id=submitted.job_id,
        )


def test_terminal_transition_preserves_timing_measurements_and_result_links() -> None:
    repository = _Repository([_variant(1)])
    runs = _Runs()
    execution = _TerminalExecution(runs)

    ExperimentAdvanceHandler(repository, runs, execution, _Jobs())(
        _work(), _lease(), now=_NOW + timedelta(seconds=7)
    )

    assert repository.transition_data["measured_seconds"] == 7.0
    assert repository.transition_data["measured_pixels"] == 4096
    assert repository.transition_data["measured_outputs"] == 1
    assert repository.transition_data["result_job_uri"].startswith("comfyui://jobs/job_")
    assert repository.transition_data["artifact_uris"] == [
        "comfyui://artifacts/artifact_" + "f" * 64
    ]


class _CancelAfterAcceptExecution(_Execution):
    def __init__(self, runs: _Runs, repository: _Repository) -> None:
        super().__init__(runs)
        self.repository = repository

    def submit(
        self,
        server_id: str,
        workflow_id: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str = "",
        owner_id: str = "",
        client_id: str = "",
        revision_id: str = "",
        deployment_id: str = "",
        content_digest: str = "",
    ) -> Job:
        job = super().submit(
            server_id,
            workflow_id,
            arguments,
            idempotency_key=idempotency_key,
            owner_id=owner_id,
            client_id=client_id,
            revision_id=revision_id,
            deployment_id=deployment_id,
            content_digest=content_digest,
        )
        self.repository.experiment["cancel_mode"] = "cancel_queued"
        return job


def test_cancel_after_accept_reconciles_and_targets_claimed_queue_item() -> None:
    repository = _Repository([_variant(1)])
    runs = _Runs()
    execution = _CancelAfterAcceptExecution(runs, repository)
    jobs = _CancellingJobs(runs)

    ExperimentAdvanceHandler(repository, runs, execution, jobs)(_work(), _lease(), now=_NOW)

    assert execution.queue_submissions != []
    assert jobs.cancelled_prompts == ["prompt-1"]
    assert repository.variants[0]["status"] == "cancelled"


def test_lost_variant_with_continue_advances_remaining_without_resubmitting_lost() -> None:
    repository = _Repository([_variant(1, "lost"), _variant(2)], failure_policy="continue")
    runs = _Runs()
    execution = _Execution(runs)

    ExperimentAdvanceHandler(repository, runs, execution, _Jobs())(_work(), _lease(), now=_NOW)

    assert len(execution.queue_submissions) == 1
    assert execution.queue_submissions[0].endswith(repository.variants[1]["variant_id"])
    assert [variant["status"] for variant in repository.variants] == ["lost", "submitted"]
    assert repository.finishes[-1]["completed"] is False


def test_measured_budget_violation_pauses_before_next_submission() -> None:
    repository = _Repository(
        [_variant(1), _variant(2)],
        concurrency=2,
        measured_max_outputs=0,
    )
    runs = _Runs()
    execution = _TerminalExecution(runs)

    ExperimentAdvanceHandler(repository, runs, execution, _Jobs())(_work(), _lease(), now=_NOW)

    assert len(execution.queue_submissions) == 1
    assert [variant["status"] for variant in repository.variants] == ["completed", "pending"]
    assert repository.finishes[-1]["checkpoint"]["pause_reason"] == ("MEASURED_BUDGET_EXCEEDED")


def test_takeover_resumes_persisted_pre_submit_claim_once() -> None:
    claimed = _variant(1, "submitted")
    claimed["client_id"] = "persisted-claim-client"
    repository = _Repository([claimed])
    repository.active_fence = 2
    runs = _Runs()
    execution = _Execution(runs)

    ExperimentAdvanceHandler(repository, runs, execution, _Jobs())(
        _work(), _lease(2), now=_NOW + timedelta(seconds=31)
    )

    assert execution.queue_submissions == [
        "experiment:" + _EXPERIMENT_ID + ":variant:" + claimed["variant_id"]
    ]
    assert repository.variants[0]["job_id"].startswith("job_")


class _FailingReconcileRuns(_Runs):
    def get_by_idempotency(self, server_id: str, key: str, owner_id: str) -> Job | None:
        del server_id, key, owner_id
        raise RuntimeError("simulated reconcile persistence failure")


def _work_with(checkpoint: dict[str, Any]) -> WorkItem:
    base = _work()
    return WorkItem(
        base.work_item_id,
        base.subject_uri,
        base.work_type,
        base.payload,
        dict(checkpoint),
        base.status,
    )


def test_reconcile_persistent_error_fails_variant_after_bound() -> None:
    """A persistent reconcile failure must surface as a failed variant, not retry forever."""
    repository = _Repository([_variant(1, "submitted")])
    runs = _FailingReconcileRuns()
    handler = ExperimentAdvanceHandler(repository, runs, _Execution(runs), _Jobs())
    variant_id = _variant(1, "submitted")["variant_id"]

    handler(_work(), _lease(), now=_NOW)
    assert repository.variants[0]["status"] == "submitted"
    assert repository.finishes[-1]["checkpoint"]["reconcile_errors"] == {variant_id: 1}

    handler(_work_with(repository.finishes[-1]["checkpoint"]), _lease(), now=_NOW)
    assert repository.variants[0]["status"] == "submitted"
    assert repository.finishes[-1]["checkpoint"]["reconcile_errors"] == {variant_id: 2}

    handler(_work_with(repository.finishes[-1]["checkpoint"]), _lease(), now=_NOW)
    assert repository.variants[0]["status"] == "failed"
    assert repository.finishes[-1]["checkpoint"]["last_error"] == (
        "RECONCILE_ERROR:RuntimeError"
    )
