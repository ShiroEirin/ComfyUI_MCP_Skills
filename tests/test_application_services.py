"""Application-service integration contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest
from comfyui_mcp_skills.application.catalog import WorkflowCatalog
from comfyui_mcp_skills.application.execution import ExecutionService
from comfyui_mcp_skills.application.jobs import JobService
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import (
    AssetNotFound,
    IdempotencyConflict,
    ServerOffline,
    UnsafeCancel,
)
from comfyui_mcp_skills.domain.models import Asset, Job
from comfyui_mcp_skills.infrastructure.persistence.assets import FileAssetRepository
from comfyui_mcp_skills.infrastructure.persistence.runs import FileRunRepository
from comfyui_mcp_skills.infrastructure.persistence.workflows import FileWorkflowRepository


class FakeGateway:
    def __init__(self) -> None:
        self.queued: list[dict[str, Any]] = []
        self.interrupted: list[str] = []
        self.histories: dict[str, dict[str, Any]] = {}

    def queue_prompt(self, workflow: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.queued.append(workflow)
        return {"prompt_id": "prompt-1", "client_id": "client-1"}

    def get_history(self, prompt_id: str) -> dict[str, Any] | None:
        return self.histories.get(prompt_id)

    def get_queue(self) -> dict[str, Any]:
        return {"queue_running": [], "queue_pending": []}

    def ws_events(
        self,
        client_id: str,
        prompt_id: str,
        timeout_seconds: float | None = None,
        cancel_check: Callable[[], None] | None = None,
    ):
        yield {
            "type": "progress",
            "data": {"prompt_id": prompt_id, "node": "3", "value": 1, "max": 2},
        }
        yield {
            "type": "executing",
            "data": {"prompt_id": prompt_id, "node": None},
        }
    def interrupt(self, prompt_id: str = "") -> dict[str, Any]:
        self.interrupted.append(prompt_id)
        return {"success": True}

    def queue_delete(self, prompt_ids: list[str]) -> dict[str, Any]:
        return {"success": True}


def _project(tmp_path: Path) -> None:
    workflow_dir = tmp_path / "data" / "local" / "img2img"
    workflow_dir.mkdir(parents=True)
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "id": "local",
                        "name": "Local GPU",
                        "url": "http://127.0.0.1:8188",
                        "auth": "secret",
                    }
                ],
                "default_server": "local",
            }
        ),
        encoding="utf-8",
    )
    (workflow_dir / "schema.json").write_text(
        json.dumps(
            {
                "description": "Image to image",
                "enabled": True,
                "parameters": {
                    "prompt": {
                        "type": "string",
                        "required": True,
                        "node_id": "1",
                        "field": "text",
                    },
                    "image": {
                        "type": "image",
                        "required": True,
                        "node_id": "2",
                        "field": "image",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (workflow_dir / "workflow.json").write_text(
        json.dumps(
            {
                "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "old"}},
                "2": {"class_type": "LoadImage", "inputs": {"image": "old.png"}},
            }
        ),
        encoding="utf-8",
    )


def test_catalog_registry_execution_and_job_lifecycle(tmp_path: Path) -> None:
    _project(tmp_path)
    workflow_repository = FileWorkflowRepository(tmp_path)
    catalog = WorkflowCatalog(workflow_repository)
    registry = ServerRegistry(tmp_path)
    run_repository = FileRunRepository(tmp_path)
    asset_repository = FileAssetRepository(tmp_path)
    asset_repository.save(
        Asset(
            asset_id="asset_abc123",
            server_id="local",
            comfyui_ref="agent/cat.png",
            name="cat.png",
            subfolder="agent",
            media_type="image",
            mime_type="image/png",
            size_bytes=8,
            sha256="0" * 64,
        )
    )
    gateway = FakeGateway()
    gateway_factory = MagicMock(return_value=gateway)
    execution = ExecutionService(
        catalog,
        registry,
        run_repository,
        asset_repository,
        gateway_factory,
    )

    listed = catalog.list_enabled()
    server = registry.get("local")
    submitted = execution.submit(
        "local",
        "img2img",
        {"prompt": "a cat", "image": "asset_abc123"},
        idempotency_key="call-1",
    )

    assert [workflow.workflow_id for workflow in listed] == ["img2img"]
    assert server.server_id == "local"
    assert not hasattr(server, "auth")
    assert submitted.status == "submitted"
    assert gateway.queued[0]["1"]["inputs"]["text"] == "a cat"
    assert gateway.queued[0]["2"]["inputs"]["image"] == "agent/cat.png"

    duplicate = execution.submit(
        "local",
        "img2img",
        {"prompt": "a cat", "image": "asset_abc123"},
        idempotency_key="call-1",
    )
    assert duplicate.prompt_id == "prompt-1"
    with pytest.raises(IdempotencyConflict):
        execution.submit(
            "local",
            "img2img",
            {"prompt": "a dog", "image": "asset_abc123"},
            idempotency_key="call-1",
        )
    assert len(gateway.queued) == 1

    gateway.histories["prompt-1"] = {
        "status": {"completed": True, "status_str": "success"},
        "outputs": {
            "3": {
                "images": [
                    {"filename": "result.png", "subfolder": "", "type": "output"}
                ]
            }
        },
    }
    jobs = JobService(registry, run_repository, gateway_factory)
    progress: list[dict[str, Any]] = []
    completed = jobs.wait(
        "local", "prompt-1", timeout_seconds=5, progress=progress.append
    )
    cancelled = jobs.cancel("local", "prompt-1")

    assert completed.status == "completed"
    assert completed.outputs[0]["resource_uri"].startswith(
        "comfyui://outputs/local/prompt-1/"
    )
    assert cancelled.status == "completed"
    assert gateway.interrupted == []


def test_unknown_submission_is_reconciled_by_client_id(tmp_path: Path) -> None:
    _project(tmp_path)
    catalog = WorkflowCatalog(FileWorkflowRepository(tmp_path))
    registry = ServerRegistry(tmp_path)
    runs = FileRunRepository(tmp_path)
    assets = FileAssetRepository(tmp_path)
    assets.save(
        Asset(
            asset_id="asset_abc123",
            server_id="local",
            comfyui_ref="agent/cat.png",
            name="cat.png",
            subfolder="agent",
            media_type="image",
            mime_type="image/png",
            size_bytes=8,
            sha256="0" * 64,
        )
    )

    class UnknownGateway(FakeGateway):
        def __init__(self) -> None:
            super().__init__()
            self.client_id = ""
            self.calls = 0

        def queue_prompt(self, workflow: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            self.client_id = str(kwargs["client_id"])
            raise ServerOffline("response was lost")

        def get_queue(self) -> dict[str, Any]:
            return {
                "queue_running": [],
                "queue_pending": [
                    [0, "prompt-recovered", {}, {"client_id": self.client_id}]
                ],
            }

        def get_history_list(self, max_items: int = 20, offset: int = 0):
            return {}

    gateway = UnknownGateway()
    execution = ExecutionService(catalog, registry, runs, assets, lambda _config: gateway)
    arguments = {"prompt": "cat", "image": "asset_abc123"}

    with pytest.raises(ServerOffline):
        execution.submit("local", "img2img", arguments, idempotency_key="recover-1")
    recovered = execution.submit(
        "local", "img2img", arguments, idempotency_key="recover-1"
    )

    assert recovered.prompt_id == "prompt-recovered"
    assert gateway.calls == 1


def test_run_repository_does_not_regress_terminal_status(tmp_path: Path) -> None:
    runs = FileRunRepository(tmp_path)
    submitted = Job(
        prompt_id="prompt-1",
        server_id="local",
        workflow_id="flow",
        status="submitted",
    )
    runs.save(submitted)
    runs.save(
        Job(
            prompt_id="prompt-1",
            server_id="local",
            workflow_id="flow",
            status="completed",
            outputs=({"filename": "out.png"},),
        )
    )
    runs.save(
        Job(
            prompt_id="prompt-1",
            server_id="local",
            workflow_id="flow",
            status="running",
        )
    )

    persisted = runs.get("local", "prompt-1")
    assert persisted is not None
    assert persisted.status == "completed"
    assert persisted.outputs[0]["filename"] == "out.png"


def test_execution_rejects_raw_media_reference_for_owned_request(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    gateway = FakeGateway()
    execution = ExecutionService(
        WorkflowCatalog(FileWorkflowRepository(tmp_path)),
        ServerRegistry(tmp_path),
        FileRunRepository(tmp_path),
        FileAssetRepository(tmp_path),
        lambda _config: gateway,
    )

    with pytest.raises(AssetNotFound, match="authorized asset_id"):
        execution.submit(
            "local",
            "img2img",
            {"prompt": "a cat", "image": "victim/secret.png"},
            owner_id="token-attacker",
        )

    assert gateway.queued == []


def test_running_cancel_is_safely_rejected(tmp_path: Path) -> None:
    _project(tmp_path)
    runs = FileRunRepository(tmp_path)
    runs.save(Job("prompt-running", "local", "img2img", "submitted"))
    gateway = FakeGateway()
    gateway.get_queue = lambda: {
        "queue_running": [[0, "prompt-running"]],
        "queue_pending": [],
    }
    service = JobService(ServerRegistry(tmp_path), runs, lambda _config: gateway)

    with pytest.raises(UnsafeCancel):
        service.cancel("local", "prompt-running")

    assert gateway.interrupted == []


def test_job_outputs_include_gifs_and_media_metadata(tmp_path: Path) -> None:
    _project(tmp_path)
    runs = FileRunRepository(tmp_path)
    runs.save(Job("prompt-video", "local", "img2img", "submitted"))
    gateway = FakeGateway()
    gateway.histories["prompt-video"] = {
        "status": {"completed": True, "status_str": "success"},
        "outputs": {
            "9": {
                "gifs": [
                    {"filename": "clip.mp4", "subfolder": "video", "type": "output"}
                ]
            }
        },
    }
    service = JobService(ServerRegistry(tmp_path), runs, lambda _config: gateway)

    completed = service.get("local", "prompt-video")

    assert completed.outputs[0]["media_type"] == "video"
    assert completed.outputs[0]["mime_type"] == "video/mp4"


def test_wait_zero_returns_handle_and_callback_errors_propagate(tmp_path: Path) -> None:
    _project(tmp_path)
    runs = FileRunRepository(tmp_path)
    saved = Job(
        "prompt-wait",
        "local",
        "img2img",
        "submitted",
        client_id="client-wait",
    )
    runs.save(saved)
    gateway = FakeGateway()
    service = JobService(ServerRegistry(tmp_path), runs, lambda _config: gateway)

    assert service.wait("local", "prompt-wait", timeout_seconds=0) == saved
    with pytest.raises(ValueError, match="callback failed"):
        service.wait(
            "local",
            "prompt-wait",
            timeout_seconds=1,
            progress=lambda _event: (_ for _ in ()).throw(
                ValueError("callback failed")
            ),
        )
