"""Narrow contracts for SQLite-backed MCP Resource aliases."""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import ReadResourceRequestParams

from comfyui_mcp_skills.adapters.mcp import resources as resource_adapter
from comfyui_mcp_skills.adapters.mcp.resources import create_resource_handlers
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.models import Asset, Job
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.resource_aliases import (
    SQLiteLegacyResourceAliasReader,
)

_ASSET_ID = "asset_" + "a" * 64
_JOB_ID = "job_" + "b" * 64
_ATTEMPT_ID = "attempt_" + "c" * 64
_ARTIFACT_ID = "artifact_" + "d" * 64
_OWNER = "owner-a"
_CREATED_AT = "2026-07-30T00:00:00.000000Z"
_LEGACY_ASSET_URI = f"comfyui://assets/local/{_ASSET_ID}"
_LEGACY_JOB_URI = "comfyui://jobs/local/prompt-1"
_LEGACY_OUTPUT_URI = "comfyui://outputs/local/prompt-1/0"
_CANONICAL_ASSET_URI = f"comfyui://assets/{_ASSET_ID}"
_CANONICAL_JOB_URI = f"comfyui://jobs/{_JOB_ID}"
_CANONICAL_ARTIFACT_URI = f"comfyui://artifacts/{_ARTIFACT_ID}"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def alias_reader(tmp_path: Path) -> SQLiteLegacyResourceAliasReader:
    store = SQLiteControlPlaneStore((tmp_path / "control-plane.sqlite3").resolve())
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO assets(
                asset_id, owner_id, server_id, name, subfolder, media_type,
                mime_type, size_bytes, sha256, source_type, comfyui_ref,
                created_at, expires_at
            ) VALUES (?, ?, 'local', 'input.png', '', 'image', 'image/png',
                      8, ?, 'legacy_upload', 'input.png', ?, NULL)
            """,
            (_ASSET_ID, _OWNER, "1" * 64, _CREATED_AT),
        )
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, workflow_id, owner_id, status, created_at,
                created_at_source, legacy_migrated, execution_origin
            ) VALUES (?, 'txt2img', ?, 'completed', ?, 'legacy_file_mtime', 1,
                      'legacy_migrated')
            """,
            (_JOB_ID, _OWNER, _CREATED_AT),
        )
        connection.execute(
            """
            INSERT INTO execution_attempts(
                attempt_id, job_id, attempt, server_id, upstream_prompt_id,
                upstream_job_id, client_id, submission_state, created_at
            ) VALUES (?, ?, 1, 'local', 'prompt-1', NULL, 'client-1',
                      'submitted', ?)
            """,
            (_ATTEMPT_ID, _JOB_ID, _CREATED_AT),
        )
        connection.execute(
            """
            INSERT INTO artifacts(
                artifact_id, job_id, server_id, upstream_node_id, output_key,
                upstream_output_index, filename, subfolder, storage_type,
                media_type, digest, created_at
            ) VALUES (?, ?, 'local', '9', 'images', 0, 'out.png', '',
                      'output', 'image', ?, ?)
            """,
            (_ARTIFACT_ID, _JOB_ID, "2" * 64, _CREATED_AT),
        )
        connection.executemany(
            """
            INSERT INTO legacy_resource_aliases(
                alias_uri, canonical_uri, object_kind, asset_id, job_id,
                artifact_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    _LEGACY_ASSET_URI,
                    _CANONICAL_ASSET_URI,
                    "asset",
                    _ASSET_ID,
                    None,
                    None,
                    _CREATED_AT,
                ),
                (
                    _LEGACY_JOB_URI,
                    _CANONICAL_JOB_URI,
                    "job",
                    None,
                    _JOB_ID,
                    None,
                    _CREATED_AT,
                ),
                (
                    _LEGACY_OUTPUT_URI,
                    _CANONICAL_ARTIFACT_URI,
                    "output",
                    None,
                    None,
                    _ARTIFACT_ID,
                    _CREATED_AT,
                ),
            ],
        )
    return SQLiteLegacyResourceAliasReader(store)


def test_alias_reader_resolves_legacy_and_canonical_uris_to_same_targets(
    alias_reader: SQLiteLegacyResourceAliasReader,
) -> None:
    pairs = [
        (_LEGACY_ASSET_URI, _CANONICAL_ASSET_URI),
        (_LEGACY_JOB_URI, _CANONICAL_JOB_URI),
        (_LEGACY_OUTPUT_URI, _CANONICAL_ARTIFACT_URI),
    ]

    for legacy_uri, canonical_uri in pairs:
        legacy = alias_reader.resolve(legacy_uri, owner_id=_OWNER)
        canonical = alias_reader.resolve(canonical_uri, owner_id=_OWNER)
        assert legacy is not None
        assert legacy == canonical
        assert legacy.canonical_uri == canonical_uri


def test_alias_reader_rejects_unknown_cross_owner_and_malformed_uris(
    alias_reader: SQLiteLegacyResourceAliasReader,
) -> None:
    assert alias_reader.resolve(_LEGACY_ASSET_URI, owner_id="owner-b") is None
    assert alias_reader.resolve(_CANONICAL_JOB_URI, owner_id="owner-b") is None
    assert alias_reader.resolve("comfyui://outputs/local/missing/0", owner_id=_OWNER) is None

    malformed = [
        _LEGACY_ASSET_URI + "?download=true",
        _LEGACY_JOB_URI + "#fragment",
        "comfyui://outputs/local/prompt-1/00",
        "comfyui://outputs/local/prompt%2D1/0",
        _CANONICAL_ARTIFACT_URI + "/extra",
        "comfyui://assets/job_" + "b" * 64,
        " comfyui://jobs/" + _JOB_ID,
        "comfyui://jobs/" + _JOB_ID + "\r\n",
    ]
    assert all(alias_reader.resolve(uri, owner_id=_OWNER) is None for uri in malformed)


class _Assets:
    def get(self, asset_id: str, *, owner_id: str = "") -> Asset:
        assert asset_id == _ASSET_ID
        assert owner_id == _OWNER
        return Asset(
            asset_id=_ASSET_ID,
            server_id="local",
            comfyui_ref="input.png",
            name="input.png",
            subfolder="",
            media_type="image",
            mime_type="image/png",
            size_bytes=8,
            sha256="1" * 64,
            owner_id=_OWNER,
            created_at=_CREATED_AT,
        )


class _Jobs:
    async_calls: list[tuple[str, str, str]] = []

    def get(self, server_id: str, prompt_id: str, *, owner_id: str = "") -> Job:
        self.async_calls.append((server_id, prompt_id, owner_id))
        return Job(
            prompt_id="prompt-1",
            server_id="local",
            workflow_id="txt2img",
            status="completed",
            outputs=(),
            owner_id=_OWNER,
        )


class _Gateway:
    def __init__(self) -> None:
        self.downloads: list[tuple[str, str, str, int]] = []

    def download_output(
        self,
        filename: str,
        subfolder: str = "",
        output_type: str = "output",
        *,
        max_bytes: int,
    ) -> bytes:
        self.downloads.append((filename, subfolder, output_type, max_bytes))
        return b"same-output"


@pytest.mark.anyio
async def test_resource_handlers_return_canonical_identity_and_same_sqlite_fact(
    tmp_path: Path,
    alias_reader: SQLiteLegacyResourceAliasReader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "default_server": "local",
                "servers": [{"id": "local", "name": "Local", "url": "http://127.0.0.1:8188"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(resource_adapter, "current_owner", lambda: _OWNER)
    gateway = _Gateway()
    handlers = create_resource_handlers(
        catalog=Any,  # type: ignore[arg-type]
        servers=ServerRegistry(tmp_path),
        assets=_Assets(),  # type: ignore[arg-type]
        jobs=_Jobs(),  # type: ignore[arg-type]
        gateway_factory=lambda _config: gateway,  # type: ignore[arg-type,return-value]
        enabled_workflows=lambda: [],
        resource_aliases=alias_reader,
    )

    async def read(uri: str) -> Any:
        return await handlers.read_resource(None, ReadResourceRequestParams(uri=uri))

    legacy_asset = await read(_LEGACY_ASSET_URI)
    canonical_asset = await read(_CANONICAL_ASSET_URI)
    legacy_asset_document = json.loads(legacy_asset.contents[0].text)
    assert legacy_asset_document == json.loads(canonical_asset.contents[0].text)
    assert legacy_asset_document["canonical_uri"] == _CANONICAL_ASSET_URI
    assert str(legacy_asset.contents[0].uri) == _CANONICAL_ASSET_URI

    legacy_job = await read(_LEGACY_JOB_URI)
    canonical_job = await read(_CANONICAL_JOB_URI)
    legacy_job_document = json.loads(legacy_job.contents[0].text)
    assert legacy_job_document == json.loads(canonical_job.contents[0].text)
    assert legacy_job_document["canonical_uri"] == _CANONICAL_JOB_URI
    assert str(legacy_job.contents[0].uri) == _CANONICAL_JOB_URI

    legacy_output = await read(_LEGACY_OUTPUT_URI)
    canonical_output = await read(_CANONICAL_ARTIFACT_URI)
    assert legacy_output.contents[0].blob == canonical_output.contents[0].blob
    assert base64.b64decode(legacy_output.contents[0].blob) == b"same-output"
    assert str(legacy_output.contents[0].uri) == _CANONICAL_ARTIFACT_URI
    assert gateway.downloads == [
        ("out.png", "", "output", 25 * 1024 * 1024),
        ("out.png", "", "output", 25 * 1024 * 1024),
    ]

    with pytest.raises(MCPError) as cross_owner:
        monkeypatch.setattr(resource_adapter, "current_owner", lambda: "owner-b")
        await read(_CANONICAL_ASSET_URI)
    assert cross_owner.value.code == -32602


@pytest.mark.anyio
@pytest.mark.parametrize(
    "uri",
    [
        "comfyui://outputs/local/prompt-1/00",
        "comfyui://jobs/local/prompt-1?x=1",
        "comfyui://assets/local/../asset",
        "comfyui://artifacts/" + _ARTIFACT_ID + "/extra",
    ],
)
async def test_resource_handlers_reject_malformed_alias_uris(
    uri: str,
    alias_reader: SQLiteLegacyResourceAliasReader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resource_adapter, "current_owner", lambda: _OWNER)
    handlers = create_resource_handlers(
        catalog=Any,  # type: ignore[arg-type]
        servers=Any,  # type: ignore[arg-type]
        assets=Any,  # type: ignore[arg-type]
        jobs=Any,  # type: ignore[arg-type]
        gateway_factory=Any,  # type: ignore[arg-type]
        enabled_workflows=lambda: [],
        resource_aliases=alias_reader,
    )

    with pytest.raises(MCPError) as captured:
        await handlers.read_resource(None, ReadResourceRequestParams(uri=uri))
    assert captured.value.code == -32602
