"""One SQLite connection policy shared by the whole control plane.

Seven repositories used to open their own connection with a slightly different
PRAGMA set. Keeping a single factory means a safety setting can never drift
between the store that initializes the schema and the repositories that read
it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from comfyui_mcp_skills.domain.errors import SchemaMigrationError


def _close_preserving_error(
    connection: sqlite3.Connection, error: BaseException
) -> None:
    try:
        connection.close()
    except BaseException as cleanup_error:
        # Keep the original exception on top while retaining the close failure
        # for diagnosis, matching the store's existing cleanup contract.
        add_note = getattr(error, "add_note", None)
        if add_note is not None:
            add_note(f"connection close also failed: {cleanup_error!r}")
            return
        cleanup_error.__context__ = error.__context__
        error.__context__ = cleanup_error


def connect_control_plane(
    path: str | Path, *, require_wal: bool = False
) -> sqlite3.Connection:
    """Open one control-plane connection with the shared safety policy.

    ``require_wal`` is opt-in: the store that owns initialization requires it,
    while a repository may open a fresh or in-memory database that has not
    entered WAL yet.
    """
    connection = sqlite3.connect(path, isolation_level=None, timeout=5.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA trusted_schema = OFF")
        settings = (
            int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
            int(connection.execute("PRAGMA busy_timeout").fetchone()[0]),
            int(connection.execute("PRAGMA synchronous").fetchone()[0]),
            int(connection.execute("PRAGMA trusted_schema").fetchone()[0]),
        )
        if settings != (1, 5000, 2, 0):
            raise SchemaMigrationError(
                "SQLite connection safety settings are unavailable"
            )
        if require_wal:
            journal_mode = str(
                connection.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()
            if journal_mode != "wal":
                raise SchemaMigrationError("SQLite WAL journal mode is required")
        return connection
    except BaseException as exc:
        _close_preserving_error(connection, exc)
        raise
