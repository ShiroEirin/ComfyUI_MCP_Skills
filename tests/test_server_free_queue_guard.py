"""``server.free`` must not interrupt running or queued work by default."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from comfyui_mcp_skills.application.observability import ObservationService
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import MemoryReleaseBlocked


class _Gateway:
    def __init__(
        self, queue: object = None, *, raises: Exception | None = None
    ) -> None:
        self.queue = (
            queue
            if queue is not None
            else {"queue_running": [], "queue_pending": []}
        )
        self.raises = raises
        self.free_calls: list[dict[str, bool]] = []

    def get_queue(self, *, timeout_seconds: float | None = None) -> Any:
        del timeout_seconds
        if self.raises is not None:
            raise self.raises
        return self.queue

    def free_memory(
        self, *, unload_models: bool, free_memory: bool
    ) -> dict[str, Any]:
        self.free_calls.append(
            {"unload_models": unload_models, "free_memory": free_memory}
        )
        return {"success": True}


def _service(tmp_path: Path, gateway: _Gateway) -> ObservationService:
    (tmp_path / "config.json").write_text(
        '{"default_server": "local", "servers": [{"id": "local", "name": "L", '
        '"url": "http://127.0.0.1:8188"}]}',
        encoding="utf-8",
    )
    return ObservationService(ServerRegistry(tmp_path), lambda _config: gateway)


def test_free_refuses_while_work_is_running(tmp_path: Path) -> None:
    gateway = _Gateway({"queue_running": [[1, "p1"]], "queue_pending": [[2, "p2"]]})
    service = _service(tmp_path, gateway)

    with pytest.raises(MemoryReleaseBlocked) as caught:
        service.free("local", unload_models=True, free_memory=True)

    assert caught.value.details == {
        "server_id": "local",
        "running": 1,
        "pending": 1,
        "force": True,
    }
    assert gateway.free_calls == []


def test_free_force_releases_through_live_work(tmp_path: Path) -> None:
    gateway = _Gateway({"queue_running": [[1, "p1"]], "queue_pending": []})
    service = _service(tmp_path, gateway)

    result = service.free("local", unload_models=False, free_memory=True, force=True)

    assert result["force"] is True
    assert gateway.free_calls == [{"unload_models": False, "free_memory": True}]


def test_free_releases_when_the_queue_is_idle(tmp_path: Path) -> None:
    gateway = _Gateway()
    service = _service(tmp_path, gateway)

    result = service.free("local", unload_models=True, free_memory=False)

    assert result == {
        "server_id": "local",
        "success": True,
        "unload_models": True,
        "free_memory": False,
        "force": False,
        "impact": ["loaded_models"],
    }
    assert gateway.free_calls == [{"unload_models": True, "free_memory": False}]


def test_free_refuses_when_the_queue_cannot_be_verified(tmp_path: Path) -> None:
    gateway = _Gateway(raises=RuntimeError("queue unavailable"))
    service = _service(tmp_path, gateway)

    with pytest.raises(MemoryReleaseBlocked) as caught:
        service.free("local", unload_models=True, free_memory=True)

    assert caught.value.details["reason"] == "RuntimeError"
    assert gateway.free_calls == []


def test_free_refuses_an_invalid_queue_payload(tmp_path: Path) -> None:
    gateway = _Gateway(queue=["not", "a", "dict"])
    service = _service(tmp_path, gateway)

    with pytest.raises(MemoryReleaseBlocked) as caught:
        service.free("local", unload_models=True, free_memory=False)

    assert caught.value.details["reason"] == "invalid_queue_response"
    assert gateway.free_calls == []
