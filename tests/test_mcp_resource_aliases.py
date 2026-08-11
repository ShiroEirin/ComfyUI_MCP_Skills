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
from comfyui_mcp_skills.application.authorization import Scope
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import AssetLibraryConflict
from comfyui_mcp_skills.domain.models import Asset, Job
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.resource_aliases import (
    SQLiteLegacyResourceAliasReader,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_asset_library import (
    SQLiteAssetLibraryRepository,
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
_WORKFLOW_ID = "portrait"
_REVISION_ID = "revision_" + "e" * 64
_DEPLOYMENT_ID = "deployment_" + "f" * 64
_CANONICAL_WORKFLOW_URI = f"comfyui://workflows/{_WORKFLOW_ID}"
_CANONICAL_REVISION_URI = f"comfyui://revisions/{_REVISION_ID}"
_CANONICAL_DEPLOYMENT_URI = f"comfyui://deployments/{_DEPLOYMENT_ID}"


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
            "INSERT INTO workflows(workflow_id, created_at) VALUES (?, ?)",
            (_WORKFLOW_ID, _CREATED_AT),
        )
        connection.execute(
            """
            INSERT INTO workflow_revisions(
                revision_id, workflow_id, graph_json, parameter_schema_json,
                dependency_contract_json, content_digest, created_at
            ) VALUES (?, ?, ?, ?, '{}', ?, ?)
            """,
            (
                _REVISION_ID,
                _WORKFLOW_ID,
                '{"secret_prompt":"do not expose","path":"C:/private"}',
                '{"resolved_inputs":{"token":"do not expose"}}',
                "3" * 64,
                _CREATED_AT,
            ),
        )
        connection.execute(
            """
            INSERT INTO workflow_deployments(
                deployment_id, workflow_id, revision_id, server_id, enabled,
                validation_status, published, created_at
            ) VALUES (?, ?, ?, 'local', 1, 'valid', 1, ?)
            """,
            (_DEPLOYMENT_ID, _WORKFLOW_ID, _REVISION_ID, _CREATED_AT),
        )
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
        connection.execute(
            """
            INSERT INTO artifact_completeness(
                artifact_id, completeness, mime_type, size_bytes, sha256,
                legacy_index, observed_at
            ) VALUES (?, 'locator_only', 'image/png', NULL, NULL, 0, ?)
            """,
            (_ARTIFACT_ID, _CREATED_AT),
        )
        connection.execute(
            """
            UPDATE job_artifact_collections
            SET status='complete', artifact_count=1,
                output_snapshot_digest=?, error_code=NULL, updated_at=?
            WHERE job_id=?
            """,
            ("4" * 64, _CREATED_AT, _JOB_ID),
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


@pytest.mark.parametrize("state", ["archived", "deleted"])
def test_alias_reader_rejects_unavailable_artifact_locations(
    alias_reader: SQLiteLegacyResourceAliasReader,
    state: str,
) -> None:
    archived_at = _CREATED_AT if state == "archived" else None
    deleted_at = _CREATED_AT if state == "deleted" else None
    with sqlite3.connect(alias_reader._store.path) as connection:
        connection.execute(
            """
            UPDATE media_locations
            SET state=?, archived_at=?, deleted_at=?, updated_at=?
            WHERE artifact_id=?
            """,
            (state, archived_at, deleted_at, _CREATED_AT, _ARTIFACT_ID),
        )

    assert alias_reader.resolve(_CANONICAL_ARTIFACT_URI, owner_id=_OWNER) is None
    assert alias_reader.resolve(_LEGACY_OUTPUT_URI, owner_id=_OWNER) is None


def test_alias_reader_fails_closed_while_artifact_backfill_is_pending(
    alias_reader: SQLiteLegacyResourceAliasReader,
) -> None:
    with sqlite3.connect(alias_reader._store.path) as connection:
        connection.execute(
            """
            UPDATE phase_l_backfill_state
            SET status='pending', incomplete_count=1, completed_at=NULL,
                failure_code=NULL
            WHERE backfill_name='artifact_outputs'
            """
        )

    for uri in (_CANONICAL_ARTIFACT_URI, _LEGACY_OUTPUT_URI):
        with pytest.raises(AssetLibraryConflict) as raised:
            alias_reader.resolve(uri, owner_id=_OWNER)
        assert raised.value.details == {"reason": "backfill_pending"}


def test_alias_reader_rejects_incomplete_job_artifact_collection(
    alias_reader: SQLiteLegacyResourceAliasReader,
) -> None:
    with sqlite3.connect(alias_reader._store.path) as connection:
        connection.execute(
            """
            UPDATE job_artifact_collections
            SET status='needs_backfill', output_snapshot_digest=NULL,
                error_code=NULL, updated_at=?
            WHERE job_id=?
            """,
            (_CREATED_AT, _JOB_ID),
        )

    assert alias_reader.resolve(_CANONICAL_ARTIFACT_URI, owner_id=_OWNER) is None
    assert alias_reader.resolve(_LEGACY_OUTPUT_URI, owner_id=_OWNER) is None


def test_alias_reader_rejects_missing_artifact_completeness_fact(
    alias_reader: SQLiteLegacyResourceAliasReader,
) -> None:
    with sqlite3.connect(alias_reader._store.path) as connection:
        connection.execute(
            "DELETE FROM artifact_completeness WHERE artifact_id=?",
            (_ARTIFACT_ID,),
        )

    assert alias_reader.resolve(_CANONICAL_ARTIFACT_URI, owner_id=_OWNER) is None
    assert alias_reader.resolve(_LEGACY_OUTPUT_URI, owner_id=_OWNER) is None


@pytest.mark.parametrize(
    "alias_uri",
    [
        "comfyui://outputs/local/prompt-1/1",
        "comfyui://outputs/local/other-prompt/0",
    ],
)
def test_output_alias_requires_exact_job_and_legacy_index_binding(
    alias_reader: SQLiteLegacyResourceAliasReader,
    alias_uri: str,
) -> None:
    with sqlite3.connect(alias_reader._store.path) as connection:
        connection.execute(
            """
            INSERT INTO legacy_resource_aliases(
                alias_uri, canonical_uri, object_kind, asset_id, job_id,
                artifact_id, created_at
            ) VALUES (?, ?, 'output', NULL, NULL, ?, ?)
            """,
            (alias_uri, _CANONICAL_ARTIFACT_URI, _ARTIFACT_ID, _CREATED_AT),
        )

    assert alias_reader.resolve(alias_uri, owner_id=_OWNER) is None


def test_alias_reader_resolves_safe_workflow_revision_and_deployment_metadata(
    alias_reader: SQLiteLegacyResourceAliasReader,
) -> None:
    workflow = alias_reader.resolve(_CANONICAL_WORKFLOW_URI, owner_id="unrelated-owner")
    revision = alias_reader.resolve(_CANONICAL_REVISION_URI, owner_id="unrelated-owner")
    deployment = alias_reader.resolve(_CANONICAL_DEPLOYMENT_URI, owner_id="unrelated-owner")

    assert workflow is not None
    assert workflow.metadata == {
        "workflow_id": _WORKFLOW_ID,
        "created_at": _CREATED_AT,
    }
    assert revision is not None
    assert revision.metadata == {
        "revision_id": _REVISION_ID,
        "workflow_id": _WORKFLOW_ID,
        "content_digest": "3" * 64,
        "created_at": _CREATED_AT,
    }
    assert deployment is not None
    assert deployment.metadata == {
        "deployment_id": _DEPLOYMENT_ID,
        "workflow_id": _WORKFLOW_ID,
        "revision_id": _REVISION_ID,
        "server_id": "local",
        "enabled": True,
        "validation_status": "valid",
        "published": True,
        "created_at": _CREATED_AT,
        "content_digest": "3" * 64,
    }

    serialized = json.dumps(
        [workflow.metadata, revision.metadata, deployment.metadata],
        ensure_ascii=False,
    )
    assert "secret_prompt" not in serialized
    assert "resolved_inputs" not in serialized
    assert "C:/private" not in serialized


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
        _CANONICAL_WORKFLOW_URI + "?raw=1",
        _CANONICAL_REVISION_URI + "/extra",
        "comfyui://revisions/deployment_" + "f" * 64,
        "comfyui://deployments/%64eployment_" + "f" * 64,
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


class _AssetLibraryList:
    def list_assets(self, *, owner_id: str, limit: int) -> dict[str, Any]:
        assert owner_id == _OWNER
        assert limit == 100
        return {"items": [], "next_cursor": ""}


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["archived", "deleted"])
async def test_resource_listing_omits_unavailable_artifacts(
    alias_reader: SQLiteLegacyResourceAliasReader,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    monkeypatch.setattr(resource_adapter, "current_owner", lambda: _OWNER)
    repository = SQLiteAssetLibraryRepository(alias_reader._store)
    handlers = create_resource_handlers(
        catalog=Any,  # type: ignore[arg-type]
        servers=Any,  # type: ignore[arg-type]
        assets=Any,  # type: ignore[arg-type]
        jobs=Any,  # type: ignore[arg-type]
        gateway_factory=Any,  # type: ignore[arg-type]
        enabled_workflows=lambda: [],
        asset_library=_AssetLibraryList(),  # type: ignore[arg-type]
        asset_library_repository=repository,
    )

    available = await handlers.list_resources(None, None)
    assert [str(resource.uri) for resource in available.resources] == [
        "ui://comfyui/job.html",
        "ui://comfyui/gallery.html",
        "comfyui://gallery/jobs",
        _CANONICAL_ARTIFACT_URI,
    ]

    archived_at = _CREATED_AT if state == "archived" else None
    deleted_at = _CREATED_AT if state == "deleted" else None
    with sqlite3.connect(alias_reader._store.path) as connection:
        connection.execute(
            """
            UPDATE media_locations
            SET state=?, archived_at=?, deleted_at=?, updated_at=?
            WHERE artifact_id=?
            """,
            (state, archived_at, deleted_at, _CREATED_AT, _ARTIFACT_ID),
        )

    unavailable = await handlers.list_resources(None, None)
    assert [str(resource.uri) for resource in unavailable.resources] == [
        "ui://comfyui/job.html",
        "ui://comfyui/gallery.html",
        "comfyui://gallery/jobs",
    ]


@pytest.mark.anyio
async def test_resource_listing_fails_closed_while_artifact_backfill_is_pending(
    alias_reader: SQLiteLegacyResourceAliasReader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resource_adapter, "current_owner", lambda: _OWNER)
    with sqlite3.connect(alias_reader._store.path) as connection:
        connection.execute(
            """
            UPDATE phase_l_backfill_state
            SET status='pending', incomplete_count=1, completed_at=NULL,
                failure_code=NULL
            WHERE backfill_name='artifact_outputs'
            """
        )
    handlers = create_resource_handlers(
        catalog=Any,  # type: ignore[arg-type]
        servers=Any,  # type: ignore[arg-type]
        assets=Any,  # type: ignore[arg-type]
        jobs=Any,  # type: ignore[arg-type]
        gateway_factory=Any,  # type: ignore[arg-type]
        enabled_workflows=lambda: [],
        asset_library=_AssetLibraryList(),  # type: ignore[arg-type]
        asset_library_repository=SQLiteAssetLibraryRepository(alias_reader._store),
    )

    with pytest.raises(AssetLibraryConflict) as raised:
        await handlers.list_resources(None, None)
    assert raised.value.details == {"reason": "backfill_pending"}


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
@pytest.mark.parametrize("scope", [Scope.OBSERVE, Scope.AUTHOR])
async def test_observe_and_author_scopes_expose_only_safe_metadata_resources(
    tmp_path: Path,
    alias_reader: SQLiteLegacyResourceAliasReader,
    monkeypatch: pytest.MonkeyPatch,
    scope: Scope,
) -> None:
    monkeypatch.setattr(resource_adapter, "current_owner", lambda: _OWNER)
    monkeypatch.setattr(
        resource_adapter,
        "current_scopes",
        lambda: frozenset({scope}),
    )
    handlers = create_resource_handlers(
        catalog=Any,  # type: ignore[arg-type]
        servers=ServerRegistry(tmp_path),
        assets=_Assets(),  # type: ignore[arg-type]
        jobs=_Jobs(),  # type: ignore[arg-type]
        gateway_factory=lambda _config: _Gateway(),  # type: ignore[arg-type,return-value]
        enabled_workflows=lambda: [],
        resource_aliases=alias_reader,
    )

    templates = await handlers.list_templates(None, None)
    uris = {template.uri_template for template in templates.resource_templates}
    assert uris == {
        "comfyui://workflows/{server_id}/{workflow_id}",
        "comfyui://workflows/{workflow_id}",
        "comfyui://revisions/{revision_id}",
        "comfyui://deployments/{deployment_id}",
    }

    expected_documents = {
        _CANONICAL_WORKFLOW_URI: {
            "workflow_id": _WORKFLOW_ID,
            "created_at": _CREATED_AT,
        },
        _CANONICAL_REVISION_URI: {
            "revision_id": _REVISION_ID,
            "workflow_id": _WORKFLOW_ID,
            "content_digest": "3" * 64,
            "created_at": _CREATED_AT,
        },
        _CANONICAL_DEPLOYMENT_URI: {
            "deployment_id": _DEPLOYMENT_ID,
            "workflow_id": _WORKFLOW_ID,
            "revision_id": _REVISION_ID,
            "server_id": "local",
            "enabled": True,
            "validation_status": "valid",
            "published": True,
            "created_at": _CREATED_AT,
            "content_digest": "3" * 64,
        },
    }
    for uri, expected in expected_documents.items():
        result = await handlers.read_resource(None, ReadResourceRequestParams(uri=uri))
        assert str(result.contents[0].uri) == uri
        assert json.loads(result.contents[0].text) == expected

    for uri in (_CANONICAL_ASSET_URI, _CANONICAL_JOB_URI, _CANONICAL_ARTIFACT_URI):
        with pytest.raises(MCPError) as captured:
            await handlers.read_resource(None, ReadResourceRequestParams(uri=uri))
        assert captured.value.code == -32602


@pytest.mark.anyio
async def test_enforced_resource_authorization_fails_closed_without_context(
    alias_reader: SQLiteLegacyResourceAliasReader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resource_adapter, "current_owner", lambda: _OWNER)
    monkeypatch.setattr(resource_adapter, "current_scopes", lambda: None)
    handlers = create_resource_handlers(
        catalog=Any,  # type: ignore[arg-type]
        servers=Any,  # type: ignore[arg-type]
        assets=_Assets(),  # type: ignore[arg-type]
        jobs=_Jobs(),  # type: ignore[arg-type]
        gateway_factory=Any,  # type: ignore[arg-type]
        enabled_workflows=lambda: [],
        resource_aliases=alias_reader,
        require_authorization=True,
    )

    templates = await handlers.list_templates(None, None)
    assert templates.resource_templates == []
    with pytest.raises(MCPError) as captured:
        await handlers.read_resource(None, ReadResourceRequestParams(uri=_CANONICAL_ASSET_URI))
    assert captured.value.code == -32602


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


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("filename", "subfolder", "storage_type"),
    [
        ("../private.png", "", "output"),
        ("private.png", "../secret", "output"),
        ("private.png", "", "input"),
        ("private.png", "", "temp"),
    ],
)
async def test_resource_download_rejects_unsafe_locator_and_storage_type(
    tmp_path: Path,
    filename: str,
    subfolder: str,
    storage_type: str,
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

    def forbidden_gateway(_config: dict[str, Any]) -> Any:
        raise AssertionError("unsafe locator reached the gateway")

    with pytest.raises(MCPError, match="Unsafe|Unsupported"):
        await resource_adapter._download_output(
            uri="comfyui://artifacts/artifact_" + "a" * 64,
            mime_type="image/png",
            server_id="local",
            filename=filename,
            subfolder=subfolder,
            storage_type=storage_type,
            servers=ServerRegistry(tmp_path),
            gateway_factory=forbidden_gateway,
        )
