"""Write and query execution history in data/{server_id}/{workflow_id}/history/."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock

from .storage import _safe_path

_STATUS_PRIORITY = {
    "reserved": 0,
    "submission_unknown": 0,
    "submitted": 1,
    "queued": 2,
    "running": 3,
    "cancelled": 4,
    "interrupted": 5,
    "error": 5,
    "success": 6,
}


def _hist_dir(base_dir: Path, server_id: str, workflow_id: str) -> Path:
    return _safe_path(base_dir, server_id, workflow_id) / "history"


def _record_path(
    base_dir: Path,
    server_id: str,
    workflow_id: str,
    external_id: str,
    *,
    kind: str,
) -> Path:
    digest = hashlib.sha256(external_id.encode("utf-8")).hexdigest()
    return _hist_dir(base_dir, server_id, workflow_id) / f"{kind}-{digest}.json"


def _request_digest(args: dict[str, Any]) -> str:
    payload = json.dumps(
        args, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def claim_job(
    base_dir: Path,
    server_id: str,
    workflow_id: str,
    job_id: str,
    args: dict[str, Any],
    client_id: str = "",
) -> str | bool:
    """Atomically reserve an idempotency key and return its fencing token."""
    if not job_id:
        raise ValueError("job_id must not be empty")
    path = _record_path(base_dir, server_id, workflow_id, job_id, kind="job")
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = _request_digest(args)
    with FileLock(f"{path}.lock", timeout=10):
        existing = _read_record(path)
        if existing:
            if existing.get("request_digest") not in {None, digest}:
                raise ValueError("job_id was already used with different arguments")
            if existing.get("status") != "reserved":
                return False
            try:
                claimed_at = datetime.fromisoformat(
                    str(existing.get("timestamp", ""))
                )
                age = (datetime.now(timezone.utc) - claimed_at).total_seconds()
            except ValueError:
                age = 301
            if age <= 300:
                return False
            path.unlink(missing_ok=True)
        lease_token = uuid.uuid4().hex
        record = {
            "run_id": job_id,
            "job_id": job_id,
            "prompt_id": "",
            "server_id": server_id,
            "workflow_id": workflow_id,
            "status": "reserved",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_ms": 0,
            "args": args,
            "request_digest": digest,
            "lease_token": lease_token,
            "client_id": client_id,
        }
        _atomic_write(path, record)
        return lease_token


def release_job_claim(
    base_dir: Path,
    server_id: str,
    workflow_id: str,
    job_id: str,
    lease_token: str,
) -> None:
    """Release only the caller's unsubmitted reservation."""
    path = _record_path(base_dir, server_id, workflow_id, job_id, kind="job")
    with FileLock(f"{path}.lock", timeout=10):
        existing = _read_record(path)
        if (
            existing
            and existing.get("status") == "reserved"
            and not existing.get("prompt_id")
            and existing.get("lease_token") == lease_token
        ):
            path.unlink(missing_ok=True)


def find_existing_run(
    base_dir: Path, server_id: str, workflow_id: str, job_id: str
) -> dict[str, Any] | None:
    path = _record_path(base_dir, server_id, workflow_id, job_id, kind="job")
    return _read_record(path)


def find_run_record(
    base_dir: Path, server_id: str, workflow_id: str, run_id: str
) -> dict[str, Any] | None:
    """Find a persisted job or prompt record by its public run identifier."""
    for kind in ("job", "prompt"):
        record = _read_record(
            _record_path(base_dir, server_id, workflow_id, run_id, kind=kind)
        )
        if record and run_id in {
            record.get("run_id"),
            record.get("job_id"),
            record.get("prompt_id"),
        }:
            return record
    return None


def renew_job_claim(
    base_dir: Path,
    server_id: str,
    workflow_id: str,
    job_id: str,
    lease_token: str,
) -> None:
    if not job_id:
        return
    path = _record_path(base_dir, server_id, workflow_id, job_id, kind="job")
    with FileLock(f"{path}.lock", timeout=10):
        existing = _read_record(path)
        if (
            not existing
            or existing.get("status") != "reserved"
            or existing.get("lease_token") != lease_token
        ):
            raise RuntimeError("job_id lease is no longer owned")
        existing["timestamp"] = datetime.now(timezone.utc).isoformat()
        _atomic_write(path, existing)


def save_run_record(
    base_dir: Path,
    server_id: str,
    workflow_id: str,
    prompt_id: str,
    args: dict[str, Any],
    status: str,
    *,
    job_id: str = "",
    lease_token: str = "",
    client_id: str = "",
    duration_ms: int = 0,
    outputs: list[dict[str, str]] | None = None,
    error: str = "",
) -> None:
    """Persist one execution record while honoring the claim fencing token."""
    file_id = job_id or prompt_id
    kind = "job" if job_id else "prompt"
    path = _record_path(base_dir, server_id, workflow_id, file_id, kind=kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(f"{path}.lock", timeout=10):
        existing = _read_record(path)
        if job_id and existing:
            existing_lease = str(existing.get("lease_token", ""))
            if existing_lease and existing_lease != lease_token:
                raise RuntimeError("job_id lease is no longer owned")
            if not existing_lease and (
                existing.get("prompt_id") != prompt_id
                or existing.get("request_digest") != _request_digest(args)
            ):
                raise RuntimeError("job_id record was finalized differently")
        if existing and _STATUS_PRIORITY.get(
            str(existing.get("status", "")), 0
        ) > _STATUS_PRIORITY.get(status, 0):
            return
        record = {
            "run_id": file_id,
            "job_id": job_id,
            "prompt_id": prompt_id,
            "server_id": server_id,
            "workflow_id": workflow_id,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_ms": duration_ms,
            "args": args,
            "request_digest": _request_digest(args),
        }
        if job_id:
            record["lease_token"] = lease_token or str(
                (existing or {}).get("lease_token", "")
            )
            record["client_id"] = client_id or str(
                (existing or {}).get("client_id", "")
            )
        if outputs:
            record["outputs"] = outputs
        if error:
            record["error"] = error
        _atomic_write(path, record)


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _atomic_write(path: Path, record: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as file:
            json.dump(record, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
