"""Administrative workflow audit contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from comfyui_mcp_skills.application.admin import AdminAuditError, WorkflowAdmin
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

    result = admin.set_enabled(
        "local", "txt2img", False, request_id="request-success"
    )

    assert result["enabled"] is False
    records = _audit_records(tmp_path)
    assert records == [
        {
            "timestamp": records[0]["timestamp"],
            "request_id": "request-success",
            "actor": "configured-admin",
            "action": "workflow.set_enabled",
            "target": {"server_id": "local", "workflow_id": "txt2img"},
            "outcome": "intent",
            "error_code": None,
        },
        {
            "timestamp": records[1]["timestamp"],
            "request_id": "request-success",
            "actor": "configured-admin",
            "action": "workflow.set_enabled",
            "target": {"server_id": "local", "workflow_id": "txt2img"},
            "outcome": "success",
            "error_code": None,
        },
    ]
    serialized = json.dumps(records)
    assert all("parameters" not in record and "enabled" not in record for record in records)
    assert "token" not in serialized.lower()


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
        admin.set_enabled(
            "local", "txt2img", False, request_id="request-audit-down"
        )

    assert exc_info.value.code == "ADMIN_AUDIT_UNAVAILABLE"
    schema = json.loads(
        (tmp_path / "data" / "local" / "txt2img" / "schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["enabled"] is True
