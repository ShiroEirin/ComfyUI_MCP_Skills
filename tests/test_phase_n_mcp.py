"""Focused MCP Phase N structured diagnostics and safe retry contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError

from comfyui_mcp_skills.adapters.mcp.prompts import create_prompt_handlers
from comfyui_mcp_skills.adapters.mcp.server import create_server
from comfyui_mcp_skills.adapters.mcp.tooling import (
    diagnostic_report_dict,
    phase_n_tools,
    repair_plan_dict,
)
from comfyui_mcp_skills.application.authorization import AuthorizationContext, Scope, Toolset

_JOB_ID = "job_" + "a" * 32
_DIAGNOSTIC_ID = "diagnostic_" + "b" * 64
_REPAIR_PLAN_ID = "repair_plan_" + "c" * 64
_PLAN_DIGEST = "d" * 64
_PHASE_N_NAMES = {
    "comfyui.job.diagnose",
    "comfyui.server.diagnose",
    "comfyui.job.retry.plan",
    "comfyui.job.retry.commit",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _project(root: Path) -> None:
    (root / "config.json").write_text('{"servers":[]}', encoding="utf-8")


def _diagnostic() -> dict[str, Any]:
    return {
        "diagnostic_id": _DIAGNOSTIC_ID,
        "registry_version": "diagnostic-rules-v1",
        "subject_uri": f"comfyui://jobs/{_JOB_ID}",
        "classification": "NODE_MISSING",
        "retryable": True,
        "evidence": {
            "status": "error",
            "failed_node": {"node_id": "7", "class_type": "MissingNode", "message": "missing"},
            "events": [
                {
                    "event_type": "failed",
                    "occurred_at": "2026-08-03T00:00:00Z",
                    "message": "missing",
                }
            ],
            "log_window": ["bounded diagnostic line"],
        },
        "safe_actions": [
            {
                "tool": "comfyui.job.retry.plan",
                "name": "Plan an owned retry",
                "required_arguments": {"job_id": _JOB_ID, "changes": {}},
                "risk": "low",
                "command": "comfyui-skill job retry --shell-secret",
            }
        ],
        "approval_actions": [
            {
                "tool": "comfyui.server.free",
                "name": "Free server memory",
                "required_arguments": {"server_id": "local", "free_memory": True},
                "risk": "high",
                "shell": "rm -rf private",
            }
        ],
        "created_at": "2026-08-03T00:00:00Z",
        "owner_id": "private-owner",
        "raw_error": "C:/private/models/secret.safetensors",
    }


def _repair_plan() -> dict[str, Any]:
    return {
        "repair_plan_id": _REPAIR_PLAN_ID,
        "plan_digest": _PLAN_DIGEST,
        "resource_uri": f"comfyui://plans/{_REPAIR_PLAN_ID}",
        "original_job_id": _JOB_ID,
        "workflow_id": "workflow_" + "e" * 32,
        "server_id": "local",
        "pinned_plan_id": "plan_" + "1" * 32,
        "pinned_revision_id": "revision_" + "f" * 32,
        "pinned_deployment_id": "deployment_" + "2" * 32,
        "normalized_changes": {"steps": 24},
        "diff": [{"path": "/steps", "before": 20, "after": 24}],
        "original_arguments_digest": "3" * 64,
        "resulting_arguments_digest": "4" * 64,
        "status": "planned",
        "created_at": "2026-08-03T00:00:00Z",
        "expires_at": "2026-08-03T00:10:00Z",
        "original_arguments_snapshot": {"prompt": "private prompt"},
        "owner_id": "private-owner",
    }


def test_phase_n_tools_use_strict_canonical_schemas_and_allowlisted_outputs() -> None:
    tools = {tool.name: tool for tool in phase_n_tools()}

    assert set(tools) == _PHASE_N_NAMES
    assert all(tool.input_schema["additionalProperties"] is False for tool in tools.values())
    assert (
        tools["comfyui.job.diagnose"]
        .input_schema["properties"]["job_id"]["pattern"]
        .startswith("^(?!.*[\\r\\n])job_")
    )
    assert tools["comfyui.job.retry.plan"].input_schema["properties"]["changes"] == {
        "type": "object",
    }
    assert tools["comfyui.job.retry.commit"].annotations.destructive_hint is False

    report = diagnostic_report_dict(_diagnostic())
    plan = repair_plan_dict(_repair_plan())
    serialized = json.dumps({"report": report, "plan": plan})

    assert set(report) == {
        "diagnostic_id",
        "registry_version",
        "subject_uri",
        "classification",
        "retryable",
        "evidence",
        "safe_actions",
        "approval_actions",
        "created_at",
    }
    assert set(report["evidence"]) == {"status", "failed_node", "events", "log_window"}
    assert set(report["safe_actions"][0]["required_arguments"]) == {"job_id", "changes"}
    assert set(report["safe_actions"][0]) == {
        "tool",
        "name",
        "required_arguments",
        "risk",
    }
    assert set(report["approval_actions"][0]) == {
        "tool",
        "name",
        "required_arguments",
        "risk",
    }
    assert set(plan) == {
        "repair_plan_id",
        "plan_digest",
        "resource_uri",
        "original_job_id",
        "workflow_id",
        "server_id",
        "pinned_plan_id",
        "pinned_revision_id",
        "pinned_deployment_id",
        "normalized_changes",
        "diff",
        "original_arguments_digest",
        "resulting_arguments_digest",
        "status",
        "created_at",
        "expires_at",
    }
    assert "original_arguments_snapshot" not in plan
    assert "owner_id" not in plan
    assert "private" not in serialized
    assert "comfyui-skill" not in serialized
    assert "rm -rf" not in serialized


class _DiagnosticService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def diagnose_job(self, job_id: str, owner_id: str) -> dict[str, Any]:
        self.calls.append(("job", job_id, owner_id))
        return _diagnostic()

    def diagnose_server(self, server_id: str, owner_id: str) -> dict[str, Any]:
        self.calls.append(("server", server_id, owner_id))
        report = _diagnostic()
        report["subject_uri"] = f"comfyui://servers/{server_id}"
        return report

    def get(self, diagnostic_id: str, owner_id: str) -> dict[str, Any]:
        self.calls.append(("get", diagnostic_id, owner_id))
        if owner_id != "owner-a" or diagnostic_id != _DIAGNOSTIC_ID:
            raise LookupError("sensitive storage detail")
        return _diagnostic()


class _RetryService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def plan(self, job_id: str, owner_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("plan", (job_id, owner_id, changes)))
        return _repair_plan()

    def commit(self, repair_plan_id: str, plan_digest: str, owner_id: str) -> dict[str, Any]:
        self.calls.append(("commit", (repair_plan_id, plan_digest, owner_id)))
        result = _repair_plan()
        result.update(
            result_job_id="job_" + "2" * 32,
            result_job_uri="comfyui://jobs/job_" + "2" * 32,
            retry_of=_JOB_ID,
            repair_plan_id=repair_plan_id,
            status="committed",
            committed_at="2026-08-03T00:01:00Z",
            raw_arguments={"prompt": "private prompt"},
        )
        return result

    def get(self, repair_plan_id: str, owner_id: str) -> dict[str, Any]:
        self.calls.append(("get", (repair_plan_id, owner_id)))
        if owner_id != "owner-a" or repair_plan_id != _REPAIR_PLAN_ID:
            raise LookupError("sensitive storage detail")
        return _repair_plan()


@pytest.mark.anyio
async def test_phase_n_tools_dispatch_authenticated_owner_and_resources_are_private(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    diagnostics = _DiagnosticService()
    retries = _RetryService()
    authorization = AuthorizationContext("owner-a", frozenset({Scope.EXECUTE}), Toolset.EXECUTION)
    server = create_server(
        tmp_path,
        authorization=authorization,
        diagnostic_service=diagnostics,
        retry_service=retries,
    )

    async with Client(server) as client:
        first_names = [tool.name for tool in (await client.list_tools()).tools]
        second_names = [tool.name for tool in (await client.list_tools()).tools]
        report = await client.call_tool("comfyui.job.diagnose", {"job_id": _JOB_ID})
        plan = await client.call_tool(
            "comfyui.job.retry.plan", {"job_id": _JOB_ID, "changes": {"steps": 24}}
        )
        committed = await client.call_tool(
            "comfyui.job.retry.commit",
            {"repair_plan_id": _REPAIR_PLAN_ID, "plan_digest": _PLAN_DIGEST},
        )
        invalid = await client.call_tool(
            "comfyui.job.diagnose", {"job_id": _JOB_ID, "raw_error": "secret"}
        )
        diagnostic_resource = await client.read_resource(f"comfyui://diagnostics/{_DIAGNOSTIC_ID}")
        plan_resource = await client.read_resource(f"comfyui://plans/{_REPAIR_PLAN_ID}")
        capabilities = await client.call_tool(
            "comfyui.capability.search", {"query": "diagnose retry", "limit": 50}
        )

    assert first_names == second_names
    assert len(first_names) <= 64
    assert _PHASE_N_NAMES - {"comfyui.server.diagnose"} <= set(first_names)
    assert "comfyui.server.diagnose" not in first_names
    assert diagnostics.calls[:2] == [
        ("job", _JOB_ID, "owner-a"),
        ("get", _DIAGNOSTIC_ID, "owner-a"),
    ]
    assert retries.calls[0] == ("plan", (_JOB_ID, "owner-a", {"steps": 24}))
    assert retries.calls[1] == ("commit", (_REPAIR_PLAN_ID, _PLAN_DIGEST, "owner-a"))
    assert report.structured_content == diagnostic_report_dict(_diagnostic())
    assert plan.structured_content == repair_plan_dict(_repair_plan())
    assert set(committed.structured_content) == {
        *set(plan.structured_content),
        "result_job_id",
        "result_job_uri",
        "retry_of",
        "committed_at",
    }
    assert invalid.is_error is True
    assert json.loads(diagnostic_resource.contents[0].text) == report.structured_content
    assert json.loads(plan_resource.contents[0].text) == plan.structured_content
    discovered = {item["name"] for item in capabilities.structured_content["items"]}
    assert {"comfyui.job.diagnose", "comfyui.job.retry.plan"} <= discovered
    assert "private" not in json.dumps(
        {
            "report": report.structured_content,
            "plan": plan.structured_content,
            "commit": committed.structured_content,
        }
    )


@pytest.mark.anyio
async def test_phase_n_resource_cross_owner_and_errors_are_redacted(tmp_path: Path) -> None:
    _project(tmp_path)
    diagnostics = _DiagnosticService()
    retries = _RetryService()
    authorization = AuthorizationContext("attacker", frozenset({Scope.EXECUTE}), Toolset.EXECUTION)
    server = create_server(
        tmp_path,
        authorization=authorization,
        diagnostic_service=diagnostics,
        retry_service=retries,
    )

    async with Client(server) as client:
        with pytest.raises(MCPError, match="Resource not found") as diagnostic_error:
            await client.read_resource(f"comfyui://diagnostics/{_DIAGNOSTIC_ID}")
        with pytest.raises(MCPError, match="Resource not found") as plan_error:
            await client.read_resource(f"comfyui://plans/{_REPAIR_PLAN_ID}")

    assert "sensitive" not in str(diagnostic_error.value)
    assert "sensitive" not in str(plan_error.value)


@pytest.mark.anyio
async def test_phase_n_surface_is_hidden_without_services_and_prompt_is_bounded(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    server = create_server(tmp_path)
    prompt_handlers = create_prompt_handlers(diagnostics_available=True)
    rendered = await prompt_handlers.get_prompt(
        None,
        type(
            "Params",
            (),
            {"name": "diagnose-failure", "arguments": {"job_id": _JOB_ID}},
        )(),
    )

    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        templates = {
            template.uri_template
            for template in (await client.list_resource_templates()).resource_templates
        }
        prompt_names = {prompt.name for prompt in (await client.list_prompts()).prompts}
        capabilities = await client.call_tool(
            "comfyui.capability.search", {"query": "diagnose retry", "limit": 50}
        )

    text = rendered.messages[0].content.text
    assert not (_PHASE_N_NAMES & names)
    assert "comfyui://diagnostics/{diagnostic_id}" not in templates
    assert "comfyui://plans/{repair_plan_id}" not in templates
    assert "diagnose-failure" not in prompt_names
    assert not (
        _PHASE_N_NAMES & {item["name"] for item in capabilities.structured_content.get("items", [])}
    )
    assert text.count("comfyui.job.diagnose") == 1
    assert text.count(f"comfyui://diagnostics/{_DIAGNOSTIC_ID}") == 0
    assert "exactly once" in text
    assert "Do not infer" in text
    assert "Do not loop" in text
    assert "raw error" in text
    assert "comfyui.job.get" not in text
