"""Process entry points for MCP transports."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import anyio
from mcp.server.stdio import stdio_server

from .adapters.mcp.server import create_server
from .application.auth_context import reset_authorization, set_authorization
from .application.authorization import authorization_for_stdio
from .application.runtime_control import RuntimeController
from .infrastructure.comfyui.manager_gateway import SafeManagerGateway
from .infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from .infrastructure.runtime.systemd import controller_from_config
from .observability import configure_logging


def _configured_upload_roots(base_dir: Path) -> list[Path]:
    configured = os.environ.get("COMFYUI_MCP_UPLOAD_ROOTS", "")
    if not configured:
        return [(base_dir / "uploads").resolve()]
    roots: list[Path] = []
    for value in configured.split(os.pathsep):
        if not value.strip():
            continue
        root = Path(value.strip()).expanduser()
        if not root.is_absolute():
            root = base_dir / root
        roots.append(root.resolve())
    if not roots:
        raise ValueError("COMFYUI_MCP_UPLOAD_ROOTS must contain at least one path")
    return roots


def _configured_manager_hosts() -> set[str]:
    return {
        value.strip().lower().rstrip(".")
        for value in os.environ.get("COMFYUI_MCP_PROVISION_HOSTS", "").split(",")
        if value.strip()
    }


def _configured_manager_origins() -> set[str]:
    return {
        value.strip().rstrip("/")
        for value in os.environ.get("COMFYUI_MCP_MANAGER_ORIGINS", "").split(",")
        if value.strip()
    }


def _runtime_controller_provider(
    base_dir: Path,
) -> Callable[[str], RuntimeController | None] | None:
    """Resolve one controller per server from config.json runtime bindings."""
    from comfyui_mcp_skills.application.servers import ServerRegistry

    registry = ServerRegistry(base_dir)
    controllers: dict[str, RuntimeController] = {}
    try:
        for server in registry.list():
            connection = registry.connection(server.server_id)
            controller = controller_from_config(connection)
            if controller is not None:
                controllers[server.server_id] = controller
    except Exception:
        return None

    if not controllers:
        return None

    def provider(server_id: str) -> RuntimeController | None:
        return controllers.get(server_id)

    return provider


async def _run_stdio(base_dir: Path) -> None:
    authorization = authorization_for_stdio(os.environ)
    data_dir = base_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    SQLiteControlPlaneStore((data_dir / "control-plane.sqlite3").resolve()).initialize()
    server = create_server(
        base_dir,
        upload_roots=_configured_upload_roots(base_dir),
        authorization=authorization,
        portable_tool_names=os.environ.get("COMFYUI_MCP_PORTABLE_TOOL_NAMES") == "1",
        max_dynamic_tools=int(os.environ.get("COMFYUI_MCP_MAX_DYNAMIC_TOOLS", "8")),
        runtime_controller_provider=_runtime_controller_provider(base_dir),
        manager_gateway=SafeManagerGateway(
            allowed_source_hosts=_configured_manager_hosts(),
            allowed_server_origins=_configured_manager_origins(),
        ),
    )
    token = set_authorization(authorization)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        reset_authorization(token)


def main() -> None:
    """Run the local stdio MCP server without writing non-protocol stdout."""
    configure_logging(os.environ.get("COMFYUI_MCP_LOG_LEVEL", "INFO"))
    base_dir = Path(os.environ.get("COMFYUI_MCP_DIR", os.getcwd())).resolve()
    anyio.run(_run_stdio, base_dir)


if __name__ == "__main__":
    main()
