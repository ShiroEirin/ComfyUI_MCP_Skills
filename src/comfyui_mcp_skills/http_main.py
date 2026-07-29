"""Remote Streamable HTTP process entry point."""

from __future__ import annotations

import json
import os
from pathlib import Path

import uvicorn

from .adapters.http.server import create_http_app


def main() -> None:
    host = os.environ.get("COMFYUI_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("COMFYUI_MCP_PORT", "8765"))
    base_dir = Path(os.environ.get("COMFYUI_MCP_DIR", os.getcwd())).resolve()
    tokens_raw = os.environ.get("COMFYUI_MCP_TOKENS", "{}")
    tokens = json.loads(tokens_raw)
    if not isinstance(tokens, dict):
        raise ValueError("COMFYUI_MCP_TOKENS must be a JSON object")
    allowed_hosts = _csv(
        os.environ.get(
            "COMFYUI_MCP_ALLOWED_HOSTS",
            ",".join(_default_allowed_hosts(host, port)),
        )
    )
    allowed_origins = _csv(os.environ.get("COMFYUI_MCP_ALLOWED_ORIGINS", ""))
    if host not in {"127.0.0.1", "localhost", "::1"} and not allowed_origins:
        raise ValueError("Remote binding requires COMFYUI_MCP_ALLOWED_ORIGINS")
    local_host = host in {"127.0.0.1", "localhost", "::1"}
    public_mcp_url = os.environ.get("COMFYUI_MCP_PUBLIC_URL", "")
    if local_host:
        public_mcp_url = public_mcp_url or f"http://{host}:{port}/mcp"
    elif not public_mcp_url:
        raise ValueError("Remote binding requires COMFYUI_MCP_PUBLIC_URL")
    app = create_http_app(
        base_dir,
        host=host,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        tokens={str(token): list(scopes) for token, scopes in tokens.items()},
        upload_root=Path(
            os.environ.get("COMFYUI_MCP_UPLOAD_ROOT", base_dir / ".mcp-uploads")
        ),
        max_upload_bytes=int(
            os.environ.get("COMFYUI_MCP_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024))
        ),
        max_mcp_body_bytes=int(
            os.environ.get("COMFYUI_MCP_MAX_JSON_BYTES", str(1024 * 1024))
        ),
        max_fetch_body_bytes=int(
            os.environ.get("COMFYUI_MCP_MAX_FETCH_JSON_BYTES", str(64 * 1024))
        ),
        requests_per_minute=int(
            os.environ.get("COMFYUI_MCP_REQUESTS_PER_MINUTE", "120")
        ),
        max_concurrent_requests=int(
            os.environ.get("COMFYUI_MCP_MAX_CONCURRENT_REQUESTS", "32")
        ),
        public_mcp_url=public_mcp_url,
        remote_fetch_hosts=_csv(
            os.environ.get("COMFYUI_MCP_FETCH_HOSTS", "")
        ),
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


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
