"""comfyui.local.plugins contracts: layout rules, bounded scan, safety."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import anyio
import pytest

from comfyui_mcp_skills.application.discovery import DiscoveryService


class _Registry:
    def __init__(self, servers: list[dict[str, object]]) -> None:
        self._servers = servers

    def connection(self, server_id: str) -> dict[str, Any]:
        for server in self._servers:
            if server.get("id") == server_id:
                return dict(server)
        raise LookupError(server_id)


def _service(root: Path, server_id: str = "local") -> DiscoveryService:
    servers = _Registry(
        [{"id": server_id, "url": "http://127.0.0.1:8188", "local_root": str(root)}]
    )
    return DiscoveryService(servers, lambda _config: None)


def _make_plugin(directory: Path, name: str, readme: str = "") -> None:
    plugin_dir = directory / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    if readme:
        (plugin_dir / "README.md").write_text(readme, encoding="utf-8")


def test_nested_layout_preferred_and_flat_fallback(tmp_path: Path) -> None:
    root = tmp_path / "aki"
    nested = root / "ComfyUI" / "custom_nodes"
    _make_plugin(nested, "ComfyUI-AnimaTool", "Anima tooling")

    result = _service(root).plugins("local")
    assert result["available"] is True
    assert result["layout"] == "nested"
    assert {item["name"] for item in result["plugins"]} == {"ComfyUI-AnimaTool"}

    flat_only = tmp_path / "standard"
    _make_plugin(flat_only / "custom_nodes", "efficiency-nodes-comfyui")
    result = _service(flat_only).plugins("local")
    assert result["layout"] == "flat"
    assert result["plugins"][0]["name"] == "efficiency-nodes-comfyui"


def test_merged_dedupes_casefold_and_nested_wins(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    nested = root / "ComfyUI" / "custom_nodes"
    flat = root / "custom_nodes"
    _make_plugin(nested, "MyPlugin", "nested readme")
    _make_plugin(flat, "myplugin", "flat readme")
    _make_plugin(flat, "OtherPlugin", "other")

    result = _service(root).plugins("local")
    assert result["layout"] == "merged"
    by_name = {item["name"]: item for item in result["plugins"]}
    assert len(by_name) == 2
    assert by_name["MyPlugin"]["readme"] == "nested readme"  # nested wins
    assert result["plugins"][0]["name"] == "MyPlugin"  # casefold-stable order


def test_no_plugin_dirs_reports_none_layout(tmp_path: Path) -> None:
    root = tmp_path / "bare"
    root.mkdir()
    result = _service(root).plugins("local")
    assert result["available"] is True
    assert result["layout"] == "none"
    assert result["plugins"] == []
    assert result["total"] == 0
    assert result["truncated"] is False


def test_downgrades_report_fixed_reason_codes(tmp_path: Path) -> None:
    bare = _Registry([{"id": "local", "url": "x"}])
    result = DiscoveryService(bare, lambda _config: None).plugins("local")
    assert result == {"available": False, "reason": "no_local_root"}

    missing = tmp_path / "missing-root"
    servers = _Registry(
        [{"id": "local", "url": "x", "local_root": str(missing)}]
    )
    result = DiscoveryService(servers, lambda _config: None).plugins("local")
    assert result == {"available": False, "reason": "root_not_found"}

    file_root = tmp_path / "a-file"
    file_root.write_text("x", encoding="utf-8")
    servers = _Registry(
        [{"id": "local", "url": "x", "local_root": str(file_root)}]
    )
    result = DiscoveryService(servers, lambda _config: None).plugins("local")
    assert result == {"available": False, "reason": "root_not_directory"}


def test_scan_budget_is_total_direntry_budget(tmp_path: Path) -> None:
    root = tmp_path / "root"
    flat = root / "custom_nodes"
    flat.mkdir(parents=True)
    for index in range(220):
        (flat / f"plugin-{index}").mkdir()
    result = _service(root).plugins("local")
    assert result["truncated"] is True
    assert result["scanned_entries"] == 201
    assert result["total"] == 201  # all 201 budgeted entries were valid plugins
    assert len(result["plugins"]) == 201


def test_bad_entries_consume_budget_and_total_is_lower_bound(tmp_path: Path) -> None:
    root = tmp_path / "root"
    flat = root / "custom_nodes"
    flat.mkdir(parents=True)
    for index in range(100):
        (flat / f"good-{index}").mkdir()
    # 'z-' sorts after 'good-': deterministic scan consumes good first
    for index in range(150):
        (flat / f"z-bad name {index}").write_text("x", encoding="utf-8")

    result = _service(root).plugins("local")
    assert result["truncated"] is True
    assert result["scanned_entries"] == 201
    assert result["total"] < 201  # invalid entries consumed part of the budget
    assert result["total"] == 100


def test_readme_bounded_and_cleaned(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _make_plugin(
        root / "custom_nodes",
        "Plugin",
        "D:\\secret\\path and C:/other/leak with " + "x" * 500,
    )
    result = _service(root).plugins("local")
    readme = result["plugins"][0]["readme"]
    assert "D:" not in readme and "C:" not in readme
    assert len(readme) <= 200


def test_symlink_plugin_dir_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    flat = root / "custom_nodes"
    flat.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, flat / "evil", target_is_directory=True)
    except OSError:
        pytest.skip("symlink unsupported on this platform")
    _make_plugin(flat, "good")

    result = _service(root).plugins("local")
    names = {item["name"] for item in result["plugins"]}
    assert names == {"good"}


def test_mcp_end_to_end(tmp_path: Path) -> None:
    """Tool registered, dispatchable, and EXECUTION-default session hides it."""
    import sys

    sys.path.insert(0, "tests")
    from mcp import Client

    from comfyui_mcp_skills.adapters.mcp.server import create_server
    from comfyui_mcp_skills.application.authorization import (
        AuthorizationContext,
        Scope,
        Toolset,
    )
    from tests.test_mcp_server import _project

    _project(tmp_path)
    root = tmp_path / "comfyui-root"
    _make_plugin(root / "custom_nodes", "ComfyUI-AnimaTool", "Anima tooling")

    async def run() -> None:
        from comfyui_mcp_skills.adapters.mcp.admin import create_admin_server

        admin = create_admin_server(
            tmp_path,
            enabled=True,
            gateway_factory=lambda _config: None,
        )
        async with Client(admin) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "comfyui.local.plugins" in names
            result = await client.call_tool(
                "comfyui.local.plugins", {"server_id": "local"}
            )
            assert result.is_error is False  # admin config has no local_root
            assert result.structured_content["reason"] == "no_local_root"

        server = create_server(
            tmp_path,
            gateway_factory=lambda _config: None,
            authorization=AuthorizationContext(
                "observe-test", frozenset({Scope.OBSERVE}), Toolset.OPERATIONS
            ),
        )
        async with Client(server) as client:
            result = await client.call_tool(
                "comfyui.local.plugins", {"server_id": "local"}
            )
            assert result.is_error is False
            assert result.structured_content["available"] is False

    anyio.run(run)


def test_empty_nested_falls_back_to_flat(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    nested = root / "ComfyUI" / "custom_nodes"
    flat = root / "custom_nodes"
    nested.mkdir(parents=True)  # exists but empty
    _make_plugin(flat, "efficiency-nodes-comfyui", "Efficiency nodes")

    result = _service(root).plugins("local")
    assert result["layout"] == "flat"
    assert {item["name"] for item in result["plugins"]} == {
        "efficiency-nodes-comfyui"
    }


def test_both_empty_reports_none(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    (root / "ComfyUI" / "custom_nodes").mkdir(parents=True)
    (root / "custom_nodes").mkdir(parents=True)

    result = _service(root).plugins("local")
    assert result["layout"] == "none"
    assert result["plugins"] == []
    assert result["total"] == 0


def test_same_layout_casefold_duplicate_keeps_first(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    flat = root / "custom_nodes"
    flat.mkdir(parents=True)
    _make_plugin(flat, "MyPlugin", "first readme")
    _make_plugin(flat, "myplugin", "second readme")

    result = _service(root).plugins("local")
    assert len(result["plugins"]) == 1
    assert result["plugins"][0]["name"] == "MyPlugin"  # first-seen wins


def test_readme_cleans_posix_unc_and_traversal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    plugin = root / "custom_nodes" / "Plugin"
    plugin.mkdir(parents=True)
    (plugin / "README.md").write_text(
        "see /etc/passwd or \\\\server\\share or C:\\evil\\x and ../up",
        encoding="utf-8",
    )
    result = _service(root).plugins("local")
    readme = result["plugins"][0]["readme"]
    assert "/etc" not in readme
    assert "\\\\server" not in readme
    assert "C:" not in readme
    assert "../" not in readme


def test_readme_missing_is_omitted(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "custom_nodes" / "NoReadme").mkdir(parents=True)
    result = _service(root).plugins("local")
    assert result["plugins"][0]["name"] == "NoReadme"
    assert "readme" not in result["plugins"][0]


def test_authoring_toolset_sees_local_plugins(tmp_path: Path) -> None:
    """AUTHORING (observe-capable) surfaces expose the tool; EXECUTION hides it."""
    import sys

    sys.path.insert(0, "tests")
    from mcp import Client

    from comfyui_mcp_skills.adapters.mcp.server import create_server
    from comfyui_mcp_skills.application.authorization import (
        AuthorizationContext,
        Scope,
        Toolset,
    )
    from tests.test_mcp_server import _project

    _project(tmp_path)

    async def run() -> None:
        authoring = create_server(
            tmp_path,
            gateway_factory=lambda _config: None,
            authorization=AuthorizationContext(
                "author-a", frozenset({Scope.OBSERVE, Scope.AUTHOR}), Toolset.AUTHORING
            ),
        )
        async with Client(authoring) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "comfyui.local.plugins" in names

        execution = create_server(tmp_path, gateway_factory=lambda _config: None)
        async with Client(execution) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "comfyui.local.plugins" not in names

    anyio.run(run)
