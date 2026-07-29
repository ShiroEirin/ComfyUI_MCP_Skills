"""Authenticated Streamable HTTP application with bounded asset ingress."""

from __future__ import annotations

import hmac
import json
import logging
import re
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import anyio
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from comfyui_mcp_skills.adapters.http.security import SafeHTTPSDownloader
from comfyui_mcp_skills.adapters.mcp.server import create_server
from comfyui_mcp_skills.application.assets import AssetService
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import (
    ComfyUISkillsError,
    PayloadTooLarge,
    ServerNotFound,
)
from comfyui_mcp_skills.infrastructure.comfyui.gateway import create_gateway
from comfyui_mcp_skills.infrastructure.persistence.assets import FileAssetRepository
from comfyui_mcp_skills.observability import REQUEST_METRICS

logger = logging.getLogger(__name__)


_PRINCIPAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_ALLOWED_SCOPES = frozenset({"comfyui:execute"})
_BEARER_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/-]+={0,}$")
_MAX_BEARER_TOKEN_LENGTH = 4096
_MAX_PRINCIPAL_ID_LENGTH = 128
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _validate_tokens(tokens: object) -> dict[str, tuple[str, tuple[str, ...]]]:
    if not isinstance(tokens, dict):
        raise ValueError("tokens must be a JSON object")
    if not tokens:
        raise ValueError("Remote MCP requires at least one bearer token")
    normalized: dict[str, tuple[str, tuple[str, ...]]] = {}
    for token, config in tokens.items():
        if (
            not isinstance(token, str)
            or not token
            or len(token) > _MAX_BEARER_TOKEN_LENGTH
            or _BEARER_TOKEN_PATTERN.fullmatch(token) is None
        ):
            raise ValueError("bearer tokens must be valid non-empty RFC 6750 token values")
        if not isinstance(config, dict):
            raise ValueError("each bearer token must map to a principal configuration")
        if set(config) != {"principal_id", "scopes"}:
            raise ValueError("each principal configuration requires principal_id and scopes")
        principal_id = config["principal_id"]
        scopes = config["scopes"]
        if (
            not isinstance(principal_id, str)
            or not principal_id
            or len(principal_id) > _MAX_PRINCIPAL_ID_LENGTH
            or _PRINCIPAL_ID_PATTERN.fullmatch(principal_id) is None
        ):
            raise ValueError("principal_id must be a safe non-empty identifier")
        if (
            not isinstance(scopes, list)
            or not scopes
            or any(not isinstance(scope, str) for scope in scopes)
            or any(scope not in _ALLOWED_SCOPES for scope in scopes)
            or len(scopes) != len(set(scopes))
        ):
            raise ValueError(
                "scopes must be a unique non-empty list containing only comfyui:execute"
            )
        normalized[token] = (principal_id, tuple(scopes))
    return normalized


class StaticTokenVerifier:
    """Verify deployment-provided opaque bearer tokens without logging them."""

    def __init__(self, tokens: dict[str, dict[str, object]]) -> None:
        self._tokens = _validate_tokens(tokens)

    async def verify_token(self, token: str) -> AccessToken | None:
        for configured, (principal_id, scopes) in self._tokens.items():
            if hmac.compare_digest(token, configured):
                return AccessToken(
                    token=token,
                    client_id=principal_id,
                    scopes=list(scopes),
                )
        return None


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
        max_subscription_streams: int = 8,
        max_subscriptions_per_principal: int = 2,
    ) -> None:
        self._app = app
        self._limit = requests_per_minute
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._max_clients = 4096
        self._bearer_tokens = bearer_tokens
        self._bearer_principals = bearer_principals or {}
        self._concurrency = anyio.Semaphore(max_concurrent_requests)
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
        authorization = request.headers.get("authorization", "")
        candidate = _bearer_token(authorization)
        if candidate:
            for configured in self._bearer_tokens:
                if hmac.compare_digest(candidate, configured):
                    principal_id = self._bearer_principals.get(configured, "authenticated")
                    client = f"principal:{principal_id}"
                    break
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

    async def _acquire_concurrency_slot(
        self, client: str, is_subscription: bool
    ) -> int | None:
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

    async def _release_concurrency_slot(
        self, client: str, is_subscription: bool
    ) -> None:
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


