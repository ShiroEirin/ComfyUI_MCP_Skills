"""Application ports for the isolated G0 control-plane transaction slice."""

from __future__ import annotations

from types import TracebackType
from typing import Any, Protocol, TypeVar


class TestAggregateRepository(Protocol):
    def add(self, aggregate_id: str, payload: dict[str, Any]) -> None: ...


class WorkItemRepository(Protocol):
    def add(
        self,
        work_item_id: str,
        aggregate_id: str,
        work_type: str,
        payload: dict[str, Any],
    ) -> None: ...


class EventRepository(Protocol):
    def append(
        self,
        event_id: str,
        event_type: str,
        subject_uri: str,
        principal_id: str,
        correlation_id: str,
        data: dict[str, Any],
    ) -> int: ...


class OutboxRepository(Protocol):
    def add(
        self,
        outbox_id: str,
        event_id: str,
        topic: str,
        payload: dict[str, Any],
    ) -> None: ...


_ControlPlaneUnitOfWorkT = TypeVar("_ControlPlaneUnitOfWorkT", bound="ControlPlaneUnitOfWork")


class ControlPlaneUnitOfWork(Protocol):
    """One explicit transaction shared by every G0 control-plane repository."""

    @property
    def test_aggregates(self) -> TestAggregateRepository: ...

    @property
    def work_items(self) -> WorkItemRepository: ...

    @property
    def events(self) -> EventRepository: ...

    @property
    def outbox(self) -> OutboxRepository: ...

    def __enter__(
        self: _ControlPlaneUnitOfWorkT,
    ) -> _ControlPlaneUnitOfWorkT: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...
