"""Administrative workflow audit contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from comfyui_mcp_skills.application.admin import (
    AdminAuditError,
    JsonlAuditLog,
    WorkflowAdmin,
)
from comfyui_mcp_skills.domain.errors import IdempotencyConflict
from comfyui_mcp_skills.domain.models import Workflow


class MemoryWorkflowRepository:
    def __init__(self, workflow: Workflow) -> None:
        self._workflow = workflow

    def list(self) -> list[Workflow]:
        return [self._workflow]

    def get(self, server_id: str, workflow_id: str) -> Workflow | None:
        if (server_id, workflow_id) == (
            self._workflow.server_id,
            self._workflow.workflow_id,
        ):
            return self._workflow
        return None


class FailOutcomeOnceAuditLog(JsonlAuditLog):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._append_count = 0

    def append(self, event: dict[str, object]) -> None:
        self._append_count += 1
        if self._append_count == 2:
            raise OSError("simulated outcome audit failure")
        super().append(event)


def _workflow_project(tmp_path: Path) -> MemoryWorkflowRepository:
    directory = tmp_path / "data" / "local" / "txt2img"
    directory.mkdir(parents=True)
    (directory / "schema.json").write_text(
        json.dumps({"enabled": True, "parameters": {}}), encoding="utf-8"
    )
    (directory / "workflow.json").write_text("{}", encoding="utf-8")
    return MemoryWorkflowRepository(
        Workflow(
            server_id="local",
            workflow_id="txt2img",
            description="",
            parameters={},
            graph={},
        )
    )


def _audit_records(tmp_path: Path) -> list[dict[str, object]]:
    path = tmp_path / "data" / "admin-audit.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_admin_records_intent_and_success_without_operation_parameters(
    tmp_path: Path,
) -> None:
    repository = _workflow_project(tmp_path)
    admin = WorkflowAdmin(tmp_path, repository, actor="configured-admin")

    result = admin.set_enabled("local", "txt2img", False, request_id="request-success")

    assert result["request_id"] == "request-success"
    assert result["committed"] is True
    assert result["audit_status"] == "audited"
    assert result["enabled"] is False
    records = _audit_records(tmp_path)
    assert records == [
        {
            "timestamp": records[0]["timestamp"],
            "request_id": "request-success",
            "actor": "configured-admin",
            "action": "workflow.set_enabled",
            "target": {"server_id": "local", "workflow_id": "txt2img"},
            "operation_key": records[0]["operation_key"],
            "outcome": "intent",
            "error_code": None,
        },
        {
            "timestamp": records[1]["timestamp"],
            "request_id": "request-success",
            "actor": "configured-admin",
            "action": "workflow.set_enabled",
            "target": {"server_id": "local", "workflow_id": "txt2img"},
            "operation_key": records[1]["operation_key"],
            "outcome": "success",
            "error_code": None,
        },
    ]
    assert records[0]["operation_key"] == records[1]["operation_key"]
    assert len(str(records[0]["operation_key"])) == 64
    serialized = json.dumps(records)
    assert all("parameters" not in record and "enabled" not in record for record in records)
    assert "token" not in serialized.lower()


def test_admin_returns_committed_pending_when_outcome_audit_fails(
    tmp_path: Path,
) -> None:
    repository = _workflow_project(tmp_path)
    audit_log = FailOutcomeOnceAuditLog(tmp_path / "data" / "admin-audit.jsonl")
    admin = WorkflowAdmin(
        tmp_path,
        repository,
        actor="configured-admin",
        audit_log=audit_log,
    )

    result = admin.set_enabled("local", "txt2img", False, request_id="request-outcome-down")

    assert result == {
        "server_id": "local",
        "workflow_id": "txt2img",
        "enabled": False,
        "request_id": "request-outcome-down",
        "committed": True,
        "audit_status": "pending",
    }
    schema = json.loads(
        (tmp_path / "data" / "local" / "txt2img" / "schema.json").read_text(encoding="utf-8")
    )
    assert schema["enabled"] is False
    assert [record["outcome"] for record in _audit_records(tmp_path)] == ["intent"]


def test_pending_delete_can_be_queried_retried_and_reused_without_redeleting(
    tmp_path: Path,
) -> None:
    repository = _workflow_project(tmp_path)
    audit_path = tmp_path / "data" / "admin-audit.jsonl"
    flaky_admin = WorkflowAdmin(
        tmp_path,
        repository,
        actor="configured-admin",
        audit_log=FailOutcomeOnceAuditLog(audit_path),
    )

    first_result = flaky_admin.delete(
        "local",
        "txt2img",
        "delete:local/txt2img",
        request_id="request-delete-pending",
    )

    assert first_result["committed"] is True
    assert first_result["audit_status"] == "pending"
    assert not (tmp_path / "data" / "local" / "txt2img").exists()

    recovered_admin = WorkflowAdmin(
        tmp_path,
        repository,
        actor="configured-admin",
    )
    assert recovered_admin.get_audit_status("request-delete-pending") == {
        "request_id": "request-delete-pending",
        "action": "workflow.delete",
        "target": {"server_id": "local", "workflow_id": "txt2img"},
        "committed": True,
        "audit_status": "pending",
    }
    assert recovered_admin.retry_audit("request-delete-pending")["audit_status"] == ("audited")

    repeated_result = recovered_admin.delete(
        "local",
        "txt2img",
        "delete:local/txt2img",
        request_id="request-delete-pending",
    )

    assert repeated_result == {
        "server_id": "local",
        "workflow_id": "txt2img",
        "deleted": True,
        "request_id": "request-delete-pending",
        "committed": True,
        "audit_status": "audited",
    }
    assert [record["outcome"] for record in _audit_records(tmp_path)] == [
        "intent",
        "success",
    ]


def test_request_id_cannot_be_reused_for_a_different_operation(
    tmp_path: Path,
) -> None:
    repository = _workflow_project(tmp_path)
    admin = WorkflowAdmin(tmp_path, repository, actor="configured-admin")
    admin.set_enabled("local", "txt2img", False, request_id="request-stable-operation")

    with pytest.raises(IdempotencyConflict):
        admin.set_enabled("local", "txt2img", True, request_id="request-stable-operation")

    schema = json.loads(
        (tmp_path / "data" / "local" / "txt2img" / "schema.json").read_text(encoding="utf-8")
    )
    assert schema["enabled"] is False
    assert [record["outcome"] for record in _audit_records(tmp_path)] == [
        "intent",
        "success",
    ]


def test_admin_records_failure_without_confirmation_or_token(tmp_path: Path) -> None:
    repository = _workflow_project(tmp_path)
    admin = WorkflowAdmin(tmp_path, repository, actor="configured-admin")

    with pytest.raises(ValueError, match="confirmation must equal"):
        admin.delete(
            "local",
            "txt2img",
            "bearer secret-token-value",
            request_id="request-failure",
        )

    records = _audit_records(tmp_path)
    assert [record["outcome"] for record in records] == ["intent", "failure"]
    assert records[-1]["error_code"] == "INVALID_ARGUMENTS"
    assert records[-1]["request_id"] == "request-failure"
    assert admin.get_audit_status("request-failure")["committed"] is False
    assert admin.get_audit_status("request-failure")["audit_status"] == "audited"
    serialized = json.dumps(records)
    assert "secret-token-value" not in serialized
    assert "confirmation" not in serialized
    assert (tmp_path / "data" / "local" / "txt2img").is_dir()


def test_admin_fails_closed_when_intent_audit_cannot_be_written(
    tmp_path: Path,
) -> None:
    repository = _workflow_project(tmp_path)
    audit_path = tmp_path / "data" / "admin-audit.jsonl"
    audit_path.mkdir()
    admin = WorkflowAdmin(tmp_path, repository, actor="configured-admin")

    with pytest.raises(AdminAuditError) as exc_info:
        admin.set_enabled("local", "txt2img", False, request_id="request-audit-down")

    assert exc_info.value.code == "ADMIN_AUDIT_UNAVAILABLE"
    schema = json.loads(
        (tmp_path / "data" / "local" / "txt2img" / "schema.json").read_text(encoding="utf-8")
    )
    assert schema["enabled"] is True


def test_terminal_audit_reconciles_stale_transaction_after_restart(tmp_path: Path) -> None:
    repository = _workflow_project(tmp_path)
    admin = WorkflowAdmin(tmp_path, repository, actor="configured-admin")
    admin.set_enabled("local", "txt2img", False, request_id="request-reconcile")
    transaction_path = next((tmp_path / ".admin-transactions").glob("*.json"))
    transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
    transaction["committed"] = None
    transaction["audit_status"] = "pending"
    transaction.pop("result", None)
    transaction_path.write_text(json.dumps(transaction), encoding="utf-8")

    recovered = WorkflowAdmin(tmp_path, repository, actor="configured-admin")

    assert recovered.get_audit_status("request-reconcile") == {
        "request_id": "request-reconcile",
        "action": "workflow.set_enabled",
        "target": {"server_id": "local", "workflow_id": "txt2img"},
        "committed": True,
        "audit_status": "audited",
    }


def test_admin_rejects_request_id_beyond_public_contract(tmp_path: Path) -> None:
    repository = _workflow_project(tmp_path)
    admin = WorkflowAdmin(tmp_path, repository, actor="configured-admin")

    with pytest.raises(ValueError, match="between 1 and 128"):
        admin.set_enabled("local", "txt2img", False, request_id="x" * 129)


def test_terminal_audit_prevents_reexecution_when_transaction_file_is_lost(
    tmp_path: Path,
) -> None:
    repository = _workflow_project(tmp_path)
    admin = WorkflowAdmin(tmp_path, repository, actor="configured-admin")
    first = admin.delete(
        "local",
        "txt2img",
        "delete:local/txt2img",
        request_id="request-audit-only",
    )
    assert first["committed"] is True

    for transaction_path in (tmp_path / ".admin-transactions").glob("*.json"):
        transaction_path.unlink()
    recreated = tmp_path / "data" / "local" / "txt2img"
    recreated.mkdir(parents=True)
    (recreated / "schema.json").write_text(
        json.dumps({"enabled": True, "parameters": {}}), encoding="utf-8"
    )
    (recreated / "workflow.json").write_text("{}", encoding="utf-8")

    recovered = WorkflowAdmin(tmp_path, repository, actor="configured-admin")
    repeated = recovered.delete(
        "local",
        "txt2img",
        "delete:local/txt2img",
        request_id="request-audit-only",
    )
    assert repeated["deleted"] is True

    assert repeated["committed"] is True
    assert repeated["audit_status"] == "audited"
    assert recreated.is_dir()
    assert [record["outcome"] for record in _audit_records(tmp_path)] == [
        "intent",
        "success",
    ]


def test_audit_only_recovery_rejects_different_operation_parameters(
    tmp_path: Path,
) -> None:
    repository = _workflow_project(tmp_path)
    admin = WorkflowAdmin(tmp_path, repository, actor="configured-admin")
    admin.set_enabled("local", "txt2img", False, request_id="request-audit-conflict")
    for transaction_path in (tmp_path / ".admin-transactions").glob("*.json"):
        transaction_path.unlink()

    recovered = WorkflowAdmin(tmp_path, repository, actor="configured-admin")
    with pytest.raises(IdempotencyConflict):
        recovered.set_enabled(
            "local",
            "txt2img",
            True,
            request_id="request-audit-conflict",
        )

    schema = json.loads(
        (tmp_path / "data" / "local" / "txt2img" / "schema.json").read_text(encoding="utf-8")
    )
    assert schema["enabled"] is False