def create_http_app(
    base_dir: Path,
    *,
    host: str,
    allowed_hosts: list[str],
    allowed_origins: list[str],
    tokens: dict[str, dict[str, object]],
    upload_root: Path,
    max_upload_bytes: int = 25 * 1024 * 1024,
    max_mcp_body_bytes: int = 1024 * 1024,
    max_fetch_body_bytes: int = 64 * 1024,
    requests_per_minute: int = 120,
    max_concurrent_requests: int = 32,
    max_subscription_streams: int = 8,
    max_subscriptions_per_principal: int = 2,
    public_mcp_url: str | None = None,
    auth_mode: str = "static",
    remote_fetch_hosts: list[str] | None = None,
):
    """Build the remote MCP app; remote mode refuses anonymous operation."""
    if auth_mode != "static":
        raise ValueError("Only static bearer-token authentication is currently implemented")
    normalized_tokens = _validate_tokens(tokens)
    if requests_per_minute <= 0:
        raise ValueError("requests_per_minute must be positive")
    if max_concurrent_requests <= 0:
        raise ValueError("max_concurrent_requests must be positive")
    if max_subscription_streams <= 0 or max_subscriptions_per_principal <= 0:
        raise ValueError("subscription concurrency limits must be positive")
    if max_mcp_body_bytes <= 0 or max_fetch_body_bytes <= 0:
        raise ValueError("request body limits must be positive")
    base_dir = base_dir.resolve()
    upload_root = upload_root.resolve()
    upload_root.mkdir(parents=True, exist_ok=True)
    verifier = StaticTokenVerifier(tokens)
    servers = ServerRegistry(base_dir)
    assets = AssetService(
        FileAssetRepository(base_dir),
        upload_roots=[upload_root],
        max_bytes=max_upload_bytes,
    )
    downloader = SafeHTTPSDownloader(
        allowed_hosts=remote_fetch_hosts or [], max_bytes=max_upload_bytes
    )

    async def upload(request: Request) -> Response:
        denied = await _authorize(request, verifier, "comfyui:execute")
        if denied is not None:
            return denied
        denied = _validate_request_origin(request, allowed_hosts, allowed_origins)
        if denied is not None:
            return denied
        owner_id = await _request_owner(request, verifier)
        server_id = request.query_params.get("server_id", "")
        purpose = request.query_params.get("purpose", "image")
        filename = request.query_params.get("filename", "")
        if not server_id or not filename or Path(filename).name != filename:
            return JSONResponse({"code": "INVALID_ARGUMENTS"}, status_code=400)
        destination = upload_root / f"upload-{uuid.uuid4().hex}-{filename}"
        size = 0
        try:
            with destination.open("xb") as handle:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > max_upload_bytes:
                        raise PayloadTooLarge(f"Upload exceeds {max_upload_bytes} bytes")
                    handle.write(chunk)
            gateway = create_gateway(servers.connection(server_id))
            asset = await anyio.to_thread.run_sync(
                lambda: assets.upload_local(
                    gateway,
                    server_id,
                    destination,
                    purpose=purpose,
                    owner_id=owner_id,
                )
            )
            return JSONResponse(asset.to_public_dict(), status_code=201)
        except ComfyUISkillsError as exc:
            return JSONResponse(exc.as_dict(), status_code=_error_status(exc))
        finally:
            destination.unlink(missing_ok=True)

    async def fetch(request: Request) -> Response:
        denied = await _authorize(request, verifier, "comfyui:execute")
        if denied is not None:
            return denied
        denied = _validate_request_origin(request, allowed_hosts, allowed_origins)
        if denied is not None:
            return denied
        owner_id = await _request_owner(request, verifier)
        try:
            body = await _read_json_body(request, max_fetch_body_bytes)
            if not isinstance(body, dict):
                raise TypeError("request body must be an object")
            if set(body) - {"server_id", "url", "purpose"}:
                raise ValueError("request body contains unexpected fields")
            server_id = body.get("server_id")
            url = body.get("url")
            purpose = body.get("purpose", "image")
            if not isinstance(server_id, str) or not server_id:
                raise TypeError("server_id must be a non-empty string")
            if not isinstance(url, str) or not url:
                raise TypeError("url must be a non-empty string")
            if not isinstance(purpose, str):
                raise TypeError("purpose must be a string")
            downloaded = await anyio.to_thread.run_sync(
                lambda: downloader.download(url, upload_root)
            )
            try:
                gateway = create_gateway(servers.connection(server_id))
                asset = await anyio.to_thread.run_sync(
                    lambda: assets.upload_local(
                        gateway,
                        server_id,
                        downloaded,
                        purpose=purpose,
                        owner_id=owner_id,
                    )
                )
            finally:
                downloaded.unlink(missing_ok=True)
            return JSONResponse(asset.to_public_dict(), status_code=201)
        except (ComfyUISkillsError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ComfyUISkillsError):
                return JSONResponse(exc.as_dict(), status_code=_error_status(exc))
            return JSONResponse({"code": "INVALID_ARGUMENTS", "message": str(exc)}, status_code=400)

    local_host = host in {"127.0.0.1", "localhost", "::1"}
    if not local_host and not public_mcp_url:
        raise ValueError("Remote binding requires a public MCP URL")
    resource_url = public_mcp_url or f"http://{host}/mcp"
    server = create_server(base_dir, upload_roots=[upload_root])
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        max_request_body_size=max_mcp_body_bytes,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        ),
        host=host,
        auth=AuthSettings.model_validate(
            {
                "issuer_url": resource_url,
                "resource_server_url": None,
                "required_scopes": ["comfyui:execute"],
            }
        ),
        token_verifier=verifier,
        custom_starlette_routes=[
            Route("/assets", upload, methods=["POST"]),
            Route("/assets/fetch", fetch, methods=["POST"]),
        ],
    )
    app.add_middleware(StrictMCP2026Middleware)
    app.add_middleware(
        RequestControlMiddleware,
        requests_per_minute=requests_per_minute,
        max_concurrent_requests=max_concurrent_requests,
        max_subscription_streams=max_subscription_streams,
        max_subscriptions_per_principal=max_subscriptions_per_principal,
        bearer_tokens=tuple(normalized_tokens),
        bearer_principals={
            token: principal_id for token, (principal_id, _scopes) in normalized_tokens.items()
        },
    )
    return app


