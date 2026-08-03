"""Application-service integration contracts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any
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
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.runs import FileRunRepository
from comfyui_mcp_skills.infrastructure.persistence.sqlite_assets import SQLiteAssetRepository
from comfyui_mcp_skills.infrastructure.persistence.sqlite_runs import SQLiteRunRepository
from comfyui_mcp_skills.infrastructure.persistence.workflows import FileWorkflowRepository


class FakeGateway:
    def __init__(self) -> None:
        self.queued: list[dict[str, Any]] = []
        self.interrupted: list[str] = []
        self.histories: dict[str, dict[str, Any]] = {}

    def queue_prompt(self, workflow: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.queued.append(workflow)
        return {"prompt_id": "prompt-1", "client_id": "client-1"}

    def get_history(
        self, prompt_id: str, timeout_seconds: float | None = None
    ) -> dict[str, Any] | None:
        return self.histories.get(prompt_id)

    def get_queue(self, timeout_seconds: float | None = None) -> dict[str, Any]:
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
            "3": {"images": [{"filename": "result.png", "subfolder": "", "type": "output"}]}
        },
    }
    jobs = JobService(registry, run_repository, gateway_factory)
    progress: list[dict[str, Any]] = []
    completed = jobs.wait("local", "prompt-1", timeout_seconds=5, progress=progress.append)
    cancelled = jobs.cancel("local", "prompt-1")

    assert completed.status == "completed"
    assert completed.outputs[0]["resource_uri"] == "comfyui://outputs/local/prompt-1/0"
    assert completed.outputs[0]["legacy_uri"] == completed.outputs[0]["resource_uri"]
    assert completed.outputs[0]["canonical_uri"].startswith("comfyui://artifacts/artifact_")
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

        def get_queue(self, timeout_seconds: float | None = None) -> dict[str, Any]:
            return {
                "queue_running": [],
                "queue_pending": [[0, "prompt-recovered", {}, {"client_id": self.client_id}]],
            }

        def get_history_list(self, max_items: int = 20, offset: int = 0):
            return {}

    gateway = UnknownGateway()
    execution = ExecutionService(catalog, registry, runs, assets, lambda _config: gateway)
    arguments = {"prompt": "cat", "image": "asset_abc123"}

    with pytest.raises(ServerOffline):
        execution.submit("local", "img2img", arguments, idempotency_key="recover-1")
    recovered = execution.submit("local", "img2img", arguments, idempotency_key="recover-1")

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


def test_run_repository_does_not_replace_error_with_completed(tmp_path: Path) -> None:
    runs = FileRunRepository(tmp_path)
    runs.save(
        Job(
            prompt_id="prompt-error",
            server_id="local",
            workflow_id="flow",
            status="error",
            error="execution failed",
        )
    )
    runs.save(
        Job(
            prompt_id="prompt-error",
            server_id="local",
            workflow_id="flow",
            status="completed",
            outputs=({"filename": "late.png"},),
        )
    )

    persisted = runs.get("local", "prompt-error")
    assert persisted is not None
    assert persisted.status == "error"
    assert persisted.error == "execution failed"


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
            "9": {"gifs": [{"filename": "clip.mp4", "subfolder": "video", "type": "output"}]}
        },
    }
    service = JobService(ServerRegistry(tmp_path), runs, lambda _config: gateway)

    completed = service.get("local", "prompt-video")

    assert completed.outputs[0]["media_type"] == "video"
    assert completed.outputs[0]["mime_type"] == "video/mp4"


def test_job_outputs_include_video_key_and_media_metadata(tmp_path: Path) -> None:
    _project(tmp_path)
    runs = FileRunRepository(tmp_path)
    runs.save(Job("prompt-video-key", "local", "img2img", "submitted"))
    gateway = FakeGateway()
    gateway.histories["prompt-video-key"] = {
        "status": {"completed": True, "status_str": "success"},
        "outputs": {
            "10": {"video": [{"filename": "render.webm", "subfolder": "video", "type": "output"}]}
        },
    }
    service = JobService(ServerRegistry(tmp_path), runs, lambda _config: gateway)

    completed = service.get("local", "prompt-video-key")

    output = completed.outputs[0]
    assert output["filename"] == "render.webm"
    assert output["subfolder"] == "video"
    assert output["type"] == "output"
    assert output["storage_type"] == "output"
    assert output["upstream_node_id"] == "10"
    assert output["output_key"] == "video"
    assert output["upstream_output_index"] == 0
    assert output["legacy_index"] == 0
    assert output["media_type"] == "video"
    assert output["mime_type"] == "video/webm"
    assert output["resource_uri"] == "comfyui://outputs/local/prompt-video-key/0"
    assert output["legacy_uri"] == output["resource_uri"]
    assert output["canonical_uri"].startswith("comfyui://artifacts/artifact_")


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
            progress=lambda _event: (_ for _ in ()).throw(ValueError("callback failed")),
        )


def test_sqlite_asset_repository_round_trips_without_overwrite(tmp_path: Path) -> None:
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()
    repository = SQLiteAssetRepository(store)
    asset = Asset(
        "asset_" + "a" * 32,
        "local",
        "agent/assets/cat.png",
        "cat.png",
        "agent/assets",
        "image",
        "image/png",
        3,
        "b" * 64,
        "owner",
        "2026-07-30T00:00:00+00:00",
    )

    repository.save(asset)

    assert repository.get(asset.asset_id) == asset
    with pytest.raises(sqlite3.IntegrityError):
        repository.save(
            Asset(
                asset.asset_id,
                "other",
                asset.comfyui_ref,
                asset.name,
                asset.subfolder,
                asset.media_type,
                asset.mime_type,
                asset.size_bytes,
                asset.sha256,
                asset.owner_id,
                asset.created_at,
            )
        )


def test_sqlite_run_repository_claim_submit_and_lookup_are_atomic(tmp_path: Path) -> None:
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()
    runs = SQLiteRunRepository(store)
    arguments = {"prompt": "cat"}

    lease = runs.claim("local", "portrait", "request-1", arguments, "owner", "client")
    assert lease
    assert runs.claim("local", "portrait", "request-1", arguments, "owner", "client") is None

    job = Job(
        "prompt-1",
        "local",
        "portrait",
        "submitted",
        idempotency_key="request-1",
        client_id="client",
        request_digest=runs.request_digest("portrait", arguments),
        owner_id="owner",
    )
    runs.save(job, lease_token=str(lease))

    assert runs.get("local", "prompt-1") == job
    assert runs.get_by_idempotency("local", "request-1", "owner") == job
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM execution_attempts").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM idempotency_records").fetchone() == (1,)


def test_sqlite_run_repository_preserves_claim_and_terminal_state(tmp_path: Path) -> None:
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()
    runs = SQLiteRunRepository(store)
    arguments = {"prompt": "cat"}
    digest = runs.request_digest("portrait", arguments)
    lease = runs.claim("local", "portrait", "request-1", arguments, "owner", "client")
    assert lease
    claim = runs.get_claim("local", "request-1", "owner")
    assert claim is not None
    assert claim["workflow_id"] == "portrait"

    submitted = Job(
        "prompt-1",
        "local",
        "portrait",
        "submitted",
        idempotency_key="request-1",
        client_id="client",
        request_digest=digest,
        owner_id="owner",
    )
    runs.save(submitted, lease_token=str(lease))
    completed = Job(
        "prompt-1",
        "local",
        "portrait",
        "completed",
        outputs=(
            {
                "filename": "first.png",
                "subfolder": "",
                "type": "output",
                "media_type": "image",
                "mime_type": "image/png",
                "resource_uri": "comfyui://outputs/local/prompt-1/0",
            },
        ),
        idempotency_key="request-1",
        client_id="client",
        request_digest=digest,
        owner_id="owner",
    )
    runs.save(completed)
    runs.save(submitted)

    assert runs.get("local", "prompt-1") == completed
    assert runs.get_by_idempotency("local", "request-1", "owner") == completed


def test_sqlite_run_repository_reclaims_expired_claim_and_ignores_empty_unknown(
    tmp_path: Path,
) -> None:
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()
    runs = SQLiteRunRepository(store)
    arguments = {"prompt": "cat"}
    first = runs.claim("local", "portrait", "request-1", arguments, "owner", "client")
    assert first
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE idempotency_records SET expires_at = '2000-01-01T00:00:00+00:00' "
            "WHERE owner_id = 'owner' AND key = 'request-1'"
        )

    second = runs.claim("local", "portrait", "request-1", arguments, "owner", "client")
    assert second and second != first
    runs.mark_submission_unknown("local", "", "", "owner")


def test_sqlite_run_repository_recovers_unknown_submission(tmp_path: Path) -> None:
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()
    runs = SQLiteRunRepository(store)
    arguments = {"prompt": "cat"}
    digest = runs.request_digest("portrait", arguments)
    lease = runs.claim("local", "portrait", "request-1", arguments, "owner", "client")
    assert lease
    runs.mark_submission_unknown("local", "request-1", str(lease), "owner")

    recovered = Job(
        "prompt-1",
        "local",
        "portrait",
        "submitted",
        idempotency_key="request-1",
        client_id="client",
        request_digest=digest,
        owner_id="owner",
    )
    runs.save(recovered, lease_token=str(lease))

    assert runs.get_by_idempotency("local", "request-1", "owner") == recovered


def test_sqlite_run_repository_rejects_cross_owner_prompt_collision(tmp_path: Path) -> None:
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()
    runs = SQLiteRunRepository(store)
    runs.save(Job("shared", "local", "portrait", "completed", owner_id="victim"))

    with pytest.raises(RuntimeError, match="identity"):
        runs.save(Job("shared", "local", "portrait", "running", owner_id="attacker"))

    assert runs.get("local", "shared") == Job(
        "shared", "local", "portrait", "completed", owner_id="victim"
    )
