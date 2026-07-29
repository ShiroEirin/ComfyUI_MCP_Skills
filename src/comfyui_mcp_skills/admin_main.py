"""Opt-in stdio entry point for dangerous administrative tools."""

from __future__ import annotations

import os
from pathlib import Path

import anyio
from mcp.server.stdio import stdio_server

from .adapters.mcp.admin import create_admin_server
from .observability import configure_logging


async def _run(base_dir: Path, actor: str) -> None:
    server = create_admin_server(base_dir, enabled=True, actor=actor)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    configure_logging(os.environ.get("COMFYUI_MCP_LOG_LEVEL", "INFO"))
    if os.environ.get("COMFYUI_MCP_ENABLE_ADMIN") != "1":
        raise PermissionError("Set COMFYUI_MCP_ENABLE_ADMIN=1 to run the admin server")
    base_dir = Path(os.environ.get("COMFYUI_MCP_DIR", os.getcwd())).resolve()
    actor = os.environ.get("COMFYUI_MCP_ADMIN_ACTOR", "stdio-admin")
    anyio.run(_run, base_dir, actor)


if __name__ == "__main__":
    main()
