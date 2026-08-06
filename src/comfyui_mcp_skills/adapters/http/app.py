"""Authenticated Streamable HTTP application assembly."""

from __future__ import annotations

from pathlib import Path

from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings

from comfyui_mcp_skills.adapters.http.auth import (
    IntrospectionTokenVerifier,
    StaticTokenVerifier,
    TokenVerifier,
    _validate_tokens,
)
from comfyui_mcp_skills.adapters.http.limits import (
    RequestControlMiddleware,
    StrictMCP2026Middleware,
)
from comfyui_mcp_skills.adapters.http.security import SafeHTTPSDownloader
from comfyui_mcp_skills.adapters.http.uploads import create_asset_routes
from comfyui_mcp_skills.adapters.mcp.server import create_server
from comfyui_mcp_skills.adapters.mcp.tooling import current_owner
from comfyui_mcp_skills.application.assets import AssetService
from comfyui_mcp_skills.application.authorization import (
    AuthorizationContext,
    Scope,
    Toolset,
    admitted_scopes,
)
from comfyui_mcp_skills.application.runtime_control import controller_provider_from_config
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.application.shared_limits import (
    SharedLimitStore,
    SharedLimitsUnavailable,
)
from comfyui_mcp_skills.infrastructure.comfyui.manager_gateway import SafeManagerGateway
from comfyui_mcp_skills.infrastructure.persistence.repository_factory import (
    create_repository_bundle,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_routing import SQLiteRoutingRepository


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
    max_dynamic_tools: int = 8,
    public_mcp_url: str | None = None,
    auth_mode: str = "static",
    introspection_url: str = "",
    introspection_client_id: str = "",
    introspection_client_secret: str = "",
    introspection_audience: str = "",
    remote_fetch_hosts: list[str] | None = None,
    manager_source_hosts: list[str] | None = None,
    manager_server_origins: list[str] | None = None,
    toolset: str = "execution",
    enable_high_risk: bool = False,
    limit_mode: str = "process",
    shared_limit_store: SharedLimitStore | None = None,
):
    """Build the remote MCP app; remote mode refuses anonymous operation."""
    if limit_mode not in {"process", "external"}:
        raise ValueError("limit_mode must be process or external")
    if limit_mode == "external" and shared_limit_store is None:
        raise SharedLimitsUnavailable("external limit mode requires a shared limit store")
    try:
        selected_toolset = Toolset(toolset)
    except ValueError as exc:
        raise ValueError("unknown MCP toolset") from exc
    if selected_toolset in {Toolset.ADMIN, Toolset.AUTHORING}:
        raise ValueError(
            "admin and authoring Toolsets are available only through isolated local servers"
        )
    verifier: TokenVerifier
    if auth_mode == "static":
        normalized_tokens = _validate_tokens(tokens)
        configured_scopes = frozenset(
            Scope(scope) for _principal, scopes in normalized_tokens.values() for scope in scopes
        )
        verifier = StaticTokenVerifier(tokens)
    elif auth_mode == "introspection":
        normalized_tokens = {}
        configured_scopes = admitted_scopes(selected_toolset)
        verifier = IntrospectionTokenVerifier(
            introspection_url,
            client_id=introspection_client_id,
            client_secret=introspection_client_secret,
            expected_audience=introspection_audience,
        )
    else:
        raise ValueError("auth_mode must be static or introspection")
    if selected_toolset is not Toolset.EXECUTION and not enable_high_risk:
        raise PermissionError("high-risk HTTP Toolset requires explicit enablement")
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
    servers = ServerRegistry(base_dir)
    repositories = create_repository_bundle(base_dir)
    server_connections = (
        SQLiteRoutingRepository(repositories.store) if repositories.store is not None else None
    )
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
        manager_gateway=SafeManagerGateway(
            allowed_source_hosts=set(manager_source_hosts or []),
            allowed_server_origins=set(manager_server_origins or []),
        ),
        owner_provider=current_owner,
        max_dynamic_tools=max_dynamic_tools,
        runtime_controller_provider=controller_provider_from_config(base_dir),
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
            server_connection=(
                server_connections.current_server_connection
                if server_connections is not None
                else None
            ),
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
        token_verifier=verifier,
        bearer_principals={
            token: principal_id for token, (principal_id, _scopes) in normalized_tokens.items()
        },
        limit_mode=limit_mode,
        shared_limit_store=shared_limit_store,
    )
    return app
