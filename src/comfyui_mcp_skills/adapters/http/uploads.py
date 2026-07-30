"""Bounded upload and remote-fetch routes for the HTTP adapter."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from comfyui_mcp_skills.adapters.http.auth import (
    StaticTokenVerifier,
    authorize,
    request_owner,
)
from comfyui_mcp_skills.adapters.http.security import SafeHTTPSDownloader
from comfyui_mcp_skills.application.assets import AssetService
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import (
    ComfyUISkillsError,
    PayloadTooLarge,
    ServerNotFound,
)
from comfyui_mcp_skills.infrastructure.comfyui.gateway import create_gateway


def create_asset_routes(
    *,
    verifier: StaticTokenVerifier,
    servers: ServerRegistry,
    assets: AssetService,
    downloader: SafeHTTPSDownloader,
    upload_root: Path,
    allowed_hosts: list[str],
    allowed_origins: list[str],
    max_upload_bytes: int,
    max_fetch_body_bytes: int,
) -> list[Route]:
    async def upload(request: Request) -> Response:
        denied = await authorize(request, verifier, "comfyui:execute")
        if denied is not None:
            return denied
        denied = _validate_request_origin(request, allowed_hosts, allowed_origins)
        if denied is not None:
            return denied
        owner_id = await request_owner(request, verifier)
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
        denied = await authorize(request, verifier, "comfyui:execute")
        if denied is not None:
            return denied
        denied = _validate_request_origin(request, allowed_hosts, allowed_origins)
        if denied is not None:
            return denied
        owner_id = await request_owner(request, verifier)
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

    return [
        Route("/assets", upload, methods=["POST"]),
        Route("/assets/fetch", fetch, methods=["POST"]),
    ]


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
