"""Durable G5 orchestration value objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

WorkStatus = Literal["pending", "running", "completed", "failed"]
ReconcileState = Literal[
    "unknown",
    "missing",
    "queued",
    "running",
    "completed",
    "error",
    "cancelled",
    "interrupted",
]
PROVISIONING_WORK_TYPE = "provisioning.execute"


@dataclass(frozen=True, slots=True)
class WorkItem:
    work_item_id: str
    subject_uri: str
    work_type: str
    payload: dict[str, Any]
    checkpoint: dict[str, Any]
    status: WorkStatus


@dataclass(frozen=True, slots=True)
class WorkLease:
    work_item_id: str
    worker_id: str
    fencing_token: int
    expires_at: str


@dataclass(frozen=True, slots=True)
class JobReconciliationContext:
    status: str
    server_id: str
    prompt_id: str
    upstream_job_id: str
    client_id: str


@dataclass(frozen=True, slots=True)
class ReconcileObservation:
    online: bool
    generation: str = ""
    state: ReconcileState = "unknown"
    error: str = ""
    upstream_prompt_id: str = ""


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    outbox_id: str
    topic: str
    payload: dict[str, Any]
