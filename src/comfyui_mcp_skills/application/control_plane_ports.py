"""Application ports for the isolated G0 control-plane transaction slice."""

from __future__ import annotations

from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, TypeVar

from comfyui_mcp_skills.domain.orchestration import (
    JobReconciliationContext,
    OutboxMessage,
    WorkItem,
    WorkLease,
)


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


class OrchestrationRepository(Protocol):
    def acquire_next(
        self, worker_id: str, *, now: datetime, lease_seconds: int = 30
    ) -> WorkLease | None: ...
    def get_work_item(self, work_item_id: str) -> WorkItem: ...
    def job_context(self, job_id: str) -> JobReconciliationContext: ...
    def checkpoint(
        self,
        lease: WorkLease,
        checkpoint: dict[str, Any],
        *,
        now: datetime,
        delay_seconds: int = 0,
    ) -> None: ...
    def apply_reconciliation(
        self,
        lease: WorkLease,
        *,
        checkpoint: dict[str, Any],
        now: datetime,
        job_status: str | None = None,
        completed: bool = False,
        event_type: str = "",
        event_data: dict[str, Any] | None = None,
        generation: str = "",
        upstream_prompt_id: str = "",
        delay_seconds: int = 0,
    ) -> None: ...
    def job_owner_for_uri(self, uri: str) -> str | None: ...
    def pending_outbox(self, *, limit: int = 100) -> list[OutboxMessage]: ...
    def mark_outbox_delivered(self, outbox_id: str, *, now: datetime) -> None: ...


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
