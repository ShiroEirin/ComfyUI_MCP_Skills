"""Batch Job submission, waiting, and cancellation contracts."""

from __future__ import annotations

from typing import Any

import pytest

from comfyui_mcp_skills.application.job_batch import BatchService
from comfyui_mcp_skills.application.jobs import JobWaitTimeout
from comfyui_mcp_skills.domain.errors import WorkflowArgumentsError
from comfyui_mcp_skills.domain.models import Job


def _job(
    number: int,
    status: str = "submitted",
    *,
    error: str = "",
) -> Job:
    return Job(
        prompt_id=f"prompt-{number}",
        server_id="local",
        workflow_id=f"workflow-{number}",
        status=status,
        job_id=f"job_{number:032x}",
        error=error,
    )


def _descriptor(number: int, **extra: Any) -> dict[str, Any]:
    return {
        "server_id": "local",
        "workflow_id": f"workflow-{number}",
        "arguments": {"seed": number},
        **extra,
    }


class _Execution:
    def __init__(self, failures: dict[int, Exception] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[dict[str, Any]] = []

    def submit(
        self,
        server_id: str,
        workflow_id: str,
        arguments: dict[str, Any],
        **options: Any,
    ) -> Job:
        number = arguments["seed"]
        self.calls.append(
            {
                "server_id": server_id,
                "workflow_id": workflow_id,
                "arguments": arguments,
                **options,
            }
        )
        if number in self.failures:
            raise self.failures[number]
        return _job(number)


class _Jobs:
    def __init__(self) -> None:
        self.wait_results: dict[str, Job | Exception] = {}
        self.cancel_results: dict[str, Job | Exception] = {}
        self.wait_calls: list[tuple[str, str, float, str]] = []
        self.cancel_calls: list[tuple[str, str, str]] = []

    def wait(
        self,
        server_id: str,
        prompt_id: str,
        *,
        timeout_seconds: float,
        owner_id: str = "",
    ) -> Job:
        self.wait_calls.append((server_id, prompt_id, timeout_seconds, owner_id))
        result = self.wait_results[prompt_id]
        if isinstance(result, Exception):
            raise result
        return result

    def cancel(self, server_id: str, prompt_id: str, *, owner_id: str = "") -> Job:
        self.cancel_calls.append((server_id, prompt_id, owner_id))
        result = self.cancel_results.get(
            prompt_id,
            _job(int(prompt_id.rsplit("-", 1)[1]), "cancelled"),
        )
        if isinstance(result, Exception):
            raise result
        return result


def _service(
    *,
    execution: _Execution | None = None,
    jobs: _Jobs | None = None,
    max_items: int = 100,
    max_argument_bytes: int = 1024,
) -> tuple[BatchService, _Execution, _Jobs]:
    execution = execution or _Execution()
    jobs = jobs or _Jobs()
    return (
        BatchService(
            execution,
            jobs,
            max_items=max_items,
            max_argument_bytes=max_argument_bytes,
        ),
        execution,
        jobs,
    )


def test_submit_all_success_returns_shared_batch_and_job_handles() -> None:
    service, execution, _jobs = _service()

    result = service.submit(
        [_descriptor(1, idempotency_key="one"), _descriptor(2, targets=["7"])],
        owner_id="owner",
    )

    assert result["batch_id"].startswith("batch_")
    assert [item["status"] for item in result["items"]] == ["submitted", "submitted"]
    assert [item["prompt_id"] for item in result["items"]] == ["prompt-1", "prompt-2"]
    assert [item["job_id"] for item in result["items"]] == [
        f"job_{1:032x}",
        f"job_{2:032x}",
    ]
    assert result["counts"] == {"completed": 0, "failed": 0, "timed_out": 0}
    assert execution.calls[0]["idempotency_key"] == "one"
    assert execution.calls[0]["owner_id"] == "owner"
    assert execution.calls[1]["targets"] == ("7",)


def test_submit_isolates_validation_and_execution_failures() -> None:
    service, execution, _jobs = _service(execution=_Execution({2: ValueError("bad workflow")}))

    result = service.submit(
        [
            _descriptor(1),
            _descriptor(2),
            {"server_id": "local", "workflow_id": "broken", "arguments": []},
            _descriptor(4),
        ]
    )

    assert [item["status"] for item in result["items"]] == [
        "submitted",
        "failed",
        "failed",
        "submitted",
    ]
    assert [call["arguments"]["seed"] for call in execution.calls] == [1, 2, 4]
    assert result["items"][1]["error"] == {
        "code": "ValueError",
        "message": "bad workflow",
        "retryable": False,
        "details": {},
    }
    assert result["items"][2]["error"]["code"] == "INVALID_WORKFLOW_ARGUMENTS"
    assert result["counts"]["failed"] == 2


@pytest.mark.parametrize("descriptors", [[], "not-a-sequence", {}])
def test_submit_rejects_empty_or_invalid_batch_input(descriptors: object) -> None:
    service, execution, _jobs = _service()

    with pytest.raises(WorkflowArgumentsError):
        service.submit(descriptors)  # type: ignore[arg-type]

    assert execution.calls == []


def test_submit_enforces_item_and_argument_bounds_without_partial_submission() -> None:
    service, execution, _jobs = _service(max_items=1, max_argument_bytes=32)

    with pytest.raises(WorkflowArgumentsError, match="at most 1"):
        service.submit([_descriptor(1), _descriptor(2)])
    assert execution.calls == []

    result = service.submit([_descriptor(1, arguments={"payload": "x" * 100})])
    assert result["items"][0]["status"] == "failed"
    assert "32 UTF-8 JSON bytes" in result["items"][0]["error"]["message"]
    assert execution.calls == []


def test_wait_aggregates_terminal_and_timeout_states_without_cancelling() -> None:
    service, _execution, jobs = _service()
    submitted = service.submit([_descriptor(1), _descriptor(2), _descriptor(3)])
    running = _job(3, "running")
    jobs.wait_results = {
        "prompt-1": _job(1, "completed"),
        "prompt-2": _job(2, "error", error="execution failed"),
        "prompt-3": JobWaitTimeout("prompt-3", 5.0, running),
    }

    result = service.wait(submitted["batch_id"], timeout_seconds=5, owner_id="owner")

    assert [item["status"] for item in result["items"]] == [
        "completed",
        "error",
        "timed_out",
    ]
    assert result["items"][2]["last_status"] == "running"
    assert result["items"][2]["prompt_id"] == "prompt-3"
    assert result["items"][1]["error"]["message"] == "execution failed"
    assert result["counts"] == {"completed": 1, "failed": 1, "timed_out": 1}
    assert jobs.wait_calls == [
        ("local", "prompt-1", 5.0, "owner"),
        ("local", "prompt-2", 5.0, "owner"),
        ("local", "prompt-3", 5.0, "owner"),
    ]
    assert jobs.cancel_calls == []


def test_cancel_records_failure_and_continues_with_remaining_items() -> None:
    service, _execution, jobs = _service()
    submitted = service.submit([_descriptor(1), _descriptor(2), _descriptor(3)])
    jobs.cancel_results["prompt-2"] = RuntimeError("cancel failed")

    result = service.cancel(submitted["batch_id"], owner_id="owner")

    assert jobs.cancel_calls == [
        ("local", "prompt-1", "owner"),
        ("local", "prompt-2", "owner"),
        ("local", "prompt-3", "owner"),
    ]
    assert [item["status"] for item in result["items"]] == [
        "cancelled",
        "failed",
        "cancelled",
    ]
    assert result["items"][1]["error"]["message"] == "cancel failed"
    assert result["items"][2]["prompt_id"] == "prompt-3"


def test_return_structure_is_stable_across_actions_and_mutations() -> None:
    service, _execution, jobs = _service()
    jobs.wait_results = {
        "prompt-1": _job(1, "completed"),
        "prompt-2": _job(2, "completed"),
    }
    submitted = service.submit([_descriptor(1), _descriptor(2)])
    submitted["items"][0]["status"] = "caller-mutation"
    item_keys = {
        "index",
        "status",
        "last_status",
        "server_id",
        "workflow_id",
        "prompt_id",
        "job_id",
        "error",
    }

    for result in (
        service.wait(submitted["batch_id"]),
        service.cancel(submitted["batch_id"]),
    ):
        assert set(result) == {"batch_id", "items", "counts"}
        assert set(result["counts"]) == {"completed", "failed", "timed_out"}
        assert all(set(item) == item_keys for item in result["items"])
        assert [item["index"] for item in result["items"]] == [0, 1]
        assert all(item["status"] != "caller-mutation" for item in result["items"])


def test_wait_validates_timeout_and_unknown_batch_before_calling_jobs() -> None:
    service, _execution, jobs = _service()
    submitted = service.submit([_descriptor(1)])

    for batch_id, timeout in ((submitted["batch_id"], -1), ("missing", 1)):
        with pytest.raises(WorkflowArgumentsError):
            service.wait(batch_id, timeout_seconds=timeout)
    assert jobs.wait_calls == []


def test_retained_handles_are_capped_so_a_long_lived_server_stays_bounded() -> None:
    service = BatchService(_Execution(), _Jobs(), max_batches=2)

    handles = [service.submit([_descriptor(number)])["batch_id"] for number in (1, 2, 3)]

    with pytest.raises(WorkflowArgumentsError, match="Batch not found"):
        service.cancel(handles[0])
    assert service.cancel(handles[1])["batch_id"] == handles[1]
    assert service.cancel(handles[2])["batch_id"] == handles[2]


def test_max_batches_must_be_a_positive_integer() -> None:
    for invalid in (0, -1, True):
        with pytest.raises(ValueError, match="max_batches"):
            BatchService(_Execution(), _Jobs(), max_batches=invalid)  # type: ignore[arg-type]
