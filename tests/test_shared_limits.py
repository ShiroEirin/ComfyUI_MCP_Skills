"""Shared SQLite limit store contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfyui_mcp_skills.application.shared_limits import (
    SharedLimitsUnavailable,
    SQLiteSharedLimitStore,
)


def test_two_store_instances_share_one_rate_window(tmp_path: Path) -> None:
    database = tmp_path / "limits.sqlite3"
    first = SQLiteSharedLimitStore(database)
    second = SQLiteSharedLimitStore(database)

    assert first.consume_rate_limit("http", "10.0.0.1", limit=2) == 1
    assert second.consume_rate_limit("http", "10.0.0.1", limit=2) == 2
    assert first.consume_rate_limit("http", "10.0.0.1", limit=2) is None
    assert first.consume_rate_limit("http", "10.0.0.2", limit=2) == 1


def test_permit_is_idempotent_per_request_and_released_once(tmp_path: Path) -> None:
    store = SQLiteSharedLimitStore(tmp_path / "limits.sqlite3")

    assert (
        store.acquire_permit("http", "permit-1", "normal", ttl_seconds=60, maximum=2)
        is True
    )
    assert (
        store.acquire_permit("http", "permit-1", "normal", ttl_seconds=60, maximum=2)
        is True
    )
    assert store.release_permit("http", "permit-1", "normal") is True
    assert store.release_permit("http", "permit-1", "normal") is False


def test_permit_enforces_maximum_concurrency_per_key(tmp_path: Path) -> None:
    store = SQLiteSharedLimitStore(tmp_path / "limits.sqlite3")

    assert (
        store.acquire_permit("http", "permit-1", "normal", ttl_seconds=60, maximum=2)
        is True
    )
    assert (
        store.acquire_permit("http", "permit-2", "normal", ttl_seconds=60, maximum=2)
        is True
    )
    assert (
        store.acquire_permit("http", "permit-3", "normal", ttl_seconds=60, maximum=2)
        is False
    )
    assert store.release_permit("http", "permit-1", "normal") is True
    assert (
        store.acquire_permit("http", "permit-3", "normal", ttl_seconds=60, maximum=2)
        is True
    )


def test_subscription_quota_is_shared_and_released(tmp_path: Path) -> None:
    store = SQLiteSharedLimitStore(tmp_path / "limits.sqlite3")

    assert store.acquire_subscription("http", "principal:owner", maximum=2) is True
    assert store.acquire_subscription("http", "principal:owner", maximum=2) is True
    assert store.acquire_subscription("http", "principal:owner", maximum=2) is False
    store.release_subscription("http", "principal:owner")
    assert store.acquire_subscription("http", "principal:owner", maximum=2) is True


def test_unreadable_database_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "limits.sqlite3"
    database.write_text("not a sqlite database", encoding="utf-8")

    with pytest.raises(SharedLimitsUnavailable):
        SQLiteSharedLimitStore(database)
