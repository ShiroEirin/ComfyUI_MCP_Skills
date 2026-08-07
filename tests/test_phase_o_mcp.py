"""Focused Phase O MCP schema, projection, and owner isolation contracts."""

from __future__ import annotations

import json

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError

from comfyui_mcp_skills.adapters.mcp.admin import create_admin_server
from comfyui_mcp_skills.adapters.mcp.admin_control import (
    PHASE_O_TOOL_NAMES,
    dependency_report_dict,
    phase_o_tools,
    server_dict,
)
from comfyui_mcp_skills.application.authorization import AuthorizationContext, Scope, Toolset
from comfyui_mcp_skills.application.server_control import ServerControlService
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.sqlite_provisioning import (
    SQLiteProvisioningRepository,
)


class _Repository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def get_server(self, identity: str, owner_id: str) -> dict[str, object]:
        self.calls.append(("server", identity, owner_id))
        if owner_id != "owner-a":
            raise LookupError("private storage")
        return {
            "server_id": identity,
            "url": "https://user:secret@example.invalid/a",
            "status": "ready",
        }

    def get_bundle(self, identity: str, owner_id: str) -> dict[str, object]:
        self.calls.append(("bundle", identity, owner_id))
        raise LookupError("missing")

    def get_plan(self, identity: str, owner_id: str) -> dict[str, object]:
        self.calls.append(("plan", identity, owner_id))
        raise LookupError("missing")

    def get_approval(self, identity: str, owner_id: str) -> dict[str, object]:
        self.calls.append(("approval", identity, owner_id))
        raise LookupError("missing")

    def get_job(self, identity: str, owner_id: str) -> dict[str, object]:
        self.calls.append(("job", identity, owner_id))
        raise LookupError("missing")


def test_phase_o_tools_are_closed_and_unavailable_surfaces_are_hidden() -> None:
    tools = phase_o_tools(
        servers_available=True, config_available=False, dependencies_available=True
    )
    names = {tool.name for tool in tools}
    assert names == {
        "comfyui.admin.server.list",
        "comfyui.admin.server.inspect",
        "comfyui.admin.server.upsert",
        "comfyui.admin.server.set_enabled",
        "comfyui.admin.server.set_default",
        "comfyui.admin.server.delete",
        "comfyui.admin.dependency.inspect",
        "comfyui.admin.dependency.plan",
        "comfyui.admin.dependency.install",
        "comfyui.admin.approval.get",
        "comfyui.admin.approval.decision.plan",
        "comfyui.admin.approval.decision.commit",
        "comfyui.admin.provisioning.get",
        "comfyui.admin.provisioning.cancel",
    }
    assert names <= PHASE_O_TOOL_NAMES
    assert all(tool.input_schema["additionalProperties"] is False for tool in tools)
    install = next(tool for tool in tools if tool.name == "comfyui.admin.dependency.install")
    assert set(install.input_schema["required"]) >= {
        "plan_id",
        "plan_digest",
        "approval_id",
        "request_id",
        "confirmation",
    }
    assert (
        install.input_schema["properties"]["confirmation"]["enum"]
        == ["INSTALL APPROVED DEPENDENCIES"]
    )


def test_phase_o_projections_drop_credentials_and_manager_payloads() -> None:

    projected = server_dict(
        {
            "server_id": "srv",
            "url": "https://user:secret@example.invalid",
            "command": "install",
            "path": "C:/secret",
        }
    )
    assert projected == {"server_id": "srv"}
    report = dependency_report_dict(
        {
            "server_id": "srv",
            "requirements": [
                {
                    "name": "model",
                    "source_url": "https://u:p@example.invalid",
                    "manager_payload": {"token": "x"},
                }
            ],
            "raw_manager": "x",
        }
    )
    assert "raw_manager" not in report
    assert report["requirements"][0]["name"] == "model"
    assert "source_url" not in report["requirements"][0]
    assert "token" not in json.dumps(report)


@pytest.mark.anyio
async def test_exact_server_upsert_tool_plans_and_commits(tmp_path) -> None:
    (tmp_path / "config.json").write_text('{"servers":[]}', encoding="utf-8")
    store = SQLiteControlPlaneStore((tmp_path / "control-plane.sqlite3").resolve())
    store.initialize()
    service = ServerControlService(SQLiteProvisioningRepository(store))
    server = create_admin_server(tmp_path, enabled=True, server_control=service)

    async with Client(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert {
            "comfyui.admin.server.upsert",
            "comfyui.admin.server.set_enabled",
            "comfyui.admin.server.set_default",
            "comfyui.admin.server.delete",
        } <= listed
        planned = await client.call_tool(
            "comfyui.admin.server.upsert",
            {
                "phase": "plan",
                "server_id": "local",
                "changes": {"endpoint_url": "http://127.0.0.1:8188"},
                "expected_revision": 0,
            },
        )
        assert planned.is_error is False
        assert planned.structured_content["expected_revision"] == 0
        committed = await client.call_tool(
            "comfyui.admin.server.upsert",
            {
                "phase": "commit",
                "plan_id": planned.structured_content["plan_id"],
                "plan_digest": planned.structured_content["plan_digest"],
            },
        )
        assert committed.is_error is False
        assert committed.structured_content["server_id"] == "local"
        assert committed.structured_content["revision"] == 1
        assert len(committed.structured_content["config_digest"]) == 64


@pytest.mark.anyio
async def test_phase_o_resources_are_private_and_cross_owner_reads_are_not_found(tmp_path) -> None:
    (tmp_path / "config.json").write_text('{"servers":[]}', encoding="utf-8")
    repo = _Repository()
    server = create_admin_server(
        tmp_path,
        enabled=True,
        authorization=AuthorizationContext(
            "owner-a", frozenset({Scope.CONFIGURE, Scope.PROVISION}), Toolset.ADMIN
        ),
        provisioning_repository=repo,
    )
    async with Client(server) as client:
        templates = await client.list_resource_templates()
        assert {item.uri_template for item in templates.resource_templates} == {
            "comfyui://servers/{server_id}",
            "comfyui://config/bundles/{revision}",
            "comfyui://dependencies/plans/{plan_id}",
            "comfyui://approvals/{approval_id}",
            "comfyui://provisioning/jobs/{job_id}",
        }
        resource = await client.read_resource("comfyui://servers/srv")
        assert json.loads(resource.contents[0].text) == {
            "server_id": "srv",
            "status": "ready",
        }
    assert repo.calls == [("server", "srv", "owner-a")]

    attacker = create_admin_server(
        tmp_path,
        enabled=True,
        authorization=AuthorizationContext(
            "owner-b", frozenset({Scope.CONFIGURE, Scope.PROVISION}), Toolset.ADMIN
        ),
        provisioning_repository=repo,
    )
    async with Client(attacker) as client:
        with pytest.raises(MCPError, match="Resource not found"):
            await client.read_resource("comfyui://servers/srv")
