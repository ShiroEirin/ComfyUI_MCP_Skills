"""Phase N deterministic diagnostic rules and safe retry orchestration contracts."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from comfyui_mcp_skills.application.diagnostics import DiagnosticService, RetryService
from comfyui_mcp_skills.domain.diagnostics import DiagnosticRuleRegistry
from comfyui_mcp_skills.domain.errors import (
    DiagnosticNotFound,
    RepairPlanConflict,
    RepairPlanNotFound,
    RetryNotAllowed,
)
from comfyui_mcp_skills.domain.models import Job

_NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
_JOB_ID = "job_" + "1" * 64
_RETRY_JOB_ID = "job_" + "2" * 64
_PLAN_ID = "plan_" + "3" * 64
_REVISION_ID = "revision_" + "4" * 64
_DEPLOYMENT_ID = "deployment_" + "5" * 64


_LEGACY_CASES = (
    (
        "Unauthorized: Please login first to use this node",
        "legacy.cloud_api_unauthorized",
        "cloud_api_unauthorized",
    ),
    ("vae model: No such file", "legacy.missing_vae", "missing_vae_model"),
    (
        "clip_vision.safetensors: No such file or directory",
        "legacy.missing_clip",
        "missing_clip_model",
    ),
    ("LoRA detail_tweaker.safetensors not found", "legacy.missing_lora", "missing_lora_model"),
    ("FileNotFoundError: model.ckpt", "legacy.missing_ckpt", "missing_model"),
    ("Could not load sdxl.safetensors", "legacy.missing_safetensors", "missing_model"),
    ("class_type not found: IPAdapterApply", "legacy.missing_custom_node", "missing_node"),
    ("Error: prompt is not valid", "legacy.invalid_prompt", "invalid_prompt"),
    ("ConnectionError: Connection refused", "legacy.connection_refused", "server_offline"),
    ("requests.exceptions.ReadTimeout: timeout", "legacy.connection_timeout", "server_timeout"),
    ("RuntimeError: CUDA out of memory", "legacy.cuda_oom", "out_of_memory"),
    ("RuntimeError: MPS out of memory", "legacy.mps_oom", "out_of_memory"),
    ("RuntimeError: CUDA error: no kernel image", "legacy.cuda_driver", "gpu_driver_error"),
    (
        "FileNotFoundError: No such file or directory: input.png",
        "legacy.file_not_found",
        "missing_input",
    ),
)


@pytest.mark.parametrize(("message", "rule_id", "classification"), _LEGACY_CASES)
def test_registry_migrates_each_legacy_pattern_to_structured_result(
    message: str, rule_id: str, classification: str
) -> None:
    registry = DiagnosticRuleRegistry.default()

    match = registry.classify(message)

    assert match.rule_id == rule_id
    assert match.classification == classification
    assert len(registry.legacy_rules) == 14
    for action in (*match.safe_actions, *match.approval_actions):
        assert action.tool.startswith("comfyui.")
        assert "comfyui-skill" not in repr(action)


@pytest.mark.parametrize(
    ("message", "expected_rule"),
    (
        ("VAE model /models/vae/a.safetensors: No such file or directory", "legacy.missing_vae"),
        ("CLIP model clip.safetensors not found: FileNotFoundError", "legacy.missing_clip"),
        ("LoRA foo.safetensors not found: No such file or directory", "legacy.missing_lora"),
        ("node not found: FancyNode; No such file or directory", "legacy.missing_custom_node"),
        ("CUDA out of memory; CUDA error while allocating", "legacy.cuda_oom"),
    ),
)
def test_specific_rules_have_priority_over_generic_file_and_driver_rules(
    message: str, expected_rule: str
) -> None:
    assert DiagnosticRuleRegistry.default().classify(message).rule_id == expected_rule


@pytest.mark.parametrize(
    ("message", "classification", "retryable"),
    (
        ("Input image not found: upload.png", "missing_input", False),
        ("Type mismatch: expected IMAGE but got LATENT", "type_mismatch", False),
        ("Execution was interrupted by operator", "interrupted", True),
        ("server is offline and unreachable", "server_offline", True),
    ),
)
def test_registry_covers_phase_n_nonlegacy_failures(
    message: str, classification: str, retryable: bool
) -> None:
    match = DiagnosticRuleRegistry.default().classify(message)

    assert match.classification == classification
    assert match.retryable is retryable


class MemoryDiagnosticRetryRepository:
    def __init__(self) -> None:
        self.job_context: dict[str, Any] | None = None
        self.server_context: dict[str, Any] | None = None
        self.diagnostics: dict[str, dict[str, Any]] = {}
        self.retry_context: dict[str, Any] | None = None
        self.plans: dict[str, dict[str, Any]] = {}
        self.context_limits: list[tuple[int, int]] = []
        self.mark_calls: list[tuple[str, str, str, str, datetime]] = []
        self.commit_intents: set[str] = set()

    def get_job_diagnostic_context(
        self, job_id: str, owner_id: str, *, event_limit: int, log_line_limit: int
    ) -> dict[str, Any] | None:
        self.context_limits.append((event_limit, log_line_limit))
        if self.job_context is None or self.job_context.get("owner_id") != owner_id:
            return None
        return copy.deepcopy(self.job_context)

    def get_server_diagnostic_context(
        self, server_id: str, owner_id: str, *, event_limit: int, log_line_limit: int
    ) -> dict[str, Any] | None:
        self.context_limits.append((event_limit, log_line_limit))
        if self.server_context is None or self.server_context.get("owner_id") != owner_id:
            return None
        return copy.deepcopy(self.server_context)

    def save_diagnostic(self, report: dict[str, Any]) -> dict[str, Any]:
        self.diagnostics.setdefault(report["diagnostic_id"], copy.deepcopy(report))
        return copy.deepcopy(self.diagnostics[report["diagnostic_id"]])

    def get_diagnostic(self, diagnostic_id: str, owner_id: str) -> dict[str, Any] | None:
        report = self.diagnostics.get(diagnostic_id)
        if report is None or report["owner_id"] != owner_id:
            return None
        return copy.deepcopy(report)

    def get_retry_context(self, job_id: str, owner_id: str) -> dict[str, Any] | None:
        if self.retry_context is None or self.retry_context.get("owner_id") != owner_id:
            return None
        return copy.deepcopy(self.retry_context)

    def save_repair_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        self.plans.setdefault(plan["repair_plan_id"], copy.deepcopy(plan))
        return copy.deepcopy(self.plans[plan["repair_plan_id"]])

    def get_repair_plan(self, repair_plan_id: str, owner_id: str) -> dict[str, Any] | None:
        plan = self.plans.get(repair_plan_id)
        if plan is None or plan["owner_id"] != owner_id:
            return None
        return copy.deepcopy(plan)

    def reserve_repair_plan_commit(
        self,
        repair_plan_id: str,
        plan_digest: str,
        owner_id: str,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        plan = self.plans[repair_plan_id]
        if plan["plan_digest"] != plan_digest or plan["owner_id"] != owner_id:
            raise ValueError("plan conflict")
        if repair_plan_id not in self.commit_intents:
            if datetime.fromisoformat(plan["expires_at"]) <= now:
                raise ValueError("plan expired")
            self.commit_intents.add(repair_plan_id)
        return copy.deepcopy(plan)

    def mark_repair_plan_committed(
        self,
        repair_plan_id: str,
        plan_digest: str,
        owner_id: str,
        retry_job_id: str,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        plan = self.plans[repair_plan_id]
        if plan["plan_digest"] != plan_digest or plan["owner_id"] != owner_id:
            raise ValueError("plan conflict")
        existing = str(plan.get("result_job_id", ""))
        if existing and existing != retry_job_id:
            raise ValueError("result conflict")
        plan.update(
            status="committed",
            result_job_id=retry_job_id,
            result_job_uri=f"comfyui://jobs/{retry_job_id}",
            retry_of=plan["original_job_id"],
            committed_at=now.isoformat(),
        )
        self.mark_calls.append((repair_plan_id, plan_digest, owner_id, retry_job_id, now))
        return copy.deepcopy(plan)


class RecordingExecution:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

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
        retry_of: str = "",
    ) -> Job:
        self.calls.append(
            {
                "server_id": server_id,
                "workflow_id": workflow_id,
                "arguments": copy.deepcopy(arguments),
                "idempotency_key": idempotency_key,
                "owner_id": owner_id,
                "client_id": client_id,
                "revision_id": revision_id,
                "deployment_id": deployment_id,
                "content_digest": content_digest,
                "retry_of": retry_of,
            }
        )
        return Job(
            prompt_id="prompt-retry",
            server_id=server_id,
            workflow_id=workflow_id,
            status="submitted",
            owner_id=owner_id,
            job_id=_RETRY_JOB_ID,
            revision_id=revision_id,
            deployment_id=deployment_id,
        )


def _job_context(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "job_id": _JOB_ID,
        "owner_id": "owner-a",
        "workflow_id": "portrait",
        "server_id": "local",
        "status": "error",
        "error": "Type mismatch: expected IMAGE but got LATENT",
        "failed_node": {
            "node_id": "12",
            "class_type": "LoadImage",
            "error_type": "TypeError",
            "message": "password=hunter2 at C:\\private\\workflow.json",
            "prompt": "must-not-escape",
        },
        "events": [
            {
                "event_type": "JOB_FAILED",
                "occurred_at": f"2026-08-03T12:00:{index:02d}+00:00",
                "message": f"token=secret-{index} failure {index}",
                "data": {"raw_prompt": "hidden"},
            }
            for index in range(12)
        ],
        "log_lines": [
            f"line {index} api_key=top-secret /home/user/model.safetensors" for index in range(12)
        ],
    }
    result.update(overrides)
    return result


def _retry_context(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "job_id": _JOB_ID,
        "owner_id": "owner-a",
        "workflow_id": "portrait",
        "server_id": "local",
        "status": "error",
        "plan_id": _PLAN_ID,
        "revision_id": _REVISION_ID,
        "deployment_id": _DEPLOYMENT_ID,
        "content_digest": "6" * 64,
        "raw_arguments": {"seed": 7, "steps": 20, "nullable": None},
        "legacy_migrated": False,
    }
    result.update(overrides)
    return result


def test_job_diagnostic_is_owner_bound_structured_bounded_and_redacted() -> None:
    repository = MemoryDiagnosticRetryRepository()
    repository.job_context = _job_context()
    service = DiagnosticService(repository, clock=lambda: _NOW)

    report = service.diagnose_job(_JOB_ID, "owner-a")

    assert report["registry_version"] == "diagnostic-rules-v1"
    assert report["subject_uri"] == f"comfyui://jobs/{_JOB_ID}"
    assert report["classification"] == "type_mismatch"
    assert report["resource_uri"] == f"comfyui://diagnostics/{report['diagnostic_id']}"
    assert report["evidence"]["failed_node"] == {
        "node_id": "12",
        "class_type": "LoadImage",
        "error_type": "TypeError",
        "message": "password=[REDACTED] at [PATH]",
    }
    assert len(report["evidence"]["events"]) == 8
    assert len(report["evidence"]["log_window"]) == 8
    serialized = repr(report)
    assert "hunter2" not in serialized and "top-secret" not in serialized
    assert "must-not-escape" not in serialized and "comfyui-skill" not in serialized
    assert repository.context_limits == [(8, 8)]
    assert set(report) == {
        "diagnostic_id",
        "registry_version",
        "subject_uri",
        "classification",
        "rule_id",
        "retryable",
        "evidence",
        "safe_actions",
        "approval_actions",
        "created_at",
        "resource_uri",
    }
    assert all(action["risk"] == "safe" for action in report["safe_actions"])
    assert all(action["risk"] == "approval_required" for action in report["approval_actions"])


def test_diagnostic_redacts_json_and_authorization_credentials() -> None:
    repository = MemoryDiagnosticRetryRepository()
    repository.job_context = _job_context(
        failed_node={"message": '{"password": "my secret", "Authorization": "Bearer abc.def"}'},
        events=[{"message": '{"api_key":"quoted-key"}'}],
        log_lines=['{"token":"quoted token"}', "Authorization: Basic dXNlcjpwYXNz"],
    )

    report = DiagnosticService(repository, clock=lambda: _NOW).diagnose_job(_JOB_ID, "owner-a")

    serialized = repr(report)
    for secret in ("my secret", "abc.def", "quoted-key", "quoted token", "dXNlcjpwYXNz"):
        assert secret not in serialized


def test_diagnostic_reads_are_owner_bound_and_status_can_classify_interruption() -> None:
    repository = MemoryDiagnosticRetryRepository()
    repository.job_context = _job_context(status="interrupted", error="")
    service = DiagnosticService(repository, clock=lambda: _NOW)
    report = service.diagnose_job(_JOB_ID, "owner-a")

    assert report["classification"] == "interrupted"
    assert service.get(report["diagnostic_id"], "owner-a") == report
    with pytest.raises(DiagnosticNotFound):
        service.get(report["diagnostic_id"], "owner-b")
    with pytest.raises(DiagnosticNotFound):
        service.diagnose_job(_JOB_ID, "owner-b")


def test_server_diagnostic_uses_owner_bound_server_context() -> None:
    repository = MemoryDiagnosticRetryRepository()
    repository.server_context = {
        "server_id": "local",
        "owner_id": "owner-a",
        "status": "offline",
        "error": "Connection refused",
        "events": [],
        "log_lines": [],
    }

    result = DiagnosticService(repository, clock=lambda: _NOW).diagnose_server("local", "owner-a")

    assert result["subject_uri"] == "comfyui://servers/local"
    assert result["classification"] == "server_offline"
    assert result["retryable"] is True


def test_retry_plan_preserves_snapshot_and_enumerates_normalized_exact_diff() -> None:
    repository = MemoryDiagnosticRetryRepository()
    repository.retry_context = _retry_context()
    original = copy.deepcopy(repository.retry_context["raw_arguments"])
    service = RetryService(repository, RecordingExecution(), clock=lambda: _NOW)

    result = service.plan(_JOB_ID, "owner-a", {"seed": 9, "new/value": "x", "nullable": None})

    stored = repository.plans[result["repair_plan_id"]]
    assert stored["original_arguments_snapshot"] == original
    assert repository.retry_context["raw_arguments"] == original
    assert stored["normalized_changes"] == {"new/value": "x", "nullable": None, "seed": 9}
    assert stored["resulting_arguments"] == {
        "seed": 9,
        "steps": 20,
        "nullable": None,
        "new/value": "x",
    }
    assert stored["diff"] == [
        {
            "path": "/arguments/new~1value",
            "operation": "add",
            "before": None,
            "after": "x",
        },
        {
            "path": "/arguments/nullable",
            "operation": "unchanged",
            "before": None,
            "after": None,
        },
        {
            "path": "/arguments/seed",
            "operation": "replace",
            "before": 7,
            "after": 9,
        },
    ]
    assert result["resource_uri"] == f"comfyui://plans/{result['repair_plan_id']}"
    assert result["expires_at"] == (_NOW + timedelta(hours=1)).isoformat()
    assert "original_arguments_snapshot" not in result
    assert "resulting_arguments" not in result


def test_retry_diff_distinguishes_json_number_and_boolean_types() -> None:
    repository = MemoryDiagnosticRetryRepository()
    repository.retry_context = _retry_context(raw_arguments={"value": 1, "flag": True})

    plan = RetryService(repository, RecordingExecution(), clock=lambda: _NOW).plan(
        _JOB_ID, "owner-a", {"value": 1.0, "flag": 1}
    )

    assert [item["operation"] for item in repository.plans[plan["repair_plan_id"]]["diff"]] == [
        "replace",
        "replace",
    ]


def test_retry_commit_pins_execution_and_is_idempotent() -> None:
    repository = MemoryDiagnosticRetryRepository()
    repository.retry_context = _retry_context()
    execution = RecordingExecution()
    service = RetryService(repository, execution, clock=lambda: _NOW)
    plan = service.plan(_JOB_ID, "owner-a", {"steps": 15})

    first = service.commit(plan["repair_plan_id"], plan["plan_digest"], "owner-a")
    second = service.commit(plan["repair_plan_id"], plan["plan_digest"], "owner-a")

    assert first == second
    assert first["result_job_id"] == _RETRY_JOB_ID
    assert first["retry_of"] == _JOB_ID
    assert len(execution.calls) == 1
    assert execution.calls[0] == {
        "server_id": "local",
        "workflow_id": "portrait",
        "arguments": {"seed": 7, "steps": 15, "nullable": None},
        "idempotency_key": f"repair:{plan['repair_plan_id']}",
        "owner_id": "owner-a",
        "client_id": plan["repair_plan_id"],
        "revision_id": _REVISION_ID,
        "deployment_id": _DEPLOYMENT_ID,
        "content_digest": "6" * 64,
        "retry_of": _JOB_ID,
    }
    assert len(repository.mark_calls) == 1


def test_retry_commit_rejects_expired_wrong_digest_and_owner_before_execution() -> None:
    repository = MemoryDiagnosticRetryRepository()
    repository.retry_context = _retry_context()
    execution = RecordingExecution()
    planning = RetryService(repository, execution, clock=lambda: _NOW)
    plan = planning.plan(_JOB_ID, "owner-a", {})

    with pytest.raises(RepairPlanConflict, match="digest"):
        planning.commit(plan["repair_plan_id"], "0" * 64, "owner-a")
    with pytest.raises(RepairPlanNotFound):
        planning.commit(plan["repair_plan_id"], plan["plan_digest"], "owner-b")
    expired = RetryService(repository, execution, clock=lambda: _NOW + timedelta(hours=1))
    with pytest.raises(RepairPlanConflict, match="expired"):
        expired.commit(plan["repair_plan_id"], plan["plan_digest"], "owner-a")
    assert execution.calls == []


@pytest.mark.parametrize(
    "overrides",
    (
        {"status": "completed"},
        {"legacy_migrated": True, "plan_id": "", "revision_id": "", "deployment_id": ""},
        {"content_digest": ""},
        {"raw_arguments": []},
    ),
)
def test_retry_plan_rejects_nonfailed_unpinned_or_invalid_originals(
    overrides: dict[str, Any],
) -> None:
    repository = MemoryDiagnosticRetryRepository()
    repository.retry_context = _retry_context(**overrides)

    with pytest.raises(RetryNotAllowed):
        RetryService(repository, RecordingExecution(), clock=lambda: _NOW).plan(
            _JOB_ID, "owner-a", {}
        )


def test_retry_plan_and_get_are_owner_bound_and_validate_json_changes() -> None:
    repository = MemoryDiagnosticRetryRepository()
    repository.retry_context = _retry_context()
    service = RetryService(repository, RecordingExecution(), clock=lambda: _NOW)

    with pytest.raises(RepairPlanNotFound):
        service.plan(_JOB_ID, "owner-b", {})
    with pytest.raises(ValueError, match="finite JSON"):
        service.plan(_JOB_ID, "owner-a", {"cfg": float("nan")})
    plan = service.plan(_JOB_ID, "owner-a", {})
    assert service.get(plan["repair_plan_id"], "owner-a") == plan
    with pytest.raises(RepairPlanNotFound):
        service.get(plan["repair_plan_id"], "owner-b")
