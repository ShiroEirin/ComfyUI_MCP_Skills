"""HTTP protocol, rate, and concurrency controls."""

from __future__ import annotations

import hmac
import logging
import re
import time
import uuid
from collections import defaultdict, deque

import anyio
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from comfyui_mcp_skills.adapters.http.auth import TokenVerifier, bearer_token
from comfyui_mcp_skills.observability import REQUEST_METRICS

logger = logging.getLogger(__name__)

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class StrictMCP2026Middleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method == "POST" and request.url.path == "/mcp":
            if not request.headers.get("mcp-protocol-version"):
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": -32020,
                            "message": "HeaderMismatch: MCP-Protocol-Version is required",
                        },
                    },
                    status_code=400,
                )
        return await call_next(request)


class RequestControlMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        requests_per_minute: int,
        max_concurrent_requests: int,
        bearer_tokens: tuple[str, ...],
        bearer_principals: dict[str, str] | None = None,
        token_verifier: TokenVerifier | None = None,
        max_subscription_streams: int = 8,
        max_subscriptions_per_principal: int = 2,
    ) -> None:
        self._app = app
        self._limit = requests_per_minute
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._max_clients = 4096
        self._bearer_tokens = bearer_tokens
        self._bearer_principals = bearer_principals or {}
        self._token_verifier = token_verifier
        self._concurrency = anyio.Semaphore(max_concurrent_requests)
        self._introspection_concurrency = anyio.Semaphore(max_concurrent_requests)
        self._subscription_concurrency = anyio.Semaphore(max_subscription_streams)
        self._max_subscriptions_per_principal = max_subscriptions_per_principal
        self._active_subscriptions: dict[str, int] = {}
        self._subscription_lock = anyio.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        started_at = time.monotonic()
        request_id = _request_id(request.headers.get("x-request-id", ""))
        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        if client not in self._requests and len(self._requests) >= self._max_clients:
            for key, entries in list(self._requests.items()):
                while entries and entries[0] <= now - 60:
                    entries.popleft()
                if not entries:
                    self._requests.pop(key, None)
            if len(self._requests) >= self._max_clients:
                oldest = min(
                    self._requests,
                    key=lambda key: self._requests[key][-1],
                )
                self._requests.pop(oldest, None)
        recent = self._requests[client]
        while recent and recent[0] <= now - 60:
            recent.popleft()
        if len(recent) >= self._limit:
            response = JSONResponse(
                {"code": "RATE_LIMITED", "request_id": request_id},
                status_code=429,
                headers={"retry-after": "60", "x-request-id": request_id},
            )
            REQUEST_METRICS.record(
                status_code=429,
                duration_seconds=time.monotonic() - started_at,
            )
            await response(scope, receive, send)
            return
        recent.append(now)
        authorization = request.headers.get("authorization", "")
        candidate = bearer_token(authorization)
        if candidate:
            for configured in self._bearer_tokens:
                if hmac.compare_digest(candidate, configured):
                    principal_id = self._bearer_principals.get(configured, "authenticated")
                    client = f"principal:{principal_id}"
                    break
            if self._token_verifier is not None:
                async with self._introspection_concurrency:
                    access = await self._token_verifier.verify_token(candidate)
                if access is not None:
                    client = f"principal:{access.client_id}"
                    scope.setdefault("state", {})["access_token"] = access

        is_subscription = (
            request.method == "POST"
            and request.url.path == "/mcp"
            and request.headers.get("mcp-method") == "subscriptions/listen"
        )
        limit_status = await self._acquire_concurrency_slot(client, is_subscription)
        if limit_status is not None:
            response = JSONResponse(
                {"code": "CONCURRENCY_LIMITED", "request_id": request_id},
                status_code=limit_status,
                headers={"retry-after": "1", "x-request-id": request_id},
            )
            REQUEST_METRICS.record(
                status_code=limit_status,
                duration_seconds=time.monotonic() - started_at,
            )
            await response(scope, receive, send)
            return

        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode("latin-1")),
                ]
            await send(message)

        try:
            await self._app(scope, receive, send_with_request_id)
        finally:
            await self._release_concurrency_slot(client, is_subscription)
            duration = time.monotonic() - started_at
            REQUEST_METRICS.record(status_code=status_code, duration_seconds=duration)
            logger.info(
                "http_request_complete",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": status_code,
                    "duration_ms": round(duration * 1000, 3),
                    "client_id": client,
                },
            )

    async def _acquire_concurrency_slot(self, client: str, is_subscription: bool) -> int | None:
        if not is_subscription:
            try:
                self._concurrency.acquire_nowait()
            except anyio.WouldBlock:
                return 503
            return None

        try:
            self._subscription_concurrency.acquire_nowait()
        except anyio.WouldBlock:
            return 503
        try:
            async with self._subscription_lock:
                active = self._active_subscriptions.get(client, 0)
                if active >= self._max_subscriptions_per_principal:
                    self._subscription_concurrency.release()
                    return 429
                self._active_subscriptions[client] = active + 1
        except BaseException:
            self._subscription_concurrency.release()
            raise
        return None

    async def _release_concurrency_slot(self, client: str, is_subscription: bool) -> None:
        if not is_subscription:
            self._concurrency.release()
            return
        with anyio.CancelScope(shield=True):
            async with self._subscription_lock:
                remaining = self._active_subscriptions[client] - 1
                if remaining:
                    self._active_subscriptions[client] = remaining
                else:
                    self._active_subscriptions.pop(client)
            self._subscription_concurrency.release()


def _request_id(candidate: str) -> str:
    if _REQUEST_ID_PATTERN.fullmatch(candidate) is not None:
        return candidate
    return uuid.uuid4().hex