async def _authorize(
    request: Request, verifier: StaticTokenVerifier, required_scope: str
) -> Response | None:
    token = _bearer_token(request.headers.get("authorization", ""))
    if not token:
        return JSONResponse({"code": "UNAUTHORIZED"}, status_code=401)
    access = await verifier.verify_token(token)
    if access is None:
        return JSONResponse({"code": "UNAUTHORIZED"}, status_code=401)
    if required_scope not in access.scopes:
        return JSONResponse({"code": "FORBIDDEN"}, status_code=403)
    return None


async def _request_owner(request: Request, verifier: StaticTokenVerifier) -> str:
    token = _bearer_token(request.headers["authorization"])
    access = await verifier.verify_token(token)
    if access is None:
        raise PermissionError("Authenticated token context is missing")
    return access.client_id


def _bearer_token(authorization: str) -> str:
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        return ""
    return token


def _request_id(candidate: str) -> str:
    if _REQUEST_ID_PATTERN.fullmatch(candidate) is not None:
        return candidate
    return uuid.uuid4().hex


def _validate_request_origin(
    request: Request, allowed_hosts: list[str], allowed_origins: list[str]
) -> Response | None:
    if not _matches(request.headers.get("host", ""), allowed_hosts):
        return JSONResponse({"code": "INVALID_HOST"}, status_code=403)
    origin = request.headers.get("origin")
    if origin and not _matches(origin, allowed_origins):
        return JSONResponse({"code": "INVALID_ORIGIN"}, status_code=403)
    return None


def _matches(value: str, allowed: list[str]) -> bool:
    if value in allowed:
        return True
    for candidate in allowed:
        if candidate.endswith(":*"):
            prefix = candidate[:-1]
            if value.startswith(prefix) and value[len(prefix) :].isdigit():
                return True
    return False


async def _read_json_body(request: Request, max_bytes: int) -> Any:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise PayloadTooLarge(f"Request body exceeds {max_bytes} bytes")
        except ValueError as exc:
            raise TypeError("content-length must be an integer") from exc
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > max_bytes:
            raise PayloadTooLarge(f"Request body exceeds {max_bytes} bytes")
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("request body must be valid JSON") from exc


def _error_status(exc: ComfyUISkillsError) -> int:
    if isinstance(exc, PayloadTooLarge):
        return 413
    if isinstance(exc, ServerNotFound):
        return 404
    return 400
