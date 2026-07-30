"""Authenticated Streamable HTTP application assembly."""

from __future__ import annotations

from pathlib import Path

from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings

from comfyui_mcp_skills.adapters.http.auth import StaticTokenVerifier, _validate_tokens
from comfyui_mcp_skills.adapters.http.limits import (
    RequestControlMiddleware,
    StrictMCP2026Middleware,
)
from comfyui_mcp_skills.adapters.http.security import SafeHTTPSDownloader
from comfyui_mcp_skills.adapters.http.uploads import create_asset_routes
from comfyui_mcp_skills.adapters.mcp.server import create_server
from comfyui_mcp_skills.application.assets import AssetService
from comfyui_mcp_skills.application.authorization import (
    AuthorizationContext,
    Scope,
    Toolset,
    admitted_scopes,
)
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.infrastructure.persistence.repository_factory import (
    create_repository_bundle,
)


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
    toolset: str = "execution",
    enable_high_risk: bool = False,
):
    """Build the remote MCP app; remote mode refuses anonymous operation."""
    if auth_mode != "static":
        raise ValueError("Only static bearer-token authentication is currently implemented")
    normalized_tokens = _validate_tokens(tokens)
    try:
        selected_toolset = Toolset(toolset)
    except ValueError as exc:
        raise ValueError("unknown MCP toolset") from exc
    if selected_toolset is not Toolset.EXECUTION and not enable_high_risk:
        raise PermissionError("high-risk HTTP Toolset requires explicit enablement")
    configured_scopes = frozenset(
        Scope(scope) for _principal, scopes in normalized_tokens.values() for scope in scopes
    )
    if not configured_scopes <= admitted_scopes(selected_toolset):
        raise PermissionError("configured token scope does not admit the selected Toolset")
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
    repositories = create_repository_bundle(base_dir)
    assets = AssetService(
        repositories.assets,
        upload_roots=[upload_root],
        max_bytes=max_upload_bytes,
    )
    downloader = SafeHTTPSDownloader(
        allowed_hosts=remote_fetch_hosts or [], max_bytes=max_upload_bytes
    )

    local_host = host in {"127.0.0.1", "localhost", "::1"}
    if not local_host and not public_mcp_url:
        raise ValueError("Remote binding requires a public MCP URL")
    resource_url = public_mcp_url or f"http://{host}/mcp"
    server = create_server(
        base_dir,
        upload_roots=[upload_root],
        repositories=repositories,
        authorization=AuthorizationContext("remote", configured_scopes, selected_toolset),
    )
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
                "required_scopes": [],
            }
        ),
        token_verifier=verifier,
        custom_starlette_routes=create_asset_routes(
            verifier=verifier,
            servers=servers,
            assets=assets,
            downloader=downloader,
            upload_root=upload_root,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
            max_upload_bytes=max_upload_bytes,
            max_fetch_body_bytes=max_fetch_body_bytes,
        ),
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
