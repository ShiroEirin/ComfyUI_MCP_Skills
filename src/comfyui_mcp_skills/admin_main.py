"""Opt-in stdio entry point for dangerous administrative tools."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import anyio
from mcp.server.stdio import stdio_server

from .adapters.mcp.admin import create_admin_server
from .application.assets import configured_upload_roots
from .application.config_bundles import ConfigBundleService
from .application.provisioning import DependencyProvisioningService
from .application.server_control import ServerControlService
from .infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from .infrastructure.persistence.repository_factory import create_repository_bundle
from .observability import configure_logging

_MAX_CATALOG_BYTES = 1024 * 1024


def _dependency_catalog(base_dir: Path) -> dict[str, dict[str, Any]]:
    path = base_dir / "dependency-catalog.json"
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise ValueError("dependency-catalog.json must be a regular file")
    size = path.stat().st_size
    if not 1 <= size <= _MAX_CATALOG_BYTES:
        raise ValueError("dependency-catalog.json exceeds the 1 MiB limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or len(value) > 10_000:
        raise ValueError("dependency-catalog.json must be a bounded object")
    if any(not isinstance(key, str) or not isinstance(item, dict) for key, item in value.items()):
        raise ValueError("dependency-catalog.json entries must be objects")
    return value


def _configured_manager_hosts() -> set[str]:
    return {
        value.strip().lower().rstrip(".")
        for value in os.environ.get("COMFYUI_MCP_PROVISION_HOSTS", "").split(",")
        if value.strip()
    }


async def _run(base_dir: Path, actor: str, portable_tool_names: bool = False) -> None:
    data_dir = base_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    SQLiteControlPlaneStore((data_dir / "control-plane.sqlite3").resolve()).initialize()
    repositories = create_repository_bundle(base_dir)
    provisioning = repositories.provisioning
    if provisioning is None:
        raise RuntimeError("Phase O provisioning repository is unavailable")
    server = create_admin_server(
        base_dir,
        enabled=True,
        actor=actor,
        repositories=repositories,
        upload_roots=configured_upload_roots(base_dir),
        server_control=ServerControlService(provisioning),
        config_bundles=ConfigBundleService(provisioning),
        dependency_provisioning=DependencyProvisioningService(
            provisioning,
            catalog=_dependency_catalog(base_dir),
            allowed_source_hosts=_configured_manager_hosts(),
        ),
        provisioning_repository=provisioning,
        portable_tool_names=portable_tool_names,
    )
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
    portable = os.environ.get("COMFYUI_MCP_PORTABLE_TOOL_NAMES") == "1"
    anyio.run(_run, base_dir, actor, portable)


if __name__ == "__main__":
    main()
