"""Process entry points for MCP transports."""

from __future__ import annotations

import os
from pathlib import Path

import anyio
from mcp.server.stdio import stdio_server

from .adapters.mcp.server import create_server
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


async def _run_stdio(base_dir: Path) -> None:
    server = create_server(base_dir, upload_roots=_configured_upload_roots(base_dir))
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    """Run the local stdio MCP server without writing non-protocol stdout."""
    configure_logging(os.environ.get("COMFYUI_MCP_LOG_LEVEL", "INFO"))
    base_dir = Path(os.environ.get("COMFYUI_MCP_DIR", os.getcwd())).resolve()
    anyio.run(_run_stdio, base_dir)


if __name__ == "__main__":
    main()
