"""Middleware mapping of shared limit-store rejections."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from comfyui_mcp_skills.adapters.http.limits import RequestControlMiddleware


class _DenyingStore:
    def consume_rate_limit(self, mode: str, key: str, *, limit: int, window_seconds: int = 60):
        return 1

    def acquire_permit(
        self, mode: str, permit_id: str, permit_key: str, *, ttl_seconds: int, maximum: int
    ):
        return False

    def release_permit(self, mode: str, permit_id: str, permit_key: str) -> bool:
        return True

    def acquire_subscription(self, mode: str, subject: str, *, maximum: int) -> bool:
        return True

    def release_subscription(self, mode: str, subject: str) -> None:
        return None

    def prune_expired(self) -> int:
        return 0


class _ReleaseFailingStore(_DenyingStore):
    def __init__(self) -> None:
        self.failed_once = False

    def acquire_permit(
        self, mode: str, permit_id: str, permit_key: str, *, ttl_seconds: int, maximum: int
    ):
        return True

    def release_permit(self, mode: str, permit_id: str, permit_key: str) -> bool:
        if not self.failed_once:
            self.failed_once = True
            raise RuntimeError("shared release backend failed")
        return True


def _app(*, store: object) -> Starlette:
    async def ok(request):
        return JSONResponse({"ok": True})

    application = Starlette(routes=[Route("/mcp", ok, methods=["POST"])])
    application.add_middleware(
        RequestControlMiddleware,
        requests_per_minute=100,
        max_concurrent_requests=1,
        bearer_tokens=(),
        limit_mode="external",
        shared_limit_store=store,
    )
    return application


def test_shared_permit_rejection_maps_to_503() -> None:
    application = _app(store=_DenyingStore())

    with TestClient(application) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"mcp-protocol-version": "2026-07-28"},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "CONCURRENCY_LIMITED"


def test_repeated_rejections_do_not_leak_local_concurrency_slot() -> None:
    application = _app(store=_DenyingStore())

    with TestClient(application) as client:
        for _ in range(3):
            response = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"mcp-protocol-version": "2026-07-28"},
            )
            assert response.status_code == 503


def test_release_failure_still_releases_local_slot() -> None:
    application = _app(store=_ReleaseFailingStore())

    with TestClient(application) as client:
        headers = {"mcp-protocol-version": "2026-07-28"}
        with pytest.raises(RuntimeError, match="shared release backend failed"):
            client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers=headers,
            )
        second = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers=headers,
        )

    assert second.status_code == 200
