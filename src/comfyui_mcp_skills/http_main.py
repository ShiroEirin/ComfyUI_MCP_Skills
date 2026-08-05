"""Remote Streamable HTTP process entry point."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import uvicorn

from comfyui_mcp_skills.adapters.http.app import create_http_app
from comfyui_mcp_skills.adapters.http.auth import _validate_tokens
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.observability import configure_logging

_APP_FACTORY = "comfyui_mcp_skills.http_main:create_app"


def create_app():
    """Build an HTTP app from the current process environment for Uvicorn workers."""
    configure_logging(os.environ.get("COMFYUI_MCP_LOG_LEVEL", "INFO"))
    _host, _port, app_options = _http_environment()
    _initialize_control_plane(app_options["base_dir"])
    return create_http_app(**app_options)


def main() -> None:
    configure_logging(os.environ.get("COMFYUI_MCP_LOG_LEVEL", "INFO"))
    workers = int(os.environ.get("COMFYUI_MCP_WORKERS", "1"))
    limit_mode = os.environ.get("COMFYUI_MCP_LIMIT_MODE", "process").strip().lower()
    _validate_worker_limits(workers, limit_mode)
    host, port, app_options = _http_environment()
    _initialize_control_plane(app_options["base_dir"])
    if workers > 1:
        uvicorn.run(
            _APP_FACTORY,
            host=host,
            port=port,
            log_level="info",
            workers=workers,
            factory=True,
        )
        return
    uvicorn.run(
        create_http_app(**app_options),
        host=host,
        port=port,
        log_level="info",
        workers=workers,
    )


def _http_environment() -> tuple[str, int, dict[str, Any]]:
    host = os.environ.get("COMFYUI_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("COMFYUI_MCP_PORT", "8765"))
    base_dir = Path(os.environ.get("COMFYUI_MCP_DIR", os.getcwd())).resolve()
    tokens = json.loads(os.environ.get("COMFYUI_MCP_TOKENS", "{}"))
    if not isinstance(tokens, dict):
        raise ValueError("COMFYUI_MCP_TOKENS must be a JSON object")
    auth_mode = os.environ.get("COMFYUI_MCP_AUTH_MODE", "static").strip().lower()
    if auth_mode == "static":
        _validate_tokens(tokens)
    allowed_hosts = _csv(
        os.environ.get(
            "COMFYUI_MCP_ALLOWED_HOSTS",
            ",".join(_default_allowed_hosts(host, port)),
        )
    )
    allowed_origins = _csv(os.environ.get("COMFYUI_MCP_ALLOWED_ORIGINS", ""))
    local_host = host in {"127.0.0.1", "localhost", "::1"}
    if not local_host and not allowed_origins:
        raise ValueError("Remote binding requires COMFYUI_MCP_ALLOWED_ORIGINS")
    public_mcp_url = os.environ.get("COMFYUI_MCP_PUBLIC_URL", "")
    if local_host:
        public_mcp_url = public_mcp_url or f"http://{host}:{port}/mcp"
    elif not public_mcp_url:
        raise ValueError("Remote binding requires COMFYUI_MCP_PUBLIC_URL")
    app_options: dict[str, Any] = {
        "base_dir": base_dir,
        "host": host,
        "allowed_hosts": allowed_hosts,
        "allowed_origins": allowed_origins,
        "tokens": tokens,
        "introspection_url": os.environ.get("COMFYUI_MCP_INTROSPECTION_URL", "").strip(),
        "introspection_client_id": os.environ.get(
            "COMFYUI_MCP_INTROSPECTION_CLIENT_ID", ""
        ).strip(),
        "introspection_client_secret": os.environ.get(
            "COMFYUI_MCP_INTROSPECTION_CLIENT_SECRET", ""
        ),
        "introspection_audience": os.environ.get("COMFYUI_MCP_INTROSPECTION_AUDIENCE", ""),
        "upload_root": Path(os.environ.get("COMFYUI_MCP_UPLOAD_ROOT", base_dir / ".mcp-uploads")),
        "max_upload_bytes": int(
            os.environ.get("COMFYUI_MCP_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024))
        ),
        "max_mcp_body_bytes": int(os.environ.get("COMFYUI_MCP_MAX_JSON_BYTES", str(1024 * 1024))),
        "max_fetch_body_bytes": int(
            os.environ.get("COMFYUI_MCP_MAX_FETCH_JSON_BYTES", str(64 * 1024))
        ),
        "requests_per_minute": int(os.environ.get("COMFYUI_MCP_REQUESTS_PER_MINUTE", "120")),
        "max_concurrent_requests": int(os.environ.get("COMFYUI_MCP_MAX_CONCURRENT_REQUESTS", "32")),
        "max_subscription_streams": int(
            os.environ.get("COMFYUI_MCP_MAX_SUBSCRIPTION_STREAMS", "8")
        ),
        "max_subscriptions_per_principal": int(
            os.environ.get("COMFYUI_MCP_MAX_SUBSCRIPTIONS_PER_PRINCIPAL", "2")
        ),
        "public_mcp_url": public_mcp_url,
        "auth_mode": auth_mode,
        "remote_fetch_hosts": _csv(os.environ.get("COMFYUI_MCP_FETCH_HOSTS", "")),
        "manager_source_hosts": _csv(os.environ.get("COMFYUI_MCP_PROVISION_HOSTS", "")),
        "manager_server_origins": _csv(os.environ.get("COMFYUI_MCP_MANAGER_ORIGINS", "")),
        "toolset": os.environ.get("COMFYUI_MCP_TOOLSET", "execution").strip().lower(),
        "enable_high_risk": os.environ.get("COMFYUI_MCP_ENABLE_HIGH_RISK", "") == "1",
    }
    return host, port, app_options


def _initialize_control_plane(base_dir: Path) -> None:
    data_dir = base_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    SQLiteControlPlaneStore((data_dir / "control-plane.sqlite3").resolve()).initialize()


def _validate_worker_limits(workers: int, limit_mode: str) -> None:
    if workers <= 0:
        raise ValueError("COMFYUI_MCP_WORKERS must be positive")
    if limit_mode not in {"process", "external"}:
        raise ValueError("COMFYUI_MCP_LIMIT_MODE must be process or external")
    if workers > 1:
        raise ValueError("Multiple workers require a configured shared rate-limit backend")


def _default_allowed_hosts(host: str, port: int) -> list[str]:
    hosts = {host, f"{host}:{port}"}
    if host in {"127.0.0.1", "localhost", "::1"}:
        hosts.update(
            {
                "127.0.0.1",
                f"127.0.0.1:{port}",
                "localhost",
                f"localhost:{port}",
                "[::1]",
                f"[::1]:{port}",
            }
        )
    return sorted(hosts)


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


if __name__ == "__main__":
    main()
