"""MCP runtime projection for durable G5 orchestration and committed outbox events."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from functools import partial

import anyio
from mcp.server.subscriptions import SubscriptionBus
from mcp.shared.subscriptions import ResourceUpdated

from comfyui_mcp_skills.application.control_plane_ports import OrchestrationRepository
from comfyui_mcp_skills.application.orchestration import OperationOrchestrator

logger = logging.getLogger(__name__)


class OrchestrationRuntime:
    """Run leased work and project committed resource notifications without replay claims."""

    def __init__(
        self,
        orchestrator: OperationOrchestrator,
        repository: OrchestrationRepository,
        bus: SubscriptionBus,
        *,
        worker_id: str,
        idle_seconds: float = 1.0,
        owner_for_uri: Callable[[str], str | None] | None = None,
    ) -> None:
        if not worker_id or idle_seconds <= 0:
            raise ValueError("worker_id and positive idle_seconds are required")
        self._orchestrator = orchestrator
        self._repository = repository
        self._bus = bus
        self._worker_id = worker_id
        self._idle_seconds = idle_seconds
        self._owner_for_uri = owner_for_uri or repository.job_owner_for_uri

    async def run_worker(self) -> None:
        while True:
            try:
                progressed = await anyio.to_thread.run_sync(self._run_once)
            except Exception:
                logger.exception("Orchestration worker iteration failed")
                progressed = False
            if not progressed:
                await anyio.sleep(self._idle_seconds)

    async def run_outbox(self) -> None:
        while True:
            try:
                dispatched = await self.dispatch_outbox_once()
            except Exception:
                logger.exception("Outbox iteration failed")
                dispatched = 0
            if not dispatched:
                await anyio.sleep(self._idle_seconds)

    async def dispatch_outbox_once(self) -> int:
        messages = await anyio.to_thread.run_sync(self._repository.pending_outbox)
        dispatched = 0
        for message in messages:
            try:
                uri = message.payload.get("uri")
                expected_owner = message.payload.get("owner_id")
                if not isinstance(uri, str):
                    logger.error("Discarding invalid resource outbox message")
                    await self._mark_delivered(message.outbox_id)
                    continue
                actual_owner = await anyio.to_thread.run_sync(self._owner_for_uri, uri)
                if actual_owner is None or expected_owner != actual_owner:
                    logger.error("Discarding invalid resource outbox message")
                    await self._mark_delivered(message.outbox_id)
                    continue
                await self._bus.publish(ResourceUpdated(uri))
                await self._mark_delivered(message.outbox_id)
            except Exception:
                logger.exception("Resource outbox delivery failed; retaining for retry")
                continue
            dispatched += 1
        return dispatched

    async def _mark_delivered(self, outbox_id: str) -> None:
        await anyio.to_thread.run_sync(
            partial(self._repository.mark_outbox_delivered, outbox_id, now=_now())
        )

    def _run_once(self) -> bool:
        return self._orchestrator.run_once(self._worker_id, now=_now())


def _now() -> datetime:
    return datetime.now(timezone.utc)
