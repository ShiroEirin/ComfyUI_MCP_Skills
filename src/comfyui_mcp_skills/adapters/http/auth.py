"""Static bearer-token authentication for the HTTP adapter."""

from __future__ import annotations

import hmac
import re

from mcp.server.auth.provider import AccessToken
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from comfyui_mcp_skills.application.authorization import all_scope_values

_PRINCIPAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_ALLOWED_SCOPES = all_scope_values()
_BEARER_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/-]+={0,}$")
_MAX_BEARER_TOKEN_LENGTH = 4096
_MAX_PRINCIPAL_ID_LENGTH = 128


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
                "scopes must be a unique non-empty list containing known ComfyUI scopes"
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


async def authorize(
    request: Request, verifier: StaticTokenVerifier, required_scope: str
) -> Response | None:
    token = bearer_token(request.headers.get("authorization", ""))
    if not token:
        return JSONResponse({"code": "UNAUTHORIZED"}, status_code=401)
    access = await verifier.verify_token(token)
    if access is None:
        return JSONResponse({"code": "UNAUTHORIZED"}, status_code=401)
    if required_scope not in access.scopes:
        return JSONResponse({"code": "FORBIDDEN"}, status_code=403)
    return None


async def request_owner(request: Request, verifier: StaticTokenVerifier) -> str:
    token = bearer_token(request.headers["authorization"])
    access = await verifier.verify_token(token)
    if access is None:
        raise PermissionError("Authenticated token context is missing")
    return access.client_id


def bearer_token(authorization: str) -> str:
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        return ""
    return token
