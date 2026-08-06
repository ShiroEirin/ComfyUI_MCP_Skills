"""Phase P explicit runtime control contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from comfyui_mcp_skills.adapters.mcp.server import create_server
from comfyui_mcp_skills.application.authorization import AuthorizationContext, Scope, Toolset
from comfyui_mcp_skills.application.runtime_control import RuntimeControlService
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.models import Job


class _Runs:
    def __init__(self, jobs: list[Job]) -> None:
        self.jobs = {(job.server_id, job.prompt_id): job for job in jobs}

    def get(self, server_id: str, prompt_id: str) -> Job | None:
        return self.jobs.get((server_id, prompt_id))

    def list_jobs(self, owner_id: str, **_filters: Any) -> list[dict[str, str]]:
        return [
            {
                "job_id": job.job_id,
                "prompt_id": job.prompt_id,
                "server_id": job.server_id,
                "workflow_id": job.workflow_id,
                "status": job.status,
            }
            for job in self.jobs.values()
            if job.owner_id == owner_id
        ]


class _Gateway:
    def __init__(self) -> None:
        self.deleted: list[list[str]] = []
        self.cleared = 0
        self.interrupted = 0
        self.queue: dict[str, Any] = {
            "queue_running": [[0, "prompt-running", {}]],
            "queue_pending": [
                [1, "prompt-owned", {}],
                [2, "prompt-other", {}],
            ],
        }

    def get_queue(self) -> dict[str, Any]:
        return self.queue

    def queue_delete(self, prompt_ids: list[str]) -> dict[str, Any]:
        self.deleted.append(prompt_ids)
        return {"success": True}

    def queue_clear(self) -> dict[str, Any]:
        self.cleared += 1
        return {"success": True}

    def interrupt(self, _prompt_id: str = "") -> dict[str, Any]:
        self.interrupted += 1
        return {"success": True}


def _service(tmp_path: Path) -> tuple[RuntimeControlService, _Gateway]:
    (tmp_path / "config.json").write_text(
        json.dumps({"servers": [{"id": "local", "url": "http://127.0.0.1:8188"}]}),
        encoding="utf-8",
    )
    jobs = [
        Job(
            "prompt-owned",
            "local",
            "portrait",
            "queued",
            owner_id="owner-a",
            job_id="job_" + "1" * 64,
        ),
        Job(
            "prompt-running",
            "local",
            "portrait",
            "running",
            owner_id="owner-a",
            job_id="job_" + "2" * 64,
        ),
        Job(
            "prompt-other",
            "local",
            "portrait",
            "queued",
            owner_id="owner-b",
            job_id="job_" + "3" * 64,
        ),
    ]
    gateway = _Gateway()
    service = RuntimeControlService(
        ServerRegistry(tmp_path),
        _Runs(jobs),
        lambda _config: gateway,  # type: ignore[arg-type]
    )
    return service, gateway


def test_queue_remove_previews_before_owner_safe_execution(tmp_path: Path) -> None:
    service, gateway = _service(tmp_path)

    preview = service.queue_remove("local", ["prompt-owned"], "owner-a", execute=False)
    assert preview["executed"] is False
    assert preview["affected_jobs"][0]["job_id"] == "job_" + "1" * 64
    assert gateway.deleted == []
    executed = service.queue_remove("local", ["prompt-owned"], "owner-a", execute=True)
    assert executed["executed"] is True
    assert gateway.deleted == [["prompt-owned"]]
    with pytest.raises(PermissionError, match="Cross-owner"):
        service.queue_remove("local", ["prompt-other"], "owner-a", execute=False)


def test_global_controls_report_impact_and_restart_requirement(tmp_path: Path) -> None:
    service, gateway = _service(tmp_path)

    gateway.queue["queue_pending"] = [[1, "prompt-owned", {}]]
    preview = service.queue_clear("local", "owner-a", execute=False)
    assert {item["prompt_id"] for item in preview["affected_jobs"]} == {"prompt-owned"}
    assert gateway.cleared == 0
    interrupted = service.interrupt("local", "owner-a", execute=True, allow_cross_owner=True)
    assert interrupted["affected_prompt_ids"] == ["prompt-running"]
    assert gateway.interrupted == 1
    restart = service.restart_plan("local", "owner-a")
    assert restart["approval_required"] is True
    assert restart["impact_coverage"] == "owner_jobs"
    assert restart["runtime_controller_available"] is False
    assert "Global impact" in restart["operation_requirement"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_operations_mcp_exposes_runtime_controls_and_restart_requirement(
    tmp_path: Path,
) -> None:
    (tmp_path / "config.json").write_text(
        json.dumps({"servers": [{"id": "local", "url": "http://127.0.0.1:8188"}]}),
        encoding="utf-8",
    )
    gateway = _Gateway()
    server = create_server(
        tmp_path,
        gateway_factory=lambda _config: gateway,
        authorization=AuthorizationContext(
            "operator-a", frozenset({Scope.OBSERVE, Scope.OPERATE}), Toolset.OPERATIONS
        ),
    )
    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert {
            "comfyui.queue.remove",
            "comfyui.queue.clear",
            "comfyui.server.interrupt",
            "comfyui.runtime.restart.plan",
        } <= names
        cleared = await client.call_tool(
            "comfyui.queue.clear", {"server_id": "local", "execute": True}
        )
        interrupted = await client.call_tool(
            "comfyui.server.interrupt", {"server_id": "local", "execute": True}
        )
        result = await client.call_tool("comfyui.runtime.restart.plan", {"server_id": "local"})
    assert result.structured_content["approval_required"] is True
    assert result.structured_content["runtime_controller_available"] is False
    assert cleared.structured_content["executed"] is True
    assert interrupted.structured_content["executed"] is True
