"""Query, wait for, and safely cancel durable ComfyUI jobs."""

from __future__ import annotations

import logging
import mimetypes
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from comfyui_mcp_skills.application.ports import ComfyUIGateway, RunRepository
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import JobNotFound, ServerOffline, UnsafeCancel
from comfyui_mcp_skills.domain.models import Job

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"completed", "error", "interrupted", "cancelled", "lost"}
_VIDEO_EXTENSIONS = {".avi", ".gif", ".mkv", ".mov", ".mp4", ".webm"}
_AUDIO_EXTENSIONS = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav"}


class JobService:
    def __init__(
        self,
        servers: ServerRegistry,
        runs: RunRepository,
        gateway_factory: Callable[[dict[str, Any]], ComfyUIGateway],
    ) -> None:
        self._servers = servers
        self._runs = runs
        self._gateway_factory = gateway_factory

    def get(self, server_id: str, prompt_id: str, *, owner_id: str = "") -> Job:
        return self._get_until(server_id, prompt_id, owner_id=owner_id)

    def _get_until(
        self,
        server_id: str,
        prompt_id: str,
        *,
        owner_id: str,
        deadline: float | None = None,
    ) -> Job:
        if not prompt_id:
            raise JobNotFound("prompt_id must not be empty")
        saved = self._runs.get(server_id, prompt_id)
        self._authorize_owner(saved, prompt_id, owner_id)
        if saved is not None and saved.status == "lost":
            return saved
        gateway = self._gateway_factory(self._servers.connection(server_id))
        if deadline is None:
            history = gateway.get_history(prompt_id)
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._persisted_or_raise(saved, prompt_id)
            history = gateway.get_history(prompt_id, timeout_seconds=remaining)
        if history:
            status_info = history.get("status", {})
            status = str(status_info.get("status_str", "")).lower()
            if status in {"error", "interrupted", "cancelled"}:
                job = self._copy(
                    saved,
                    server_id,
                    prompt_id,
                    status,
                    error=self._format_errors(history),
                    owner_id=owner_id,
                )
            elif status_info.get("completed", False) or history.get("outputs"):
                outputs = tuple(self._outputs(server_id, prompt_id, history.get("outputs", {})))
                job = self._copy(
                    saved,
                    server_id,
                    prompt_id,
                    "completed",
                    outputs=outputs,
                    owner_id=owner_id,
                )
            else:
                job = self._copy(saved, server_id, prompt_id, "running", owner_id=owner_id)
            self._runs.save(job)
            return job
        if deadline is None:
            queue = gateway.get_queue()
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._persisted_or_raise(saved, prompt_id)
            queue = gateway.get_queue(timeout_seconds=remaining)
        if self._in_queue(queue.get("queue_running", []), prompt_id):
            job = self._copy(saved, server_id, prompt_id, "running", owner_id=owner_id)
            self._runs.save(job)
            return job
        if self._in_queue(queue.get("queue_pending", []), prompt_id):
            job = self._copy(saved, server_id, prompt_id, "queued", owner_id=owner_id)
            self._runs.save(job)
            return job
        return self._persisted_or_raise(saved, prompt_id)

    def wait(
        self,
        server_id: str,
        prompt_id: str,
        *,
        timeout_seconds: float,
        progress: Callable[[dict[str, Any]], None] | None = None,
        cancel_check: Callable[[], None] | None = None,
        owner_id: str = "",
    ) -> Job:
        """Wait within an absolute deadline while preserving the prompt handle."""
        deadline = time.monotonic() + max(timeout_seconds, 0)
        saved = self._runs.get(server_id, prompt_id)
        self._authorize_owner(saved, prompt_id, owner_id)
        gateway = self._gateway_factory(self._servers.connection(server_id))
        if cancel_check is not None:
            cancel_check()
        if saved and saved.client_id and time.monotonic() < deadline:
            try:
                for event in gateway.ws_events(
                    saved.client_id,
                    prompt_id,
                    max(0.0, deadline - time.monotonic()),
                    cancel_check,
                ):
                    if time.monotonic() >= deadline:
                        return self._persisted_or_raise(saved, prompt_id)
                    if cancel_check is not None:
                        cancel_check()
                    if progress is not None:
                        progress(event)
                    event_type = str(event.get("type", ""))
                    data = event.get("data", {})
                    if event_type in {"execution_error", "execution_interrupted"}:
                        break
                    if event_type == "executing" and data.get("node") is None:
                        break
                if time.monotonic() >= deadline:
                    return self._persisted_or_raise(saved, prompt_id)
                job = self._get_until(
                    server_id,
                    prompt_id,
                    owner_id=owner_id,
                    deadline=deadline,
                )
                if job.status in _TERMINAL_STATUSES:
                    return job
            except ServerOffline:
                logger.warning(
                    "ComfyUI WebSocket wait failed; falling back to polling",
                    exc_info=True,
                )
        while time.monotonic() < deadline:
            if cancel_check is not None:
                cancel_check()
            job = self._get_until(
                server_id,
                prompt_id,
                owner_id=owner_id,
                deadline=deadline,
            )
            if job.status in _TERMINAL_STATUSES:
                return job
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        return self._persisted_or_raise(saved, prompt_id)

    def cancel(self, server_id: str, prompt_id: str, *, owner_id: str = "") -> Job:
        if not prompt_id:
            raise JobNotFound("prompt_id must not be empty")
        existing = self._runs.get(server_id, prompt_id)
        self._authorize_owner(existing, prompt_id, owner_id)
        if existing is None:
            raise JobNotFound(f"Job not found: {prompt_id}")
        current = self.get(server_id, prompt_id, owner_id=owner_id)
        if current.status in _TERMINAL_STATUSES:
            return current
        gateway = self._gateway_factory(self._servers.connection(server_id))
        if current.status == "running":
            raise UnsafeCancel(
                "Safe targeted cancellation is unavailable for a running ComfyUI job"
            )
        gateway.queue_delete([prompt_id])
        history = gateway.get_history(prompt_id)
        if history:
            return self.get(server_id, prompt_id, owner_id=owner_id)
        queue = gateway.get_queue()
        if self._in_queue(queue.get("queue_running", []), prompt_id):
            return self._copy(current, server_id, prompt_id, "running", owner_id=owner_id)
        if self._in_queue(queue.get("queue_pending", []), prompt_id):
            return current
        job = self._copy(current, server_id, prompt_id, "cancelled", owner_id=owner_id)
        self._runs.save(job)
        return job

    def _persisted_or_raise(self, fallback: Job | None, prompt_id: str) -> Job:
        persisted = self._runs.get(fallback.server_id, prompt_id) if fallback is not None else None
        if persisted is not None:
            return persisted
        raise JobNotFound(f"Job not found: {prompt_id}")

    @staticmethod
    def _authorize_owner(saved: Job | None, prompt_id: str, owner_id: str) -> None:
        if owner_id and (saved is None or saved.owner_id != owner_id):
            raise JobNotFound(f"Job not found: {prompt_id}")

    @staticmethod
    def _copy(
        saved: Job | None,
        server_id: str,
        prompt_id: str,
        status: str,
        *,
        outputs: tuple[dict[str, Any], ...] | None = None,
        error: str | None = None,
        owner_id: str = "",
    ) -> Job:
        return Job(
            prompt_id=prompt_id,
            server_id=server_id,
            workflow_id=saved.workflow_id if saved else "",
            status=status,
            outputs=saved.outputs if outputs is None and saved else (outputs or ()),
            error=saved.error if error is None and saved else (error or ""),
            idempotency_key=saved.idempotency_key if saved else "",
            client_id=saved.client_id if saved else "",
            request_digest=saved.request_digest if saved else "",
            owner_id=saved.owner_id if saved else owner_id,
            job_id=saved.job_id if saved else "",
            plan_id=saved.plan_id if saved else "",
            revision_id=saved.revision_id if saved else "",
            deployment_id=saved.deployment_id if saved else "",
            plan_digest=saved.plan_digest if saved else "",
        )

    @staticmethod
    def _in_queue(items: list[Any], prompt_id: str) -> bool:
        return any(len(item) > 1 and item[1] == prompt_id for item in items)

    @staticmethod
    def _outputs(server_id: str, prompt_id: str, outputs: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for node_output in outputs.values():
            if not isinstance(node_output, dict):
                continue
            for key, fallback in (
                ("images", "image"),
                ("gifs", "video"),
                ("audio", "audio"),
                ("video", "video"),
            ):
                for item in node_output.get(key, []):
                    filename = str(item.get("filename", ""))
                    media_type = JobService._infer_media_type(filename, fallback)
                    mime_type = mimetypes.guess_type(filename)[0]
                    result.append(
                        {
                            "filename": filename,
                            "subfolder": str(item.get("subfolder", "")),
                            "type": str(item.get("type", "output")),
                            "media_type": media_type,
                            "mime_type": mime_type or "application/octet-stream",
                            "resource_uri": (
                                f"comfyui://outputs/{server_id}/{prompt_id}/{len(result)}"
                            ),
                        }
                    )
        return result

    @staticmethod
    def _infer_media_type(filename: str, fallback: str) -> str:
        extension = Path(filename).suffix.lower()
        if extension in _VIDEO_EXTENSIONS:
            return "video"
        if extension in _AUDIO_EXTENSIONS:
            return "audio"
        return fallback

    @staticmethod
    def _format_errors(history: dict[str, Any]) -> str:
        messages = history.get("status", {}).get("messages", [])
        parts = [
            str(message[1])
            for message in messages
            if isinstance(message, list) and len(message) >= 2
        ]
        return "; ".join(parts) if parts else "Workflow execution failed"
