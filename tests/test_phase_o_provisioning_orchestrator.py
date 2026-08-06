"""Focused Phase O provisioning orchestration contracts."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import requests

from comfyui_mcp_skills.application.orchestration import OperationOrchestrator
from comfyui_mcp_skills.application.provisioning import ProvisioningWorkHandler
from comfyui_mcp_skills.domain.orchestration import (
    PROVISIONING_WORK_TYPE,
    WorkItem,
    WorkLease,
)
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.orchestration import (
    SQLiteOrchestrationRepository,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _ProvisioningRepository:
    """Small durable-state double exposing the public provisioning port."""

    def __init__(self, *, item_checkpoint: dict[str, Any] | None = None) -> None:
        self.item_checkpoint = dict(item_checkpoint or {})
        self.item_status = "pending"
        self.item_result: dict[str, Any] | None = None
        self.work_status = "pending"
        self.work_checkpoint: dict[str, Any] = {}
        self.current_lease: WorkLease | None = None
        self.lease_expiry = _NOW
        self.next_fence = 0
        self.crash_after_checkpoint = False
        self.finish_calls: list[bool] = []
        self.complete_calls = 0

    def acquire_next(
        self, worker_id: str, *, now: datetime, lease_seconds: int = 30
    ) -> WorkLease | None:
        if self.work_status not in {"pending", "running"}:
            return None
        if self.current_lease is not None and self.lease_expiry > now:
            return None
        self.next_fence += 1
        self.lease_expiry = now + timedelta(seconds=lease_seconds)
        self.current_lease = WorkLease(
            "work-1", worker_id, self.next_fence, self.lease_expiry.isoformat()
        )
        self.work_status = "running"
        return self.current_lease

    def get_work_item(self, work_item_id: str) -> WorkItem:
        return WorkItem(
            work_item_id,
            "comfyui://provisioning/jobs/job-1",
            PROVISIONING_WORK_TYPE,
            {"job_id": "job-1", "owner_id": "owner-1"},
            dict(self.work_checkpoint),
            self.work_status,  # type: ignore[arg-type]
        )

    def get_work_context(self, job_id: str, owner_id: str) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "owner_id": owner_id,
            "server_id": "server-1",
            "status": "running",
            "server": {"url": "https://comfy.example.test"},
            "items": [
                {
                    "item_id": "item-1",
                    "status": self.item_status,
                    "checkpoint": dict(self.item_checkpoint),
                    "kind": "node",
                    "source_type": "git",
                    "source_url": "https://example.test/node.git",
                    "version": "0123456789abcdef0123456789abcdef01234567",
                    "checksum": "a" * 64,
                    "size_bytes": 1,
                    "target_dir": "custom_nodes",
                    "restart_required": False,
                }
            ],
        }

    def renew_lease(self, lease: WorkLease, *, now: datetime, lease_seconds: int = 30) -> WorkLease:
        self._require_lease(lease, now)
        self.lease_expiry = now + timedelta(seconds=lease_seconds)
        renewed = WorkLease(
            lease.work_item_id, lease.worker_id, lease.fencing_token, self.lease_expiry.isoformat()
        )
        self.current_lease = renewed
        return renewed

    def release_lease(self, lease: WorkLease, *, now: datetime) -> None:
        self._require_lease(lease, now)
        self.lease_expiry = now

    def claim_item_for_enqueue(
        self,
        lease: WorkLease,
        *,
        job_id: str,
        owner_id: str,
        item_id: str,
        queue_id: str,
        now: datetime,
    ) -> dict[str, Any] | None:
        self._require_lease(lease, now)
        if self.item_status != "pending":
            return None
        self.item_checkpoint = {
            "enqueue_started": True,
            "queue_id": queue_id,
            "state": "enqueue_started",
        }
        return {"item_id": item_id, "kind": "node", "source_url": "https://example.test/node.git"}

    def save_item_checkpoint(
        self,
        lease: WorkLease,
        *,
        job_id: str,
        owner_id: str,
        item_id: str,
        checkpoint: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        self._require_lease(lease, now)
        self.item_checkpoint = dict(checkpoint)
        if self.crash_after_checkpoint:
            self.crash_after_checkpoint = False
            raise RuntimeError("simulated worker crash after durable checkpoint")
        return dict(checkpoint)

    def complete_item(
        self,
        lease: WorkLease,
        *,
        job_id: str,
        owner_id: str,
        item_id: str,
        result: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        self._require_lease(lease, now)
        if self.item_status == "completed":
            return dict(self.item_result or result)
        self.complete_calls += 1
        self.item_status = "completed"
        self.item_result = dict(result)
        self.item_checkpoint = dict(result)
        return dict(result)

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
    ) -> None:
        self._require_lease(lease, now)
        self.work_checkpoint = dict(checkpoint)
        self.work_status = "completed" if completed else "pending"
        self.finish_calls.append(completed)
        self.lease_expiry = now

    def owner_for_uri(self, uri: str) -> str | None:
        return "owner-1" if uri == "comfyui://provisioning/jobs/job-1" else None

    def pending_outbox(self, owner_id: str | None = None, *, limit: int = 100) -> list[Any]:
        return []

    def mark_outbox_delivered(self, outbox_id: str, *, now: datetime) -> None:
        return None

    def _require_lease(self, lease: WorkLease, now: datetime) -> None:
        if self.current_lease != lease or self.lease_expiry <= now:
            raise RuntimeError("work lease is expired or fenced")


class _Manager:
    def __init__(self, observations: list[dict[str, Any]]) -> None:
        self.observations = iter(observations)
        self.enqueues: list[str] = []
        self.observed: list[str] = []

    def inspect(self, server: dict[str, Any]) -> dict[str, Any]:
        return {"available": True}

    def enqueue_install(
        self, server: dict[str, Any], item: dict[str, Any], *, queue_id: str
    ) -> dict[str, Any]:
        self.enqueues.append(queue_id)
        return next(self.observations)

    def observe_install(
        self, server: dict[str, Any], queue_id: str, *, item: dict[str, Any]
    ) -> dict[str, Any]:
        self.observed.append(queue_id)
        return next(self.observations)


def _handler(repo: _ProvisioningRepository, manager: _Manager) -> ProvisioningWorkHandler:
    return ProvisioningWorkHandler(repo, manager, retry_delay_seconds=1)


def test_crash_after_enqueue_receipt_recovers_without_second_enqueue() -> None:
    repo = _ProvisioningRepository()
    repo.crash_after_checkpoint = True
    manager = _Manager([{"state": "queued"}, {"state": "completed"}])
    handler = _handler(repo, manager)
    orchestrator = OperationOrchestrator(repo, {PROVISIONING_WORK_TYPE: handler}, lease_seconds=30)

    with pytest.raises(RuntimeError, match="simulated worker crash"):
        orchestrator.run_once("worker-1", now=_NOW)

    assert repo.item_checkpoint["enqueue_started"] is True
    assert len(manager.enqueues) == 1

    assert orchestrator.run_once("worker-2", now=_NOW + timedelta(seconds=31))
    assert len(manager.enqueues) == 1
    assert len(manager.observed) == 1
    assert repo.item_status == "completed"


def test_manager_timeout_remains_pending_and_retries_observation() -> None:
    repo = _ProvisioningRepository()

    class _TimeoutManager(_Manager):
        def enqueue_install(
            self, server: dict[str, Any], item: dict[str, Any], *, queue_id: str
        ) -> dict[str, Any]:
            self.enqueues.append(queue_id)
            raise requests.Timeout("Manager timed out")

    timeout_manager = _TimeoutManager([])
    orchestrator = OperationOrchestrator(
        repo,
        {PROVISIONING_WORK_TYPE: _handler(repo, timeout_manager)},
        lease_seconds=30,
    )

    assert orchestrator.run_once("worker-1", now=_NOW)
    assert repo.work_status == "pending"
    assert repo.item_status == "pending"
    assert repo.item_checkpoint["state"] == "unknown"
    assert repo.finish_calls == [False]


def test_terminal_manager_observation_completes_once() -> None:
    repo = _ProvisioningRepository(item_checkpoint={"enqueue_started": True, "queue_id": "queue-1"})
    manager = _Manager([{"state": "completed"}])
    orchestrator = OperationOrchestrator(
        repo,
        {PROVISIONING_WORK_TYPE: _handler(repo, manager)},
        lease_seconds=30,
    )

    assert orchestrator.run_once("worker-1", now=_NOW)
    assert orchestrator.run_once("worker-1", now=_NOW + timedelta(seconds=1))
    assert not orchestrator.run_once("worker-1", now=_NOW + timedelta(seconds=2))
    assert manager.enqueues == []
    assert manager.observed == ["queue-1"]
    assert repo.complete_calls == 1
    assert repo.finish_calls.count(True) == 1


def test_unknown_observation_reaches_bound_and_fails_item() -> None:
    """A lost enqueue must fail the item after bounded unknown retries."""
    repo = _ProvisioningRepository()
    repo.crash_after_checkpoint = True
    manager = _Manager([{"state": "unknown", "retryable": True}] * 6)
    handler = _handler(repo, manager)
    orchestrator = OperationOrchestrator(repo, {PROVISIONING_WORK_TYPE: handler}, lease_seconds=30)

    # enqueue returns unknown, then the durable checkpoint save crashes
    with pytest.raises(RuntimeError, match="simulated worker crash"):
        orchestrator.run_once("worker-1", now=_NOW)
    assert repo.item_checkpoint["enqueue_started"] is True
    assert repo.item_checkpoint["unknown_count"] == 1

    for attempt in range(4):
        orchestrator.run_once(
            "worker-1", now=_NOW + timedelta(seconds=31 * (attempt + 1))
        )
        assert repo.item_status == "pending"
        assert repo.item_checkpoint["unknown_count"] == attempt + 2

    orchestrator.run_once("worker-1", now=_NOW + timedelta(seconds=31 * 5))
    assert repo.item_status == "completed"
    assert repo.item_result["state"] == "failed"
    assert repo.item_result["error"] == "manager_queue_unknown_timeout"
    assert len(manager.observed) == 5


def test_provisioning_lease_transfer_rejects_stale_fencing_token(tmp_path: Path) -> None:
    store = SQLiteControlPlaneStore((tmp_path / "control-plane.sqlite3").resolve())
    store.initialize()
    now = _NOW
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            INSERT INTO operation_work_items(
                work_item_id, subject_uri, work_type, payload_json, checkpoint_json,
                status, next_attempt_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                "work-1",
                "comfyui://provisioning/jobs/job-1",
                PROVISIONING_WORK_TYPE,
                json.dumps({"job_id": "job-1", "owner_id": "owner-1"}),
                "{}",
                now.isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
        connection.commit()

    repository = SQLiteOrchestrationRepository(store)
    first = repository.acquire_next("worker-1", now=now, lease_seconds=1)
    assert first is not None
    second = repository.acquire_next("worker-2", now=now + timedelta(seconds=2), lease_seconds=1)
    assert second is not None
    assert second.fencing_token == first.fencing_token + 1

    with pytest.raises(RuntimeError, match="expired or fenced"):
        repository.checkpoint(first, {"stale": True}, now=now + timedelta(seconds=2))

    repository.checkpoint(second, {"step": "recovered"}, now=now + timedelta(seconds=2))
    assert repository.get_work_item("work-1").checkpoint == {"step": "recovered"}
