"""Job/Plan lineage regeneration application-service contracts."""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from comfyui_mcp_skills.application.regeneration import RegenerationService
from comfyui_mcp_skills.domain.errors import RepairPlanNotFound, RetryNotAllowed
from comfyui_mcp_skills.domain.models import Job
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.sqlite_diagnostics import (
    SQLiteDiagnosticRetryRepository,
)

_NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
_JOB_ID = "job_" + "1" * 64
_RESULT_JOB_ID = "job_" + "2" * 64
_PLAN_ID = "plan_" + "3" * 64
_REVISION_ID = "revision_" + "4" * 64
_DEPLOYMENT_ID = "deployment_" + "5" * 64
_CONTENT_DIGEST = "6" * 64


class MemoryRegenerationRepository:
    def __init__(self, context: dict[str, Any] | None) -> None:
        self.context = context
        self.plans: dict[str, dict[str, Any]] = {}
        self.commit_intents: set[str] = set()

    def get_retry_context(self, job_id: str, owner_id: str) -> dict[str, Any] | None:
        if (
            self.context is None
            or self.context["job_id"] != job_id
            or self.context["owner_id"] != owner_id
        ):
            return None
        return copy.deepcopy(self.context)

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
        plan.update(
            status="committed",
            result_job_id=retry_job_id,
            result_job_uri=f"comfyui://jobs/{retry_job_id}",
            retry_of=plan["original_job_id"],
            committed_at=now.isoformat(),
        )
        return copy.deepcopy(plan)


class RecordingExecution:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def submit(
        self,
        server_id: str,
        workflow_id: str,
        arguments: dict[str, Any],
        **options: Any,
    ) -> Job:
        self.calls.append(
            {
                "server_id": server_id,
                "workflow_id": workflow_id,
                "arguments": copy.deepcopy(arguments),
                **copy.deepcopy(options),
            }
        )
        return Job(
            prompt_id="prompt-regeneration",
            server_id=server_id,
            workflow_id=workflow_id,
            status="submitted",
            owner_id=str(options["owner_id"]),
            job_id=_RESULT_JOB_ID,
            revision_id=str(options["revision_id"]),
            deployment_id=str(options["deployment_id"]),
        )


def _context(**changes: Any) -> dict[str, Any]:
    context: dict[str, Any] = {
        "job_id": _JOB_ID,
        "owner_id": "owner-a",
        "workflow_id": "portrait",
        "server_id": "local",
        "status": "completed",
        "plan_id": _PLAN_ID,
        "revision_id": _REVISION_ID,
        "deployment_id": _DEPLOYMENT_ID,
        "content_digest": _CONTENT_DIGEST,
        "raw_arguments": {"seed": 7, "steps": 20, "prompt": "persisted prompt"},
        "overrides": {},
        "legacy_migrated": False,
    }
    context.update(changes)
    return context


def _service(
    context: dict[str, Any] | None = None,
) -> tuple[RegenerationService, MemoryRegenerationRepository, RecordingExecution]:
    repository = MemoryRegenerationRepository(_context() if context is None else context)
    execution = RecordingExecution()
    return RegenerationService(repository, execution, clock=lambda: _NOW), repository, execution


def test_completed_job_regenerates_from_persisted_plan_and_arguments() -> None:
    service, _, execution = _service()

    plan = service.plan(_JOB_ID, "owner-a")
    repeated = service.plan(_JOB_ID, "owner-a")

    assert plan == repeated
    assert plan["operation"] == "regenerate"
    assert plan["source_job_id"] == _JOB_ID
    assert plan["pinned_plan_id"] == _PLAN_ID
    assert plan["revision_id"] == _REVISION_ID
    assert plan["deployment_id"] == _DEPLOYMENT_ID
    assert plan["content_digest"] == _CONTENT_DIGEST
    assert plan["original_arguments"] == plan["resulting_arguments"]
    assert plan["original_arguments"]["prompt"] == "persisted prompt"
    assert plan["repair_plan_id"] == "repair_plan_" + plan["plan_digest"]
    assert execution.calls == []


def test_failed_job_can_also_regenerate() -> None:
    service, _, _ = _service(_context(status="error"))

    plan = service.plan(_JOB_ID, "owner-a", {"seed": 8})

    assert plan["source_job_id"] == _JOB_ID
    assert plan["resulting_arguments"]["seed"] == 8


def test_ordinary_changes_produce_exact_original_and_resulting_diff() -> None:
    service, _, _ = _service()

    plan = service.plan(_JOB_ID, "owner-a", {"seed": 9, "cfg": 6.5})

    assert plan["original_arguments"] == {
        "seed": 7,
        "steps": 20,
        "prompt": "persisted prompt",
    }
    assert plan["resulting_arguments"] == {
        "seed": 9,
        "steps": 20,
        "prompt": "persisted prompt",
        "cfg": 6.5,
    }
    assert plan["diff"] == [
        {"path": "/arguments/cfg", "operation": "add", "before": None, "after": 6.5},
        {"path": "/arguments/seed", "operation": "replace", "before": 7, "after": 9},
    ]


def test_recorded_overrides_are_replayed_without_caller_repeating_them() -> None:
    service, _, _ = _service(_context(overrides={"7.seed": 99}))

    plan = service.plan(_JOB_ID, "owner-a", {"steps": 15})

    assert plan["overrides"] == {"7.seed": 99}
    assert plan["resulting_arguments"]["_overrides"] == {"7.seed": 99}
    assert plan["normalized_changes"] == {"_overrides": {"7.seed": 99}, "steps": 15}


