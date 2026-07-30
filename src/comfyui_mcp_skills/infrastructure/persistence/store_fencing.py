"""Fail-closed fencing for legacy file repositories after aggregate cutover."""

from __future__ import annotations

import sqlite3
from pathlib import Path


class LegacyStoreSwitched(RuntimeError):
    """A stale file repository attempted access after durable cutover."""


def assert_file_store_active(base_dir: Path, aggregate_kinds: frozenset[str]) -> None:
    """Require that no aggregate in the file-backed group has switched."""
    database = base_dir.resolve() / "data" / "control-plane.sqlite3"
    if not database.exists():
        return
    connection = sqlite3.connect(database, isolation_level=None, timeout=5.0)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store_migrations'"
        ).fetchone()
        if table is None:
            return
        placeholders = ", ".join("?" for _ in aggregate_kinds)
        switched = connection.execute(
            f"SELECT aggregate_kind FROM store_migrations "
            f"WHERE aggregate_kind IN ({placeholders}) AND status = 'switched' LIMIT 1",
            tuple(sorted(aggregate_kinds)),
        ).fetchone()
    finally:
        connection.close()
    if switched is not None:
        raise LegacyStoreSwitched(
            f"legacy file store is fenced after {switched[0]} aggregate cutover"
        )
