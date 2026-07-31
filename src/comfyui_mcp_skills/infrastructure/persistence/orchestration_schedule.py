"""Atomic creation of the first durable G5 Job reconciliation step."""

from __future__ import annotations

import hashlib
import json
import sqlite3


def schedule_job_reconciliation(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    server_id: str,
    owner_id: str,
    occurred_at: str,
) -> str:
    """Add the first durable reconciliation step and notification in the caller transaction."""
    subject_uri = f"comfyui://jobs/{job_id}"
    work_item_id = _stable_id("work", job_id, "job.reconcile")
    event_id = _stable_id("event", job_id, "JOB_RECONCILIATION_SCHEDULED")
    outbox_id = _stable_id("outbox", event_id)
    payload = {"job_id": job_id, "server_id": server_id}
    payload_json = _encode(payload)
    connection.execute(
        """
        INSERT OR IGNORE INTO operation_work_items(
            work_item_id, subject_uri, work_type, payload_json, checkpoint_json,
            status, next_attempt_at, created_at, updated_at
        ) VALUES (?, ?, 'job.reconcile', ?, '{}', 'pending', ?, ?, ?)
        """,
        (work_item_id, subject_uri, payload_json, occurred_at, occurred_at, occurred_at),
    )
    row = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM domain_events WHERE subject_uri = ?",
        (subject_uri,),
    ).fetchone()
    sequence = int(row[0])
    connection.execute(
        """
        INSERT OR IGNORE INTO domain_events(
            event_id, event_type, subject_uri, sequence, occurred_at,
            principal_id, correlation_id, data_json
        ) VALUES (?, 'JOB_RECONCILIATION_SCHEDULED', ?, ?, ?, ?, ?, ?)
        """,
        (event_id, subject_uri, sequence, occurred_at, owner_id, job_id, payload_json),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO outbox(
            outbox_id, event_id, topic, payload_json, status, created_at
        ) VALUES (?, ?, 'resources.updated', ?, 'pending', ?)
        """,
        (
            outbox_id,
            event_id,
            _encode({"uri": subject_uri, "sequence": sequence, "owner_id": owner_id}),
            occurred_at,
        ),
    )
    return work_item_id


def _stable_id(prefix: str, *components: str) -> str:
    payload = json.dumps(
        [prefix, "g5-orchestration-v1", *components], separators=(",", ":")
    ).encode()
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()}"


def _encode(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
