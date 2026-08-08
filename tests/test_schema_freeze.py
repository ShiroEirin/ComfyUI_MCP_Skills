"""Schema freeze contract: released migrations are append-only; every released
version upgrades to the current schema preserving seeded data."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from comfyui_mcp_skills.infrastructure.persistence import control_plane as control_plane_module
from comfyui_mcp_skills.infrastructure.persistence.control_plane import (
    RELEASED_SCHEMA_MIGRATIONS,
    SchemaMigration,
    SchemaMigrationError,
    SQLiteControlPlaneStore,
)

_WORKFLOW_ID = "freeze_workflow"
_JOB_ID = "job_" + "c" * 32
_ASSET_ID = "asset_" + "d" * 32
_OWNER = "owner-freeze"
_CREATED_AT = "2026-08-01T00:00:00+00:00"


def _database(tmp_path: Path) -> Path:
    return (tmp_path / "control-plane.sqlite3").resolve()


def _seed_base_data(store: SQLiteControlPlaneStore, *, version: int) -> None:
    """Seed one row per base table that exists since v1.

    v1 jobs has no ``execution_origin`` column (introduced by the v2 rebuild);
    versions >= 2 write it explicitly. Both paths end with
    ``execution_origin='legacy_migrated'`` after upgrade.
    """
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO workflows(workflow_id, created_at) VALUES (?, ?)",
            (_WORKFLOW_ID, _CREATED_AT),
        )
        if version >= 2:
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, workflow_id, owner_id, status, created_at,
                    created_at_source, legacy_migrated, execution_origin
                ) VALUES (?, ?, ?, 'completed', ?, 'test', 1, 'legacy_migrated')
                """,
                (_JOB_ID, _WORKFLOW_ID, _OWNER, _CREATED_AT),
            )
        else:
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, workflow_id, owner_id, status, created_at,
                    created_at_source, legacy_migrated
                ) VALUES (?, ?, ?, 'completed', ?, 'test', 1)
                """,
                (_JOB_ID, _WORKFLOW_ID, _OWNER, _CREATED_AT),
            )
        connection.execute(
            """
            INSERT INTO assets(
                asset_id, owner_id, server_id, name, subfolder, media_type,
                mime_type, size_bytes, sha256, source_type, comfyui_ref, created_at
            ) VALUES (?, ?, 'local', 'seed.png', '', 'image', 'image/png', 0,
                      '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
                      'test', 'input/seed.png', ?)
            """,
            (_ASSET_ID, _OWNER, _CREATED_AT),
        )


def _assert_seed_data_preserved(store: SQLiteControlPlaneStore) -> None:
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT workflow_id, created_at FROM workflows WHERE workflow_id=?",
            (_WORKFLOW_ID,),
        ).fetchone() == (_WORKFLOW_ID, _CREATED_AT)
        assert connection.execute(
            "SELECT asset_id, name FROM assets WHERE asset_id=?", (_ASSET_ID,)
        ).fetchone() == (_ASSET_ID, "seed.png")
        job = connection.execute(
            """
            SELECT job_id, workflow_id, owner_id, status, legacy_migrated,
                   execution_origin, plan_id, revision_id, deployment_id
            FROM jobs WHERE job_id=?
            """,
            (_JOB_ID,),
        ).fetchone()
        assert job[:5] == (_JOB_ID, _WORKFLOW_ID, _OWNER, "completed", 1)
        assert job[5] == "legacy_migrated"  # v2-derived field, filled by migration
        assert job[6:] == (None, None, None)


def test_released_migrations_match_frozen_spec() -> None:
    migrations = control_plane_module._MIGRATIONS
    assert len(migrations) >= len(RELEASED_SCHEMA_MIGRATIONS)
    for migration, spec in zip(migrations, RELEASED_SCHEMA_MIGRATIONS):
        assert (migration.version, migration.name, migration.checksum) == spec
    versions = [migration.version for migration in migrations]
    assert versions == list(range(1, len(migrations) + 1))


@pytest.mark.parametrize(
    "version", range(1, len(RELEASED_SCHEMA_MIGRATIONS) + 1)
)
def test_upgrade_from_released_version(version: int, tmp_path: Path, monkeypatch) -> None:
    # Bound to the frozen spec: every released historical prefix must upgrade.
    # Extending the frozen spec (publishing a new migration) extends this matrix.
    assert version <= len(RELEASED_SCHEMA_MIGRATIONS)
    migrations = control_plane_module._MIGRATIONS
    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", migrations[:version])
    store = SQLiteControlPlaneStore(_database(tmp_path))
    store.initialize()
    _seed_base_data(store, version=version)

    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", migrations)
    store.initialize()
    store.initialize()  # idempotent repeat

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT max(version) FROM schema_migrations"
        ).fetchone() == (len(migrations),)
        assert connection.execute(
            "SELECT count(*) FROM schema_migrations"
        ).fetchone() == (len(migrations),)
    _assert_seed_data_preserved(store)


def test_modifying_released_migration_fails_fast(tmp_path: Path, monkeypatch) -> None:
    migrations = control_plane_module._MIGRATIONS
    renamed = (
        SchemaMigration(
            migrations[0].version,
            "tampered-initial-control-plane",
            migrations[0].up,
            migrations[0].down,
            feasibility_note=migrations[0].feasibility_note,
            bootstrap_sql=migrations[0].bootstrap_sql,
        ),
    ) + migrations[1:]
    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", renamed)

    with pytest.raises(SchemaMigrationError, match="freezing"):
        SQLiteControlPlaneStore(_database(tmp_path)).initialize()


def test_appending_new_migration_is_allowed(tmp_path: Path, monkeypatch) -> None:
    migrations = control_plane_module._MIGRATIONS
    future = migrations + (
        SchemaMigration(
            len(migrations) + 1,
            "test-future-migration",
            ("CREATE TABLE _freeze_test(x INTEGER)",),
            (),
        ),
    )
    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", future)

    store = SQLiteControlPlaneStore(_database(tmp_path))
    store.initialize()

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT max(version) FROM schema_migrations"
        ).fetchone() == (len(migrations) + 1,)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='_freeze_test'"
        ).fetchone() is not None


def test_future_database_rejected_by_older_code(tmp_path: Path, monkeypatch) -> None:
    migrations = control_plane_module._MIGRATIONS
    store = SQLiteControlPlaneStore(_database(tmp_path))
    store.initialize()  # current schema (v12)

    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", migrations[:-1])
    with pytest.raises(SchemaMigrationError, match="unknown schema migration"):
        SQLiteControlPlaneStore(_database(tmp_path)).initialize()
