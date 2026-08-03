"""Strict aggregate cutover routing and legacy-file retention contracts."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from comfyui_mcp_skills.adapters.mcp import server as mcp_server_adapter
from comfyui_mcp_skills.infrastructure.persistence.assets import FileAssetRepository
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.repository_factory import (
    StoreRoutingError,
    create_repository_bundle,
)
from comfyui_mcp_skills.infrastructure.persistence.resource_aliases import (
    SQLiteLegacyResourceAliasReader,
)
from comfyui_mcp_skills.infrastructure.persistence.runs import FileRunRepository
from comfyui_mcp_skills.infrastructure.persistence.sqlite_assets import SQLiteAssetRepository
from comfyui_mcp_skills.infrastructure.persistence.sqlite_runs import SQLiteRunRepository
from comfyui_mcp_skills.infrastructure.persistence.store_fencing import LegacyStoreSwitched
from comfyui_mcp_skills.maintenance_main import run_maintenance

_JOB_GROUP = ("job", "execution_attempt", "idempotency_record", "artifact")
_DATABASE = Path("data/control-plane.sqlite3")


def _database(base_dir: Path) -> Path:
    return (base_dir / _DATABASE).resolve()


def _initialized_store(base_dir: Path) -> SQLiteControlPlaneStore:
    store = SQLiteControlPlaneStore(_database(base_dir))
    store.initialize()
    return store


def _switch(
    store: SQLiteControlPlaneStore,
    kinds: tuple[str, ...],
    *,
    version: int = 1,
    checksums: tuple[str, ...] | None = None,
    status: str = "switched",
) -> None:
    values = checksums or ("a" * 64,) * len(kinds)
    with sqlite3.connect(store.path) as connection:
        connection.executemany(
            """
            INSERT INTO store_migrations(
                aggregate_kind, version, status, checksum, switched_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    kind,
                    version,
                    status,
                    checksum,
                    "2026-07-30T00:00:00+00:00",
                )
                for kind, checksum in zip(kinds, values, strict=True)
            ],
        )


def test_missing_control_plane_database_routes_to_files_without_creating_it(
    tmp_path: Path,
) -> None:
    repositories = create_repository_bundle(tmp_path)

    assert isinstance(repositories.runs, FileRunRepository)
    assert isinstance(repositories.assets, FileAssetRepository)
    assert repositories.run_store == "file"
    assert repositories.asset_store == "file"
    assert not _database(tmp_path).exists()


def test_initialized_database_without_switch_rows_stays_on_files(tmp_path: Path) -> None:
    _initialized_store(tmp_path)

    repositories = create_repository_bundle(tmp_path)

    assert isinstance(repositories.runs, FileRunRepository)
    assert isinstance(repositories.assets, FileAssetRepository)
    assert repositories.run_store == "file"
    assert repositories.asset_store == "file"


def test_complete_job_and_asset_switches_route_both_ports_to_sqlite(tmp_path: Path) -> None:
    store = _initialized_store(tmp_path)
    _switch(store, _JOB_GROUP)
    _switch(store, ("asset",))

    repositories = create_repository_bundle(tmp_path)

    assert isinstance(repositories.runs, SQLiteRunRepository)
    assert isinstance(repositories.assets, SQLiteAssetRepository)
    assert repositories.run_store == "sqlite"
    assert repositories.asset_store == "sqlite"
    assert repositories.store is not None
    assert repositories.store.path == store.path
    assert repositories.runs._store is repositories.store  # type: ignore[attr-defined]
    assert repositories.assets._store is repositories.store  # type: ignore[attr-defined]


def test_mcp_resource_alias_reader_uses_repository_bundle_store(tmp_path: Path) -> None:
    store = _initialized_store(tmp_path)
    _switch(store, _JOB_GROUP)
    _switch(store, ("asset",))
    repositories = create_repository_bundle(tmp_path)
    original = mcp_server_adapter.create_resource_handlers
    captured: dict[str, object] = {}

    def capture_handlers(*args: object, **kwargs: object):
        captured["reader"] = kwargs.get("resource_aliases")
        return original(*args, **kwargs)  # type: ignore[arg-type]

    with patch.object(mcp_server_adapter, "create_resource_handlers", capture_handlers):
        mcp_server_adapter.create_server(tmp_path, repositories=repositories)

    reader = captured["reader"]
    assert isinstance(reader, SQLiteLegacyResourceAliasReader)
    assert reader._store is repositories.store  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("kinds", "checksums", "message"),
    [
        (("job",), None, "partial"),
        (_JOB_GROUP, ("a" * 64, "a" * 64, "b" * 64, "a" * 64), "conflicting"),
    ],
)
def test_partial_or_conflicting_job_switch_fails_closed(
    tmp_path: Path,
    kinds: tuple[str, ...],
    checksums: tuple[str, ...] | None,
    message: str,
) -> None:
    store = _initialized_store(tmp_path)
    _switch(store, kinds, checksums=checksums)

    with pytest.raises(StoreRoutingError, match=message):
        create_repository_bundle(tmp_path)


def test_superseded_cutover_evidence_never_routes_back_to_files(tmp_path: Path) -> None:
    store = _initialized_store(tmp_path)
    _switch(store, ("asset",), status="superseded")

    with pytest.raises(StoreRoutingError, match="cutover evidence"):
        create_repository_bundle(tmp_path)


def test_maintenance_preserves_legacy_files_after_corresponding_switches(
    tmp_path: Path,
) -> None:
    store = _initialized_store(tmp_path)
    _switch(store, _JOB_GROUP)
    _switch(store, ("asset",))
    run_path = tmp_path / "data/runs/server/prompts/old.json"
    asset_path = tmp_path / "data/assets/asset_old.json"
    for path, payload in (
        (run_path, {"prompt_id": "old", "status": "completed"}),
        (asset_path, {"asset_id": "asset_old"}),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        old = time.time() - 30 * 86_400
        os.utime(path, (old, old))

    result = run_maintenance(
        tmp_path,
        run_days=0,
        asset_days=0,
        max_history_records=0,
    )

    assert result == {
        "runs_deleted": 0,
        "assets_deleted": 0,
        "experiment_plans_deleted": 0,
        "experiment_terminal_plans_pruned": 0,
        "experiment_terminal_payloads_compacted": 0,
    }
    assert run_path.exists()
    assert asset_path.exists()


def test_stale_file_repositories_fail_closed_after_cutover(tmp_path: Path) -> None:
    stale_runs = FileRunRepository(tmp_path)
    stale_assets = FileAssetRepository(tmp_path)
    store = _initialized_store(tmp_path)
    _switch(store, _JOB_GROUP)
    _switch(store, ("asset",))

    with pytest.raises(LegacyStoreSwitched):
        stale_runs.get("local", "prompt-1")
    with pytest.raises(LegacyStoreSwitched):
        stale_assets.get("asset_" + "a" * 32)
