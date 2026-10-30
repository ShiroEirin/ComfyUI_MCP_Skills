"""Regression: a default Server must be digestible after the row_factory change.

The shared connection factory sets ``row_factory = sqlite3.Row``. The config
snapshot used to store a bare ``fetchone()`` in ``default_server``, which then
became non-serializable and broke publish/rollback with ``TypeError``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from comfyui_mcp_skills.infrastructure.persistence.control_plane import (
    SQLiteControlPlaneStore,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_workflows import (
    _advance_config_state,
)


def _connection(path: Path) -> sqlite3.Connection:
    from comfyui_mcp_skills.infrastructure.persistence.sqlite_connection import (
        connect_control_plane,
    )

    return connect_control_plane(path)


def _seed_active_default(connection: sqlite3.Connection) -> None:
    """Create the legal server revision before setting a default server."""
    connection.execute(
        "INSERT INTO server_change_plans(plan_id,plan_digest,owner_id,operation,server_id,"
        "changes_json,expected_revision,impact_json,created_at,expires_at,resource_uri) "
        "VALUES('plan-x',?,'owner-a','upsert','local','{}',NULL,'{}',"
        "'2026-10-29T00:00:00Z','2026-10-30T00:00:00Z','comfyui://servers/local')",
        ("a" * 64,),
    )
    connection.execute(
        "INSERT INTO server_revisions(server_id,owner_id,revision,lifecycle_status,config_json,"
        "config_digest,plan_id,created_at) VALUES('local','owner-a',1,'active','{}',?,"
        "'plan-x','2026-10-29T00:00:00Z')",
        ("b" * 64,),
    )
    connection.execute(
        "INSERT INTO managed_servers(server_id,owner_id,current_revision,current_digest,"
        "lifecycle_status,created_at,updated_at) VALUES('local','owner-a',1,?,'active',"
        "'2026-10-29T00:00:00Z','2026-10-29T00:00:00Z')",
        ("b" * 64,),
    )
    connection.execute(
        "INSERT INTO server_defaults(owner_id,server_id,server_revision,plan_id,updated_at) "
        "VALUES('owner-a','local',1,'plan-x','2026-10-29T00:00:00Z')"
    )


def test_config_snapshot_serializes_a_default_server_row(tmp_path: Path) -> None:
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()
    connection = _connection(store.path)
    try:
        _seed_active_default(connection)

        _advance_config_state(connection, "owner-a", "2026-10-29T00:00:00Z")

        row = connection.execute(
            "SELECT current_digest FROM config_state WHERE owner_id='owner-a'"
        ).fetchone()
        assert row is not None
        # The digest must still describe the default server, not a placeholder.
        expected = hashlib.sha256(
            json.dumps(
                {
                    "servers": [
                        ("local", 1, "b" * 64, "active"),
                    ],
                    "default_server": ["local"],
                    "workflows": [],
                },
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        assert row[0] == expected
    finally:
        connection.close()


def test_config_snapshot_without_default_server_stays_null(tmp_path: Path) -> None:
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()
    connection = _connection(store.path)
    try:
        _advance_config_state(connection, "owner-a", "2026-10-29T00:00:00Z")

        row = connection.execute(
            "SELECT current_digest FROM config_state WHERE owner_id='owner-a'"
        ).fetchone()
        assert row is not None
        expected = hashlib.sha256(
            json.dumps(
                {"servers": [], "default_server": None, "workflows": []},
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        assert row[0] == expected
    finally:
        connection.close()
