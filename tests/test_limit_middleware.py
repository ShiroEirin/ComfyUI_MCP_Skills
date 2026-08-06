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

    def acquire_subscription(
        self, mode: str, lease_id: str, subject: str, *, maximum: int, ttl_seconds: int
    ) -> bool:
        return True

    def renew_subscription(self, mode: str, lease_id: str, *, ttl_seconds: int) -> bool:
        return True

    def release_subscription(self, mode: str, lease_id: str) -> None:
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


class _RecordingStore(_DenyingStore):
    def __init__(self) -> None:
        self.acquired: list[str] = []
        self.renewed: list[str] = []
        self.released: list[str] = []

    def acquire_permit(
        self, mode: str, permit_id: str, permit_key: str, *, ttl_seconds: int, maximum: int
    ):
        return True

    def acquire_subscription(
        self, mode: str, lease_id: str, subject: str, *, maximum: int, ttl_seconds: int
    ) -> bool:
        self.acquired.append(lease_id)
        return True

    def renew_subscription(self, mode: str, lease_id: str, *, ttl_seconds: int) -> bool:
        self.renewed.append(lease_id)
        return True

    def release_subscription(self, mode: str, lease_id: str) -> None:
        self.released.append(lease_id)


def _app(*, store: object, renew_interval: float = 120.0) -> Starlette:
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
        subscription_renew_interval_seconds=renew_interval,
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


class _PermitDenyingRecordingStore(_RecordingStore):
    def acquire_permit(
        self, mode: str, permit_id: str, permit_key: str, *, ttl_seconds: int, maximum: int
    ):
        return False


def test_subscription_lease_released_when_shared_permit_rejected() -> None:
    store = _PermitDenyingRecordingStore()
    application = _app(store=store)

    with TestClient(application) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "subscriptions/listen"},
            headers={
                "mcp-protocol-version": "2026-07-28",
                "mcp-method": "subscriptions/listen",
            },
        )

    assert response.status_code == 503
    assert len(store.acquired) == 1
    assert store.released == store.acquired
    assert store.renewed == []


class _PermitRaisingStore(_RecordingStore):
    def acquire_permit(
        self, mode: str, permit_id: str, permit_key: str, *, ttl_seconds: int, maximum: int
    ):
        from comfyui_mcp_skills.application.shared_limits import SharedLimitsUnavailable

        raise SharedLimitsUnavailable("shared permit backend failed")


def test_subscription_lease_released_when_shared_permit_backend_fails() -> None:
    store = _PermitRaisingStore()
    application = _app(store=store)

    with TestClient(application) as client:
        with pytest.raises(RuntimeError, match="shared permit backend failed"):
            client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "subscriptions/listen"},
                headers={
                    "mcp-protocol-version": "2026-07-28",
                    "mcp-method": "subscriptions/listen",
                },
            )

    assert len(store.acquired) == 1
    assert store.released == store.acquired


def test_long_subscription_stream_renews_lease_and_releases_on_close() -> None:
    import anyio
    from starlette.responses import StreamingResponse

    store = _RecordingStore()

    async def stream(_request):
        async def body():
            for _ in range(4):
                yield b'{"jsonrpc":"2.0","id":1,"result":{}}\n'
                await anyio.sleep(0.2)

        return StreamingResponse(body(), media_type="text/event-stream")

    application = Starlette(routes=[Route("/mcp", stream, methods=["POST"])])
    application.add_middleware(
        RequestControlMiddleware,
        requests_per_minute=100,
        max_concurrent_requests=1,
        bearer_tokens=(),
        limit_mode="external",
        shared_limit_store=store,
        subscription_renew_interval_seconds=0.1,
    )

    with TestClient(application) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "subscriptions/listen"},
            headers={"mcp-protocol-version": "2026-07-28", "mcp-method": "subscriptions/listen"},
        )

    assert response.status_code == 200
    assert len(store.acquired) == 1
    assert len(store.renewed) >= 1
    assert store.released == store.acquired
