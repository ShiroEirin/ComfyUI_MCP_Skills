"""Application repository port contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from comfyui_mcp_skills.application.assets import AssetService
from comfyui_mcp_skills.application.catalog import WorkflowCatalog
from comfyui_mcp_skills.application.execution import ExecutionService
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.models import Asset, Job, Workflow


class MemoryWorkflowRepository:
    def __init__(self, workflow: Workflow) -> None:
        self.workflow = workflow

    def list(self) -> list[Workflow]:
        return [self.workflow]

    def get(self, server_id: str, workflow_id: str) -> Workflow | None:
        if (server_id, workflow_id) == (
            self.workflow.server_id,
            self.workflow.workflow_id,
        ):
            return self.workflow
        return None


class MemoryAssetRepository:
    def __init__(self, asset: Asset) -> None:
        self.assets = {asset.asset_id: asset}

    def save(self, asset: Asset) -> None:
        self.assets[asset.asset_id] = asset

    def get(self, asset_id: str) -> Asset | None:
        return self.assets.get(asset_id)


class MemoryRunRepository:
    def __init__(self) -> None:
        self.jobs: dict[tuple[str, str], Job] = {}

    def claim(
        self,
        server_id: str,
        workflow_id: str,
        idempotency_key: str,
        arguments: dict[str, Any],
        owner_id: str = "",
        client_id: str = "",
    ) -> str | None:
        return ""

    def get_claim(self, server_id: str, key: str, owner_id: str = "") -> dict[str, Any] | None:
        return None

    def release_claim(
        self,
        server_id: str,
        key: str,
        request_digest: str,
        lease_token: str,
        owner_id: str = "",
    ) -> None:
        return None

    def mark_submission_unknown(
        self,
        server_id: str,
        key: str,
        lease_token: str,
        owner_id: str = "",
    ) -> None:
        return None

    def request_digest(self, workflow_id: str, arguments: dict[str, Any]) -> str:
        return "memory-digest"

    def admit(self, server_id: str) -> str:
        return ""

    @staticmethod
    def release_admission(admission_id: str) -> None:
        return None

    def save(self, job: Job, *, lease_token: str = "") -> None:
        self.jobs[(job.server_id, job.prompt_id)] = job

    def get(self, server_id: str, prompt_id: str) -> Job | None:
        return self.jobs.get((server_id, prompt_id))

    def get_by_idempotency(self, server_id: str, key: str, owner_id: str = "") -> Job | None:
        return None


class MemoryGateway:
    def queue_prompt(self, workflow: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return {"prompt_id": "memory-prompt"}


def test_repository_backed_services_accept_memory_port_implementations(
    tmp_path: Path,
) -> None:
    (tmp_path / "config.json").write_text(
        json.dumps({"servers": [{"id": "local", "url": "http://127.0.0.1:8188"}]}),
        encoding="utf-8",
    )
    workflow = Workflow(
        server_id="local",
        workflow_id="memory",
        description="In-memory workflow",
        parameters={},
        graph={},
    )
    asset = Asset(
        asset_id="asset_memory",
        server_id="local",
        comfyui_ref="memory.png",
        name="memory.png",
        subfolder="",
        media_type="image",
        mime_type="image/png",
        size_bytes=8,
        sha256="0" * 64,
    )
    workflows = MemoryWorkflowRepository(workflow)
    assets = MemoryAssetRepository(asset)
    runs = MemoryRunRepository()
    catalog = WorkflowCatalog(workflows)
    asset_service = AssetService(assets, upload_roots=[tmp_path])
    execution = ExecutionService(
        catalog,
        ServerRegistry(tmp_path),
        runs,
        assets,
        lambda _config: MemoryGateway(),
    )

    submitted = execution.submit("local", "memory", {})

    assert catalog.list_enabled() == [workflow]
    assert asset_service.get("asset_memory") == asset
    assert submitted.prompt_id == "memory-prompt"
    assert runs.get("local", "memory-prompt") == submitted
