"""Shared rate and concurrency limit contracts backed by SQLite."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

_WINDOW_SECONDS = 60
_BUSY_TIMEOUT_MS = 5000

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS shared_rate_limits (
        mode TEXT NOT NULL,
        rate_key TEXT NOT NULL,
        window INTEGER NOT NULL,
        count INTEGER NOT NULL CHECK(count >= 0),
        PRIMARY KEY (mode, rate_key, window)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shared_permits (
        permit_key TEXT NOT NULL,
        permit_id TEXT NOT NULL,
        mode TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        PRIMARY KEY (permit_key, permit_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shared_subscription_counts (
        mode TEXT NOT NULL,
        subject TEXT NOT NULL,
        active INTEGER NOT NULL CHECK(active >= 0),
        PRIMARY KEY (mode, subject)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_shared_permits_expiry ON shared_permits(expires_at)",
    "CREATE INDEX IF NOT EXISTS ix_shared_rates_expiry ON shared_rate_limits(window)",
)


class SharedLimitStore(Protocol):
    def consume_rate_limit(
        self, mode: str, rate_key: str, *, limit: int, window_seconds: int = 60
    ) -> int | None: ...
    def acquire_permit(
        self, mode: str, permit_id: str, permit_key: str, *, ttl_seconds: int, maximum: int
    ) -> bool: ...
    def release_permit(self, mode: str, permit_id: str, permit_key: str) -> bool: ...
    def acquire_subscription(self, mode: str, subject: str, *, maximum: int) -> bool: ...
    def release_subscription(self, mode: str, subject: str) -> None: ...
    def prune_expired(self) -> int: ...


class SharedLimitsUnavailable(RuntimeError):
    """The configured shared limit backend is missing, unreadable, or misconfigured."""


def _open(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database, isolation_level=None, timeout=5.0)
    connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA trusted_schema = OFF")
    return connection


class SQLiteSharedLimitStore:
    """Cross-process fixed-window limits and permit leases in one SQLite file."""

    def __init__(self, database: Path) -> None:
        self.database = Path(database).resolve()
        try:
            connection = _open(self.database)
            try:
                connection.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.commit()
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            raise SharedLimitsUnavailable(str(exc)) from exc

    def consume_rate_limit(
        self, mode: str, rate_key: str, *, limit: int, window_seconds: int = 60
    ) -> int | None:
        now = int(time.time())
        window = now - (now % window_seconds)
        try:
            connection = _open(self.database)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM shared_rate_limits WHERE window < ?", (now - window_seconds,)
                )
                row = connection.execute(
                    "SELECT count FROM shared_rate_limits WHERE mode=? AND rate_key=? AND window=?",
                    (mode, rate_key, window),
                ).fetchone()
                current = int(row[0]) if row is not None else 0
                if current >= limit:
                    connection.rollback()
                    return None
                connection.execute(
                    """
                    INSERT INTO shared_rate_limits(mode, rate_key, window, count)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(mode, rate_key, window)
                    DO UPDATE SET count = count + 1
                    """,
                    (mode, rate_key, window),
                )
                connection.commit()
                return current + 1
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            raise SharedLimitsUnavailable(str(exc)) from exc

    def acquire_permit(
        self, mode: str, permit_id: str, permit_key: str, *, ttl_seconds: int, maximum: int
    ) -> bool:
        if maximum <= 0:
            raise ValueError("maximum must be positive")
        expires_at = int(time.time()) + ttl_seconds
        try:
            connection = _open(self.database)
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT 1 FROM shared_permits WHERE permit_key=? AND permit_id=?",
                    (permit_key, permit_id),
                ).fetchone()
                if existing is not None:
                    connection.rollback()
                    return True
                active = connection.execute(
                    "SELECT count(*) FROM shared_permits WHERE permit_key=? AND expires_at>?",
                    (permit_key, int(time.time())),
                ).fetchone()[0]
                if int(active) >= maximum:
                    connection.rollback()
                    return False
                connection.execute(
                    """
                    INSERT INTO shared_permits(permit_key, permit_id, mode, expires_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (permit_key, permit_id, mode, expires_at),
                )
                connection.commit()
                return True
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            raise SharedLimitsUnavailable(str(exc)) from exc

    def release_permit(self, mode: str, permit_id: str, permit_key: str) -> bool:
        try:
            connection = _open(self.database)
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "DELETE FROM shared_permits WHERE permit_key=? AND permit_id=? AND mode=?",
                    (permit_key, permit_id, mode),
                )
                connection.commit()
                return cursor.rowcount == 1
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            raise SharedLimitsUnavailable(str(exc)) from exc

    def acquire_subscription(self, mode: str, subject: str, *, maximum: int) -> bool:
        try:
            connection = _open(self.database)
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT active FROM shared_subscription_counts WHERE mode=? AND subject=?",
                    (mode, subject),
                ).fetchone()
                current = int(row[0]) if row is not None else 0
                if current >= maximum:
                    connection.rollback()
                    return False
                connection.execute(
                    """
                    INSERT INTO shared_subscription_counts(mode, subject, active)
                    VALUES (?, ?, 1)
                    ON CONFLICT(mode, subject) DO UPDATE SET active = active + 1
                    """,
                    (mode, subject),
                )
                connection.commit()
                return True
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            raise SharedLimitsUnavailable(str(exc)) from exc

    def release_subscription(self, mode: str, subject: str) -> None:
        try:
            connection = _open(self.database)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE shared_subscription_counts SET active = active - 1 "
                    "WHERE mode=? AND subject=? AND active > 0",
                    (mode, subject),
                )
                connection.commit()
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            raise SharedLimitsUnavailable(str(exc)) from exc

    def prune_expired(self) -> int:
        try:
            connection = _open(self.database)
            try:
                connection.execute("BEGIN IMMEDIATE")
                now = int(time.time())
                cursor = connection.execute(
                    "DELETE FROM shared_permits WHERE expires_at <= ?", (now,)
                )
                connection.commit()
                return cursor.rowcount
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            raise SharedLimitsUnavailable(str(exc)) from exc


def shared_limit_store(database: Path) -> SQLiteSharedLimitStore:
    return SQLiteSharedLimitStore(database)


def limit_store_factory(database_path: Path) -> Callable[[], SQLiteSharedLimitStore]:
    def build() -> SQLiteSharedLimitStore:
        return SQLiteSharedLimitStore(database_path)

    return build
