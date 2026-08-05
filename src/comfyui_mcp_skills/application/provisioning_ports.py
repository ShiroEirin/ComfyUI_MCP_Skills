"""Application persistence and Manager boundaries for Phase O provisioning."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from comfyui_mcp_skills.domain.orchestration import OutboxMessage, WorkLease


class ServerControlRepository(Protocol):
    def save_server_plan(self, plan: dict[str, Any]) -> dict[str, Any]: ...

    def commit_server_plan(
        self,
        plan_id: str,
        plan_digest: str,
        owner_id: str,
        *,
        now: datetime,
    ) -> dict[str, Any]: ...

    def list_servers(self, owner_id: str) -> list[dict[str, Any]]: ...

    def get_server(self, server_id: str, owner_id: str) -> dict[str, Any] | None: ...

    def server_delete_impact(self, server_id: str, owner_id: str) -> dict[str, Any]: ...


class ConfigBundleRepository(Protocol):
    def current_revision(self, owner_id: str) -> int: ...

    def export_snapshot(self, owner_id: str) -> dict[str, Any]: ...

    def save_bundle(self, bundle: dict[str, Any]) -> dict[str, Any]: ...

    def get_bundle(self, revision: int, owner_id: str) -> dict[str, Any] | None: ...

    def save_import_plan(self, plan: dict[str, Any]) -> None: ...

    def commit_import_plan(
        self,
        plan_id: str,
        plan_digest: str,
        owner_id: str,
        *,
        now: datetime,
    ) -> dict[str, Any]: ...


class ProvisioningRepository(Protocol):
    def inspect_dependencies(
        self,
        owner_id: str,
        server_id: str,
        workflow_id: str,
        revision_id: str,
    ) -> dict[str, Any]: ...
    def get_server(self, server_id: str, owner_id: str) -> dict[str, Any] | None: ...

    def save_plan(self, plan: dict[str, Any], items: list[dict[str, Any]]) -> None: ...

    def get_plan(self, plan_id: str, owner_id: str) -> dict[str, Any] | None: ...

    def create_approval(
        self,
        plan_id: str,
        plan_digest: str,
        owner_id: str,
        *,
        now: datetime,
    ) -> dict[str, Any]: ...

    def get_approval(
        self,
        approval_id: str,
        owner_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None: ...

    def save_approval_plan(self, plan: dict[str, Any]) -> None: ...

    def commit_approval_plan(
        self,
        approval_plan_id: str,
        plan_digest: str,
        owner_id: str,
        *,
        now: datetime,
    ) -> dict[str, Any]: ...

    def commit_plan(
        self,
        plan_id: str,
        plan_digest: str,
        approval_id: str,
        owner_id: str,
        request_id: str,
        confirmation: str,
        *,
        now: datetime,
    ) -> dict[str, Any]: ...

    def save_cancel_plan(self, plan: dict[str, Any]) -> None: ...

    def commit_cancel_plan(
        self,
        cancel_plan_id: str,
        plan_digest: str,
        owner_id: str,
        *,
        now: datetime,
    ) -> dict[str, Any]: ...

    def get_job(self, job_id: str, owner_id: str) -> dict[str, Any] | None: ...

    def get_work_context(self, job_id: str, owner_id: str) -> dict[str, Any]: ...

    def renew_lease(
        self,
        lease: WorkLease,
        *,
        now: datetime,
        lease_seconds: int = 30,
    ) -> WorkLease: ...

    def release_lease(self, lease: WorkLease, *, now: datetime) -> None: ...

    def claim_item_for_enqueue(
        self,
        lease: WorkLease,
        *,
        job_id: str,
        owner_id: str,
        item_id: str,
        queue_id: str,
        now: datetime,
    ) -> dict[str, Any] | None: ...

    def save_item_checkpoint(
        self,
        lease: WorkLease,
        *,
        job_id: str,
        owner_id: str,
        item_id: str,
        checkpoint: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]: ...

    def complete_item(
        self,
        lease: WorkLease,
        *,
        job_id: str,
        owner_id: str,
        item_id: str,
        result: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]: ...

    def finish_work(
        self,
        lease: WorkLease,
        *,
        job_id: str,
        owner_id: str,
        checkpoint: dict[str, Any],
        now: datetime,
        completed: bool,
        delay_seconds: int,
        status: str,
    ) -> None: ...

    def owner_for_uri(self, uri: str) -> str | None: ...

    def pending_outbox(
        self, owner_id: str | None = None, *, limit: int = 100
    ) -> list[OutboxMessage]: ...

    def mark_outbox_delivered(self, outbox_id: str, *, now: datetime) -> None: ...


class ManagerGateway(Protocol):
    """Safe, non-shell Manager operations consumed by ProvisioningWorkHandler."""

    def inspect(self, server: dict[str, Any]) -> dict[str, Any]: ...

    def enqueue_install(
        self, server: dict[str, Any], item: dict[str, Any], *, queue_id: str
    ) -> dict[str, Any]: ...

    def observe_install(
        self,
        server: dict[str, Any],
        queue_id: str,
        *,
        item: dict[str, Any],
    ) -> dict[str, Any]: ...
