"""SQLite control-plane Unit of Work transaction contracts."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from comfyui_mcp_skills.infrastructure.persistence.control_plane import (
    SchemaMigrationError,
    SQLiteControlPlaneStore,
)
from comfyui_mcp_skills.infrastructure.persistence.control_plane_uow import (
    SQLiteControlPlaneUnitOfWork,
)


def _transaction_table_counts(database: Path) -> tuple[int, int, int, int]:
    with sqlite3.connect(database) as connection:
        return tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("test_aggregates", "work_items", "domain_events", "outbox")
        )


def test_unit_of_work_atomically_commits_aggregate_work_event_and_outbox(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control-plane.sqlite3"
    store = SQLiteControlPlaneStore(database)
    store.initialize()

    with store.unit_of_work() as unit_of_work:
        unit_of_work.test_aggregates.add("aggregate-1", {"value": 1})
        unit_of_work.work_items.add("work-1", "aggregate-1", "test.execute", {"step": 1})
        sequence = unit_of_work.events.append(
            "event-1",
            "test.created",
            "comfyui://tests/aggregate-1",
            "principal-1",
            "correlation-1",
            {"value": 1},
        )
        unit_of_work.outbox.add("outbox-1", "event-1", "resource.updated", {"event_id": "event-1"})
        unit_of_work.commit()

    assert sequence == 1
    assert _transaction_table_counts(database) == (1, 1, 1, 1)
    with sqlite3.connect(database) as connection:
        aggregate_payload = connection.execute(
            "SELECT payload_json FROM test_aggregates WHERE aggregate_id = 'aggregate-1'"
        ).fetchone()[0]
    assert aggregate_payload == '{"value":1}'


def test_unit_of_work_rolls_back_when_commit_is_omitted(tmp_path: Path) -> None:
    database = tmp_path / "control-plane.sqlite3"
    store = SQLiteControlPlaneStore(database)
    store.initialize()

    with store.unit_of_work() as unit_of_work:
        unit_of_work.test_aggregates.add("aggregate-1", {"value": 1})

    assert _transaction_table_counts(database) == (0, 0, 0, 0)


def test_unit_of_work_failure_rolls_back_every_repository(tmp_path: Path) -> None:
    database = tmp_path / "control-plane.sqlite3"
    store = SQLiteControlPlaneStore(database)
    store.initialize()

    with pytest.raises(sqlite3.IntegrityError):
        with store.unit_of_work() as unit_of_work:
            unit_of_work.test_aggregates.add("aggregate-1", {})
            unit_of_work.work_items.add("work-1", "aggregate-1", "test.execute", {})
            unit_of_work.events.append(
                "event-1",
                "test.created",
                "comfyui://tests/aggregate-1",
                "principal-1",
                "correlation-1",
                {},
            )
            unit_of_work.outbox.add("outbox-1", "event-1", "resource.updated", {})
            unit_of_work.outbox.add("outbox-1", "event-1", "resource.updated", {})

    assert _transaction_table_counts(database) == (0, 0, 0, 0)


def test_committed_unit_of_work_is_closed_to_further_writes(tmp_path: Path) -> None:
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()

    with store.unit_of_work() as unit_of_work:
        repository = unit_of_work.test_aggregates
        repository.add("aggregate-1", {})
        unit_of_work.commit()
        with pytest.raises(RuntimeError, match="closed"):
            repository.add("aggregate-2", {})
        with pytest.raises(RuntimeError, match="closed"):
            unit_of_work.commit()


def test_unit_of_work_instance_cannot_be_reentered_after_commit(tmp_path: Path) -> None:
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()
    unit_of_work = store.unit_of_work()

    with unit_of_work:
        unit_of_work.commit()

    with pytest.raises(RuntimeError, match="more than once"):
        with unit_of_work:
            pass


def test_event_sequences_are_atomic_per_subject_under_concurrency(tmp_path: Path) -> None:
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()
    barrier = Barrier(2)

    def append_event(index: int) -> int:
        barrier.wait(timeout=5)
        with store.unit_of_work() as unit_of_work:
            sequence = unit_of_work.events.append(
                f"event-{index}",
                "test.progress",
                "comfyui://tests/shared",
                "principal-1",
                f"correlation-{index}",
                {"index": index},
            )
            unit_of_work.commit()
            return sequence

    with ThreadPoolExecutor(max_workers=2) as executor:
        sequences = sorted(executor.map(append_event, (1, 2)))

    assert sequences == [1, 2]


def test_unit_of_work_enforces_foreign_keys_on_its_shared_connection(
    tmp_path: Path,
) -> None:
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()

    with pytest.raises(sqlite3.IntegrityError):
        with store.unit_of_work() as unit_of_work:
            unit_of_work.work_items.add("work-1", "missing", "test.execute", {})


def test_unit_of_work_cannot_commit_after_a_caught_repository_failure(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control-plane.sqlite3"
    store = SQLiteControlPlaneStore(database)
    store.initialize()

    with store.unit_of_work() as unit_of_work:
        unit_of_work.test_aggregates.add("aggregate-1", {})
        with pytest.raises(sqlite3.IntegrityError):
            unit_of_work.test_aggregates.add("aggregate-1", {})
        with pytest.raises(RuntimeError, match="failed"):
            unit_of_work.test_aggregates.add("aggregate-2", {})
        with pytest.raises(RuntimeError, match="failed"):
            unit_of_work.commit()

    assert _transaction_table_counts(database) == (0, 0, 0, 0)


def test_unit_of_work_rejects_database_when_wal_was_disabled(tmp_path: Path) -> None:
    database = tmp_path / "control-plane.sqlite3"
    store = SQLiteControlPlaneStore(database)
    store.initialize()
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode = DELETE").fetchone() == ("delete",)

    with pytest.raises(SchemaMigrationError, match="WAL"):
        with store.unit_of_work():
            pass


class _FakeCursor:
    def __init__(self, value: object) -> None:
        self._value = value

    def fetchone(self) -> tuple[object]:
        return (self._value,)


class _CleanupFailingConnection:
    def execute(self, statement: str) -> _FakeCursor:
        if "foreign_keys" in statement:
            return _FakeCursor(1)
        if "synchronous" in statement:
            return _FakeCursor(2)
        if "trusted_schema" in statement:
            return _FakeCursor(0)
        if "journal_mode" in statement:
            return _FakeCursor("wal")
        return _FakeCursor(None)

    def commit(self) -> None:
        raise ValueError("commit failed")

    def rollback(self) -> None:
        raise RuntimeError("rollback failed")

    def close(self) -> None:
        raise OSError("close failed")


def _cleanup_diagnostics(error: BaseException) -> list[str]:
    notes = getattr(error, "__notes__", None)
    if notes is not None:
        return list(notes)
    diagnostics: list[str] = []
    context = error.__context__
    seen: set[int] = set()
    while context is not None and id(context) not in seen:
        seen.add(id(context))
        diagnostics.append(str(context))
        context = context.__context__
    return diagnostics


def test_unit_of_work_preserves_commit_error_when_cleanup_also_fails() -> None:
    unit_of_work = SQLiteControlPlaneUnitOfWork(_CleanupFailingConnection)
    unit_of_work.__enter__()

    with pytest.raises(ValueError, match="commit failed") as raised:
        unit_of_work.commit()

    diagnostics = _cleanup_diagnostics(raised.value)
    assert any("rollback" in message for message in diagnostics)
    assert any("close" in message for message in diagnostics)


def test_unit_of_work_preserves_rollback_error_when_close_also_fails() -> None:
    unit_of_work = SQLiteControlPlaneUnitOfWork(_CleanupFailingConnection)
    unit_of_work.__enter__()

    with pytest.raises(RuntimeError, match="rollback failed") as raised:
        unit_of_work.rollback()
    diagnostics = _cleanup_diagnostics(raised.value)
    assert any("close" in message for message in diagnostics)
