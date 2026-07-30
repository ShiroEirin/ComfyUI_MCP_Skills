"""HTTP G2 scope and fixed Toolset contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from comfyui_mcp_skills.adapters.http.server import create_http_app


def _project(root: Path) -> None:
    workflow = root / "data" / "local" / "txt2img"
    workflow.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(
        '{"servers":[{"id":"local","name":"Local","url":"http://127.0.0.1:8188"}]}',
        encoding="utf-8",
    )
    (workflow / "schema.json").write_text(
        '{"name":"txt2img","enabled":true,"parameters":{}}', encoding="utf-8"
    )
    (workflow / "workflow.json").write_text("{}", encoding="utf-8")


def _app(root: Path, scope: str, *, toolset: str = "execution", enabled: bool = False):
    _project(root)
    return create_http_app(
        root,
        host="127.0.0.1",
        allowed_hosts=["testserver"],
        allowed_origins=["https://agent.example"],
        tokens={"secret": {"principal_id": "principal", "scopes": [scope]}},
        upload_root=root / "uploads",
        toolset=toolset,
        enable_high_risk=enabled,
    )


def _headers() -> dict[str, str]:
    return {
        "authorization": "Bearer secret",
        "origin": "https://agent.example",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/list",
    }


def _request() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
            }
        },
    }


def test_http_execution_toolset_hides_observe_tools(tmp_path: Path) -> None:
    app = _app(tmp_path, "comfyui:execute")
    with TestClient(app) as client:
        response = client.post("/mcp", json=_request(), headers=_headers())
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert "comfyui.job.get" in names
    assert "comfyui.node.list" not in names
    assert any(name.startswith("comfyui.run.") for name in names)


def test_http_operations_toolset_requires_explicit_enable(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="high-risk"):
        _app(tmp_path, "comfyui:observe", toolset="operations")

    app = _app(tmp_path, "comfyui:observe", toolset="operations", enabled=True)
    with TestClient(app) as client:
        response = client.post("/mcp", json=_request(), headers=_headers())
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert "comfyui.node.list" in names
    assert "comfyui.job.get" not in names
    assert not any(name.startswith("comfyui.run.") for name in names)
