"""Ports implemented by ComfyUI transport adapters."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Generator, Protocol

from comfyui_mcp_skills.domain.models import Asset, Job, Workflow


class WorkflowRepository(Protocol):
    def list(self) -> list[Workflow]: ...
    def get(self, server_id: str, workflow_id: str) -> Workflow | None: ...


class RunRepository(Protocol):
    def claim(
        self,
        server_id: str,
        workflow_id: str,
        idempotency_key: str,
        arguments: dict[str, Any],
        owner_id: str = "",
        client_id: str = "",
    ) -> str | None: ...
    def get_claim(
        self, server_id: str, key: str, owner_id: str = ""
    ) -> dict[str, Any] | None: ...
    def release_claim(
        self,
        server_id: str,
        key: str,
        request_digest: str,
        lease_token: str,
        owner_id: str = "",
    ) -> None: ...
    def mark_submission_unknown(
        self,
        server_id: str,
        key: str,
        lease_token: str,
        owner_id: str = "",
    ) -> None: ...
    def request_digest(self, workflow_id: str, arguments: dict[str, Any]) -> str: ...
    def save(self, job: Job, *, lease_token: str = "") -> None: ...
    def get(self, server_id: str, prompt_id: str) -> Job | None: ...
    def get_by_idempotency(
        self, server_id: str, key: str, owner_id: str = ""
    ) -> Job | None: ...


class AssetRepository(Protocol):
    def save(self, asset: Asset) -> None: ...
    def get(self, asset_id: str) -> Asset | None: ...


class ComfyUIGateway(Protocol):
    def queue_prompt(self, workflow: dict[str, Any], **kwargs: Any) -> dict[str, Any]: ...
    def get_history_list(self, max_items: int = 20, offset: int = 0) -> dict[str, Any]: ...
    def get_history(self, prompt_id: str) -> dict[str, Any] | None: ...
    def get_queue(self) -> dict[str, Any]: ...
    def interrupt(self, prompt_id: str = "") -> dict[str, Any]: ...
    def queue_delete(self, prompt_ids: list[str]) -> dict[str, Any]: ...
    def upload_file(
        self, path: str, *, purpose: str, original_ref: str
    ) -> dict[str, Any]: ...
    def download_output(
        self,
        filename: str,
        subfolder: str = "",
        output_type: str = "output",
        *,
        max_bytes: int,
    ) -> bytes: ...
    def ws_events(
        self,
        client_id: str,
        prompt_id: str,
        timeout_seconds: float | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> Generator[dict[str, Any], None, None]: ...