def test_explicit_overrides_replace_recorded_overrides() -> None:
    service, _, _ = _service(_context(overrides={"7.seed": 99}))

    plan = service.plan(
        _JOB_ID,
        "owner-a",
        {"steps": 15, "_overrides": {"7.seed": 88}},
        _overrides={"7.seed": 5},
    )

    assert plan["overrides"] == {"7.seed": 5}
    assert plan["resulting_arguments"]["_overrides"] == {"7.seed": 5}


def test_missing_job_and_owner_mismatch_are_rejected() -> None:
    service, _, execution = _service()

    with pytest.raises(RepairPlanNotFound):
        service.plan(_JOB_ID, "owner-b")
    with pytest.raises(RepairPlanNotFound):
        service.plan("job_" + "f" * 64, "owner-a")
    assert execution.calls == []


@pytest.mark.parametrize("pin", ("plan_id", "revision_id", "deployment_id", "content_digest"))
def test_incomplete_execution_pins_are_rejected(pin: str) -> None:
    service, _, execution = _service(_context(**{pin: ""}))

    with pytest.raises(RetryNotAllowed):
        service.plan(_JOB_ID, "owner-a")
    assert execution.calls == []


def test_legacy_job_is_rejected_even_with_stale_pin_values() -> None:
    service, _, execution = _service(_context(legacy_migrated=True))

    with pytest.raises(RetryNotAllowed):
        service.plan(_JOB_ID, "owner-a")
    assert execution.calls == []


def test_commit_is_idempotent_and_preserves_pins_and_lineage() -> None:
    service, _, execution = _service(_context(overrides={"7.seed": 99}))
    plan = service.plan(_JOB_ID, "owner-a", {"steps": 15})

    first = service.commit(plan["repair_plan_id"], plan["plan_digest"], "owner-a")
    second = service.commit(plan["repair_plan_id"], plan["plan_digest"], "owner-a")

    assert first == second
    assert first["result_job_id"] == _RESULT_JOB_ID
    assert first["retry_of"] == _JOB_ID
    assert first["source_job_id"] == _JOB_ID
    assert len(execution.calls) == 1
    assert execution.calls[0] == {
        "server_id": "local",
        "workflow_id": "portrait",
        "arguments": {"seed": 7, "steps": 15, "prompt": "persisted prompt"},
        "idempotency_key": f"regenerate:{plan['repair_plan_id']}",
        "owner_id": "owner-a",
        "client_id": plan["repair_plan_id"],
        "revision_id": _REVISION_ID,
        "deployment_id": _DEPLOYMENT_ID,
        "content_digest": _CONTENT_DIGEST,
        "retry_of": _JOB_ID,
        "overrides": {"7.seed": 99},
    }


def test_sqlite_plan_round_trip_preserves_arguments_overrides_and_pins(
    tmp_path: Path,
) -> None:
    store = SQLiteControlPlaneStore((tmp_path / "regeneration.sqlite3").resolve())
    store.initialize()
    created_at = _NOW.isoformat()
    arguments = {"seed": 7, "steps": 20, "prompt": "persisted prompt"}
    snapshot = {
        "arguments": arguments,
        "resolved_inputs": arguments,
        "overrides": {"7.seed": 99},
    }
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO workflows(workflow_id,created_at) VALUES('portrait',?)", (created_at,)
        )
        connection.execute(
            """INSERT INTO workflow_revisions(
                   revision_id,workflow_id,graph_json,parameter_schema_json,
                   dependency_contract_json,content_digest,created_at
               ) VALUES(?,'portrait','{}','{}','{}',?,?)""",
            (_REVISION_ID, _CONTENT_DIGEST, created_at),
        )
        connection.execute(
            """INSERT INTO workflow_deployments(
                   deployment_id,workflow_id,revision_id,server_id,enabled,
                   validation_status,published,created_at
               ) VALUES(?,'portrait',?,'local',1,'valid',1,?)""",
            (_DEPLOYMENT_ID, _REVISION_ID, created_at),
        )
        connection.execute(
            """INSERT INTO execution_plans(
                   plan_id,workflow_id,revision_id,deployment_id,server_id,
                   resolved_inputs_json,input_digest,plan_digest,created_at,raw_arguments_digest
               ) VALUES(?,'portrait',?,?,'local',?,?,?,?,?)""",
            (
                _PLAN_ID,
                _REVISION_ID,
                _DEPLOYMENT_ID,
                json.dumps(snapshot, sort_keys=True),
                "a" * 64,
                "b" * 64,
                created_at,
                "c" * 64,
            ),
        )
        connection.execute(
            """INSERT INTO jobs(
                   job_id,workflow_id,plan_id,revision_id,deployment_id,owner_id,
                   server_id,status,error,outputs_json,created_at,
                   created_at_source,legacy_migrated,execution_origin
               ) VALUES(
                   ?,'portrait',?,?,?,'owner-a','local','completed','','[]',
                   ?,'runtime',0,'planned'
               )""",
            (_JOB_ID, _PLAN_ID, _REVISION_ID, _DEPLOYMENT_ID, created_at),
        )

    repository = SQLiteDiagnosticRetryRepository(store)
    service = RegenerationService(repository, RecordingExecution(), clock=lambda: _NOW)
    plan = service.plan(_JOB_ID, "owner-a", {"steps": 15})

    loaded = service.get(plan["repair_plan_id"], "owner-a")
    assert loaded == plan
    assert loaded["original_arguments"] == arguments
    assert loaded["resulting_arguments"] == {
        **arguments,
        "steps": 15,
        "_overrides": {"7.seed": 99},
    }
    assert loaded["overrides"] == {"7.seed": 99}
    assert loaded["revision_id"] == _REVISION_ID
    assert loaded["deployment_id"] == _DEPLOYMENT_ID
    assert loaded["content_digest"] == _CONTENT_DIGEST
