"""Authenticated Streamable HTTP application with bounded asset ingress."""

from __future__ import annotations

import hashlib
import hmac
import json
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


class StaticTokenVerifier:
    """Verify deployment-provided opaque bearer tokens without logging them."""

    def __init__(self, tokens: dict[str, list[str]]) -> None:
        self._tokens = {token: tuple(scopes) for token, scopes in tokens.items()}

    async def verify_token(self, token: str) -> AccessToken | None:
        for configured, scopes in self._tokens.items():
            if hmac.compare_digest(token, configured):
                principal = hashlib.sha256(configured.encode("utf-8")).hexdigest()[:24]
                return AccessToken(
                    token=token,
                    client_id=f"token-{principal}",
                    scopes=list(scopes),
                )
        return None


class RequestControlMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: Any,
        *,
        requests_per_minute: int,
        max_concurrent_requests: int,
        bearer_tokens: tuple[str, ...],
    ) -> None:
        super().__init__(app)
        self._limit = requests_per_minute
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._max_clients = 4096
        self._bearer_tokens = bearer_tokens
        self._concurrency = anyio.Semaphore(max_concurrent_requests)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        client = request.client.host if request.client else "unknown"
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            candidate = authorization[7:]
            if any(
                hmac.compare_digest(candidate, configured)
                for configured in self._bearer_tokens
            ):
                client = "token:" + hashlib.sha256(
                    candidate.encode("utf-8")
                ).hexdigest()[:24]
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
            return JSONResponse(
                {"code": "RATE_LIMITED", "request_id": request_id},
                status_code=429,
                headers={"retry-after": "60", "x-request-id": request_id},
            )
        recent.append(now)
        async with self._concurrency:
            response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response


def create_http_app(
    base_dir: Path,
    *,
    host: str,
    allowed_hosts: list[str],
    allowed_origins: list[str],
    tokens: dict[str, list[str]],
    upload_root: Path,
    max_upload_bytes: int = 25 * 1024 * 1024,
    max_mcp_body_bytes: int = 1024 * 1024,
    max_fetch_body_bytes: int = 64 * 1024,
    requests_per_minute: int = 120,
    max_concurrent_requests: int = 32,
    public_mcp_url: str | None = None,
    oauth_issuer_url: str | None = None,
    remote_fetch_hosts: list[str] | None = None,
):
    """Build the remote MCP app; remote mode refuses anonymous operation."""
    if not tokens or any(not token for token in tokens):
        raise ValueError("Remote MCP requires at least one non-empty bearer token")
    if requests_per_minute <= 0:
        raise ValueError("requests_per_minute must be positive")
    if max_concurrent_requests <= 0:
        raise ValueError("max_concurrent_requests must be positive")
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
                        raise PayloadTooLarge(
                            f"Upload exceeds {max_upload_bytes} bytes"
                        )
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
    if not local_host and (not public_mcp_url or not oauth_issuer_url):
        raise ValueError("Remote binding requires public MCP and OAuth issuer URLs")
    resource_url = public_mcp_url or f"http://{host}/mcp"
    issuer_url = oauth_issuer_url or resource_url
    server = create_server(base_dir, upload_roots=[upload_root])
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=False,
        max_request_body_size=max_mcp_body_bytes,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        ),
        host=host,
        auth=AuthSettings(
            issuer_url=issuer_url,
            resource_server_url=resource_url,
            required_scopes=["comfyui:execute"],
        ),
        token_verifier=verifier,
        custom_starlette_routes=[
            Route("/assets", upload, methods=["POST"]),
            Route("/assets/fetch", fetch, methods=["POST"]),
        ],
    )
    app.add_middleware(
        RequestControlMiddleware,
        requests_per_minute=requests_per_minute,
        max_concurrent_requests=max_concurrent_requests,
        bearer_tokens=tuple(tokens),
    )
    return app


async def _authorize(
    request: Request, verifier: StaticTokenVerifier, required_scope: str
) -> Response | None:
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        return JSONResponse({"code": "UNAUTHORIZED"}, status_code=401)
    access = await verifier.verify_token(authorization.removeprefix("Bearer "))
    if access is None:
        return JSONResponse({"code": "UNAUTHORIZED"}, status_code=401)
    if required_scope not in access.scopes:
        return JSONResponse({"code": "FORBIDDEN"}, status_code=403)
    return None


async def _request_owner(request: Request, verifier: StaticTokenVerifier) -> str:
    authorization = request.headers["authorization"]
    access = await verifier.verify_token(authorization.removeprefix("Bearer "))
    if access is None:
        raise PermissionError("Authenticated token context is missing")
    return access.client_id


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
