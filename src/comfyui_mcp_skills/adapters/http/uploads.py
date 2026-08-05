"""Bounded upload and remote-fetch routes for the HTTP adapter."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from comfyui_mcp_skills.adapters.http.auth import TokenVerifier, authorize, request_owner
from comfyui_mcp_skills.adapters.http.security import SafeHTTPSDownloader
from comfyui_mcp_skills.application.assets import AssetService
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import (
    ComfyUISkillsError,
    PayloadTooLarge,
    ServerNotFound,
)
from comfyui_mcp_skills.domain.identifiers import validate_identifier
from comfyui_mcp_skills.infrastructure.comfyui.gateway import create_gateway

_SAFE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,254}\Z")
_PURPOSES = frozenset({"image", "mask", "audio", "video"})


def create_asset_routes(
    *,
    verifier: TokenVerifier,
    servers: ServerRegistry,
    assets: AssetService,
    downloader: SafeHTTPSDownloader,
    upload_root: Path,
    allowed_hosts: list[str],
    allowed_origins: list[str],
    max_upload_bytes: int,
    max_fetch_body_bytes: int,
    server_connection: Callable[[str, str], dict[str, Any] | None] | None = None,
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
        try:
            server_id = validate_identifier(server_id, field="server_id")
        except ValueError:
            return JSONResponse({"code": "INVALID_ARGUMENTS"}, status_code=400)
        if (
            not filename
            or Path(filename).name != filename
            or _SAFE_FILENAME.fullmatch(filename) is None
            or purpose not in _PURPOSES
        ):
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
            connection = server_connection(owner_id, server_id) if server_connection else None
            gateway = create_gateway(
                servers.connection(server_id) if connection is None else connection
            )
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
            server_id = validate_identifier(server_id, field="server_id")
            if not isinstance(url, str) or not url or len(url) > 4096:
                raise TypeError("url must be a non-empty string up to 4096 characters")
            if not isinstance(purpose, str) or purpose not in _PURPOSES:
                raise TypeError("purpose must be image, mask, audio, or video")
            downloaded = await anyio.to_thread.run_sync(
                lambda: downloader.download(url, upload_root)
            )
            try:
                connection = server_connection(owner_id, server_id) if server_connection else None
                gateway = create_gateway(
                    servers.connection(server_id) if connection is None else connection
                )
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
