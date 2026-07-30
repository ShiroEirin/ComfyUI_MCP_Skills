"""SQLite Unit of Work and minimal G0 transactional repositories."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from types import TracebackType
from typing import Any

from comfyui_mcp_skills.infrastructure.persistence.control_plane import _utc_now


class _Repository:
    def __init__(
        self,
        connection: sqlite3.Connection,
        ensure_open: Callable[[], None],
        mark_failed: Callable[[], None],
    ) -> None:
        self._connection = connection
        self._ensure_open = ensure_open
        self._mark_failed = mark_failed

    def _execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        self._ensure_open()
        try:
            return self._connection.execute(sql, parameters)
        except BaseException:
            self._mark_failed()
            raise

    def _json(self, value: object) -> str:
        self._ensure_open()
        try:
            return _encode_json(value)
        except BaseException:
            self._mark_failed()
            raise


class TestAggregateRepository(_Repository):
    """Persist the isolated aggregate used to prove the G0 transaction contract."""

    def add(self, aggregate_id: str, payload: dict[str, Any]) -> None:
        payload_json = self._json(payload)
        self._execute(
            """
            INSERT INTO test_aggregates(
                aggregate_id, payload_json, revision, created_at
            ) VALUES (?, ?, 0, ?)
            """,
            (aggregate_id, payload_json, _utc_now()),
        )


class WorkItemRepository(_Repository):
    """Persist one durable work item inside the active transaction."""

    def add(
        self,
        work_item_id: str,
        aggregate_id: str,
        work_type: str,
        payload: dict[str, Any],
    ) -> None:
        payload_json = self._json(payload)
        self._execute(
            """
            INSERT INTO work_items(
                work_item_id, aggregate_id, work_type, payload_json, status, created_at
            ) VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (
                work_item_id,
                aggregate_id,
                work_type,
                payload_json,
                _utc_now(),
            ),
        )


class EventRepository(_Repository):
    """Allocate per-subject sequences and persist immutable domain events."""

    def append(
        self,
        event_id: str,
        event_type: str,
        subject_uri: str,
        principal_id: str,
        correlation_id: str,
        data: dict[str, Any],
    ) -> int:
        row = self._execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1
            FROM domain_events
            WHERE subject_uri = ?
            """,
            (subject_uri,),
        ).fetchone()
        sequence = int(row[0])
        self._execute(
            """
            INSERT INTO domain_events(
                event_id, event_type, subject_uri, sequence, occurred_at,
                principal_id, correlation_id, data_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                event_type,
                subject_uri,
                sequence,
                _utc_now(),
                principal_id,
                correlation_id,
                self._json(data),
            ),
        )
        return sequence


class OutboxRepository(_Repository):
    """Persist a post-commit notification record without dispatching it."""

    def add(
        self,
        outbox_id: str,
        event_id: str,
        topic: str,
        payload: dict[str, Any],
    ) -> None:
        payload_json = self._json(payload)
        self._execute(
            """
            INSERT INTO outbox(
                outbox_id, event_id, topic, payload_json, status, created_at
            ) VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (outbox_id, event_id, topic, payload_json, _utc_now()),
        )


class SQLiteControlPlaneUnitOfWork:
    """Bind all G0 repositories to one explicit SQLite transaction."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect
        self._connection: sqlite3.Connection | None = None
        self._closed = True
        self._failed = False
        self._entered = False
        self.test_aggregates: TestAggregateRepository
        self.work_items: WorkItemRepository
        self.events: EventRepository
        self.outbox: OutboxRepository

    def __enter__(self) -> SQLiteControlPlaneUnitOfWork:
        if self._entered:
            raise RuntimeError("unit of work cannot be entered more than once")
        self._entered = True
        connection = self._connect()
        try:
            settings = (
                int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
                int(connection.execute("PRAGMA synchronous").fetchone()[0]),
                int(connection.execute("PRAGMA trusted_schema").fetchone()[0]),
                str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
            )
            if settings != (1, 2, 0, "wal"):
                raise RuntimeError("unit of work requires safe SQLite connection settings")
            connection.execute("BEGIN IMMEDIATE")
        except BaseException as exc:
            _close_preserving_error(connection, exc)
            raise
        self._connection = connection
        self._closed = False
        self.test_aggregates = TestAggregateRepository(
            connection, self._ensure_open, self._mark_failed
        )
        self.work_items = WorkItemRepository(connection, self._ensure_open, self._mark_failed)
        self.events = EventRepository(connection, self._ensure_open, self._mark_failed)
        self.outbox = OutboxRepository(connection, self._ensure_open, self._mark_failed)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        if not self._closed:
            if exc is None:
                self.rollback()
            else:
                connection = self._require_connection(allow_failed=True)
                self._closed = True
                self._connection = None
                _rollback_and_close_preserving_error(connection, exc)
        return None

    def commit(self) -> None:
        connection = self._require_connection(allow_failed=True)
        if self._failed:
            failure = RuntimeError("unit of work is failed and cannot be committed")
            try:
                self._rollback_and_close(connection)
            except BaseException as cleanup_error:
                _add_cleanup_note(failure, "rollback/close", cleanup_error)
            raise failure
        try:
            connection.commit()
        except BaseException as exc:
            self._closed = True
            self._connection = None
            _rollback_and_close_preserving_error(connection, exc)
            raise
        self._close(connection)

    def rollback(self) -> None:
        connection = self._require_connection(allow_failed=True)
        self._rollback_and_close(connection)

    def _ensure_open(self) -> None:
        self._require_connection()

    def _mark_failed(self) -> None:
        self._failed = True

    def _require_connection(self, *, allow_failed: bool = False) -> sqlite3.Connection:
        if self._closed or self._connection is None:
            raise RuntimeError("unit of work is closed")
        if self._failed and not allow_failed:
            raise RuntimeError("unit of work is failed and must be rolled back")
        return self._connection

    def _rollback_and_close(self, connection: sqlite3.Connection) -> None:
        self._closed = True
        self._connection = None
        try:
            connection.rollback()
        except BaseException as exc:
            _close_preserving_error(connection, exc)
            raise
        connection.close()

    def _close(self, connection: sqlite3.Connection) -> None:
        self._closed = True
        self._connection = None
        connection.close()


def _rollback_and_close_preserving_error(
    connection: sqlite3.Connection, original: BaseException
) -> None:
    try:
        connection.rollback()
    except BaseException as cleanup_error:
        _add_cleanup_note(original, "rollback", cleanup_error)
    _close_preserving_error(connection, original)


def _close_preserving_error(connection: sqlite3.Connection, original: BaseException) -> None:
    try:
        connection.close()
    except BaseException as cleanup_error:
        _add_cleanup_note(original, "connection close", cleanup_error)


def _add_cleanup_note(
    original: BaseException, operation: str, cleanup_error: BaseException
) -> None:
    add_note = getattr(original, "add_note", None)
    if add_note is not None:
        add_note(f"{operation} also failed: {cleanup_error!r}")
        return
    cleanup_error.__context__ = original.__context__
    original.__context__ = cleanup_error


def _encode_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must be canonical JSON") from exc
