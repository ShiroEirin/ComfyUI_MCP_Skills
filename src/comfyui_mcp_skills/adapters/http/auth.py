"""Static bearer-token authentication for the HTTP adapter."""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

import anyio
import requests
from mcp.server.auth.provider import AccessToken
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from comfyui_mcp_skills.application.authorization import all_scope_values

_PRINCIPAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_ALLOWED_SCOPES = all_scope_values()
_BEARER_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/-]+={0,}$")
_MAX_BEARER_TOKEN_LENGTH = 4096
_MAX_PRINCIPAL_ID_LENGTH = 128


class TokenVerifier(Protocol):
    async def verify_token(self, token: str) -> AccessToken | None: ...


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


class IntrospectionTokenVerifier:
    """Validate rotating OAuth access tokens against an RFC 7662 endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        client_id: str,
        client_secret: str,
        expected_audience: str,
        request: Callable[..., requests.Response] = requests.post,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("OAuth introspection endpoint must be an HTTPS origin URL")
        if parsed.fragment:
            raise ValueError("OAuth introspection endpoint must not contain a fragment")
        if not client_id or len(client_id) > 256 or not client_secret or len(client_secret) > 4096:
            raise ValueError("OAuth introspection client credentials are invalid")
        if not expected_audience or len(expected_audience) > 512:
            raise ValueError("OAuth introspection audience is required")
        self._endpoint = endpoint
        self._client_id = client_id
        self._client_secret = client_secret
        self._expected_audience = expected_audience
        self._request = request

    async def verify_token(self, token: str) -> AccessToken | None:
        if (
            not isinstance(token, str)
            or not token
            or len(token) > _MAX_BEARER_TOKEN_LENGTH
            or _BEARER_TOKEN_PATTERN.fullmatch(token) is None
        ):
            return None
        response = await anyio.to_thread.run_sync(self._introspect, token)
        if response is None:
            return None
        principal = response.get("sub")
        if (
            not isinstance(principal, str)
            or not principal
            or len(principal) > _MAX_PRINCIPAL_ID_LENGTH
            or _PRINCIPAL_ID_PATTERN.fullmatch(principal) is None
        ):
            return None
        audience = response.get("aud")
        audiences = [audience] if isinstance(audience, str) else audience
        if not isinstance(audiences, list) or self._expected_audience not in audiences:
            return None
        raw_scopes = response.get("scope", [])
        scopes = raw_scopes.split() if isinstance(raw_scopes, str) else raw_scopes
        if not isinstance(scopes, list):
            return None
        if (
            not isinstance(scopes, list)
            or len(scopes) > 64
            or any(not isinstance(scope, str) or len(scope) > 128 for scope in scopes)
        ):
            return None
        admitted = [scope for scope in scopes if scope in _ALLOWED_SCOPES]
        if not admitted:
            return None
        return AccessToken(token=token, client_id=principal, scopes=admitted)

    def _introspect(self, token: str) -> dict[str, Any] | None:
        response: requests.Response | None = None
        try:
            response = self._request(
                self._endpoint,
                data={"token": token, "token_type_hint": "access_token"},
                auth=(self._client_id, self._client_secret),
                headers={"Accept": "application/json"},
                timeout=(3.05, 5.0),
                allow_redirects=False,
                stream=True,
            )
            if response.status_code != 200:
                return None
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=8192):
                size += len(chunk)
                if size > 64 * 1024:
                    return None
                chunks.append(chunk)
            data = json.loads(b"".join(chunks))
        except (requests.RequestException, ValueError, TypeError):
            return None
        finally:
            if response is not None:
                response.close()
        if not isinstance(data, dict) or data.get("active") is not True:
            return None
        return data


async def authorize(
    request: Request, verifier: TokenVerifier, required_scope: str
) -> Response | None:
    token = bearer_token(request.headers.get("authorization", ""))
    if not token:
        return JSONResponse({"code": "UNAUTHORIZED"}, status_code=401)
    access = getattr(request.state, "access_token", None)
    if not isinstance(access, AccessToken):
        access = await verifier.verify_token(token)
    if access is None:
        return JSONResponse({"code": "UNAUTHORIZED"}, status_code=401)
    if required_scope not in access.scopes:
        return JSONResponse({"code": "FORBIDDEN"}, status_code=403)
    request.state.access_token = access
    return None


async def request_owner(request: Request, verifier: TokenVerifier) -> str:
    access = getattr(request.state, "access_token", None)
    if not isinstance(access, AccessToken):
        token = bearer_token(request.headers["authorization"])
        access = await verifier.verify_token(token)
        if access is None:
            raise PermissionError("Authenticated token context is missing")
        request.state.access_token = access
    return access.client_id


def bearer_token(authorization: str) -> str:
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        return ""
    return token
