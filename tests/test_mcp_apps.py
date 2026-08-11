"""MCP Apps UI extension integration contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.extension import advertise

from comfyui_mcp_skills.adapters.mcp.server import create_server
from comfyui_mcp_skills.adapters.mcp.tooling import (
    JOB_VIEWER_URI,
    UI_EXTENSION_ID,
    UI_MIME_TYPE,
)


class _Gateway:
    def queue_prompt(self, workflow: dict[str, object], **kwargs: object) -> dict[str, str]:
        return {"prompt_id": "prompt-apps", "client_id": "client-apps"}

    def get_history(
        self, prompt_id: str, timeout_seconds: float | None = None
    ) -> dict[str, object] | None:
        return None

    def get_queue(self, timeout_seconds: float | None = None) -> dict[str, object]:
        return {"queue_running": [], "queue_pending": []}

    def interrupt(self, prompt_id: str = "") -> dict[str, object]:
        return {"success": True}

    def queue_delete(self, prompt_ids: list[str]) -> dict[str, object]:
        return {"success": True}

    def upload_file(self, path: str, *, purpose: str, original_ref: str) -> dict[str, str]:
        return {"name": Path(path).name, "subfolder": "agent", "type": "input"}

    def download_output(
        self,
        filename: str,
        subfolder: str = "",
        output_type: str = "output",
        timeout_seconds: float | None = None,
    ) -> bytes:
        return b"payload"


def _project(root: Path) -> None:
    (root / "data" / "local" / "portrait").mkdir(parents=True, exist_ok=True)
    schema = root / "data" / "local" / "portrait" / "schema.json"
    if not schema.exists():
        schema.write_text(
            '{"description": "Portrait", "enabled": true, "parameters": {}}',
            encoding="utf-8",
        )
    workflow = root / "data" / "local" / "portrait" / "workflow.json"
    if not workflow.exists():
        workflow.write_text(
            '{"1": {"class_type": "SaveImage", "inputs": {}}}',
            encoding="utf-8",
        )


def _server(tmp_path: Path):
    _project(tmp_path)
    return create_server(tmp_path, gateway_factory=lambda _config: _Gateway())


@pytest.mark.anyio
async def test_job_viewer_resource_is_listed_and_served(tmp_path: Path) -> None:
    async with Client(_server(tmp_path)) as client:
        resources = await client.list_resources()
        viewer = next(
            resource for resource in resources.resources if resource.uri == JOB_VIEWER_URI
        )
        assert viewer.mime_type == UI_MIME_TYPE

        content = await client.read_resource(JOB_VIEWER_URI)
        assert any("Job 状态" in block.text for block in content.contents)


@pytest.mark.anyio
async def test_job_get_gets_ui_metadata_only_for_apps_clients(tmp_path: Path) -> None:
    options = [
        advertise(UI_EXTENSION_ID, {"mimeTypes": [UI_MIME_TYPE]}),
    ]

    async with Client(_server(tmp_path), extensions=options) as client:
        job_tool = next(
            tool for tool in (await client.list_tools()).tools if tool.name == "comfyui.job.get"
        )
        meta = job_tool.meta or {}
        assert meta.get("ui", {}).get("resourceUri") == JOB_VIEWER_URI

    async with Client(_server(tmp_path)) as client:
        job_tool = next(
            tool for tool in (await client.list_tools()).tools if tool.name == "comfyui.job.get"
        )
        meta = job_tool.meta or {}
        assert "ui" not in meta


@pytest.mark.anyio
async def test_gallery_resources_are_listed_and_served(tmp_path: Path) -> None:
    from comfyui_mcp_skills.adapters.mcp.gallery_app import (
        GALLERY_DATA_URI,
        GALLERY_URI,
    )

    async with Client(_server(tmp_path)) as client:
        resources = await client.list_resources()
        uris = {resource.uri for resource in resources.resources}
        assert GALLERY_URI in uris
        assert GALLERY_DATA_URI in uris
        gallery = next(
            resource for resource in resources.resources if resource.uri == GALLERY_URI
        )
        assert gallery.mime_type == UI_MIME_TYPE

        content = await client.read_resource(GALLERY_URI)
        assert any("图库" in block.text for block in content.contents)

        data = await client.read_resource(GALLERY_DATA_URI)
        payload = json.loads(data.contents[0].text)
        assert "items" in payload
        assert "next_cursor" in payload
        assert isinstance(payload["items"], list)


@pytest.mark.anyio
async def test_gallery_data_rejects_unknown_query_parameters(tmp_path: Path) -> None:
    from comfyui_mcp_skills.adapters.mcp.gallery_app import GALLERY_DATA_URI

    async with Client(_server(tmp_path)) as client:
        with pytest.raises(Exception):
            await client.read_resource(GALLERY_DATA_URI + "?limit=5")
        with pytest.raises(Exception):
            await client.read_resource(
                GALLERY_DATA_URI + "?cursor=a&cursor=b"
            )


@pytest.mark.anyio
async def test_job_list_gets_gallery_ui_metadata_only_for_apps_clients(
    tmp_path: Path,
) -> None:
    from comfyui_mcp_skills.adapters.mcp.gallery_app import GALLERY_URI
    from comfyui_mcp_skills.infrastructure.persistence.control_plane import (
        SQLiteControlPlaneStore,
    )

    _project(tmp_path)
    store = SQLiteControlPlaneStore(tmp_path / "data" / "control-plane.sqlite3")
    store.initialize()
    import sqlite3
    from datetime import datetime, timezone

    with sqlite3.connect(store.path) as connection:
        for kind_name in ("job", "execution_attempt", "idempotency_record", "artifact"):
            connection.execute(
                "INSERT INTO store_migrations("
                "aggregate_kind, version, status, checksum, switched_at"
                ") VALUES (?, 1, 'switched', ?, ?)",
                (kind_name, "a" * 64, datetime.now(timezone.utc).isoformat()),
            )
        connection.commit()

    def _sqlite_server():
        return create_server(tmp_path, gateway_factory=lambda _config: _Gateway())

    options = [
        advertise(UI_EXTENSION_ID, {"mimeTypes": [UI_MIME_TYPE]}),
    ]

    async with Client(_sqlite_server(), extensions=options) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert "comfyui.job.list" in names
        list_tool = next(
            tool for tool in (await client.list_tools()).tools
            if tool.name == "comfyui.job.list"
        )
        meta = list_tool.meta or {}
        assert meta.get("ui", {}).get("resourceUri") == GALLERY_URI

    async with Client(_sqlite_server()) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert "comfyui.job.list" in names
        list_tool = next(
            tool for tool in (await client.list_tools()).tools
            if tool.name == "comfyui.job.list"
        )
        meta = list_tool.meta or {}
        assert "ui" not in meta


@pytest.mark.anyio
async def test_ui_extension_is_advertised_in_initialization(tmp_path: Path) -> None:
    server = _server(tmp_path)
    options = server.create_initialization_options()
    assert options.capabilities is not None
    extensions = options.capabilities.extensions or {}
    assert UI_EXTENSION_ID in extensions
