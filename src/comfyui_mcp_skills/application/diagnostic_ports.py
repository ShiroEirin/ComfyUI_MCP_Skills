"""Persistence boundaries for owner-bound diagnostics and immutable repair plans."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol


class DiagnosticRepository(Protocol):
    def get_job_diagnostic_context(
        self,
        job_id: str,
        owner_id: str,
        *,
        event_limit: int,
        log_line_limit: int,
    ) -> dict[str, Any] | None: ...

    def get_server_diagnostic_context(
        self,
        server_id: str,
        owner_id: str,
        *,
        event_limit: int,
        log_line_limit: int,
    ) -> dict[str, Any] | None: ...

    def save_diagnostic(self, report: dict[str, Any]) -> dict[str, Any]: ...

    def get_diagnostic(self, diagnostic_id: str, owner_id: str) -> dict[str, Any] | None: ...


class RetryRepository(Protocol):
    def get_retry_context(self, job_id: str, owner_id: str) -> dict[str, Any] | None: ...

    def save_repair_plan(self, plan: dict[str, Any]) -> dict[str, Any]: ...

    def get_repair_plan(self, repair_plan_id: str, owner_id: str) -> dict[str, Any] | None: ...
    def reserve_repair_plan_commit(
        self,
        repair_plan_id: str,
        plan_digest: str,
        owner_id: str,
        *,
        now: datetime,
    ) -> dict[str, Any]: ...

    def mark_repair_plan_committed(
        self,
        repair_plan_id: str,
        plan_digest: str,
        owner_id: str,
        retry_job_id: str,
        *,
        now: datetime,
    ) -> dict[str, Any]: ...
