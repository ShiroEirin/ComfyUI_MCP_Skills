"""Atomic file-backed job and idempotency repository."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from filelock import FileLock

from comfyui_mcp_skills.domain.models import Job
from comfyui_mcp_skills.infrastructure.persistence.migration_lock import (
    project_migration_lock,
)
from comfyui_mcp_skills.infrastructure.persistence.store_fencing import (
    assert_file_store_active,
)

_STATUS_PRIORITY = {
    "reserved": 0,
    "submission_unknown": 0,
    "submitted": 1,
    "queued": 2,
    "running": 3,
    "completed": 4,
    "cancelled": 5,
    "interrupted": 5,
    "error": 5,
}


class FileRunRepository:
    def __init__(self, base_dir: Path) -> None:
        data_root = (base_dir.resolve() / "data").resolve()
        data_root.mkdir(parents=True, exist_ok=True)
        self._root = data_root / "runs"
        self._retention_lock = FileLock(str(data_root / ".retention.lock"), timeout=10)
        self._generation_path = data_root / ".retention-generation"
        self._migration_lock = project_migration_lock(base_dir)
        self._base_dir = base_dir.resolve()

    def claim(
        self,
        server_id: str,
        workflow_id: str,
        idempotency_key: str,
        arguments: dict[str, Any],
        owner_id: str = "",
        client_id: str = "",
        request_digest: str | None = None,
    ) -> str | None:
        if not idempotency_key:
            return ""
        path = self._idempotency_path(server_id, idempotency_key, owner_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if request_digest is None:
            request_digest = self.request_digest(workflow_id, arguments)
        with self._migration_lock, self._retention_lock:
            self._assert_active()
            with self._lock(path):
                existing = self._read_record(path)
                if existing is not None:
                    claimed_at = float(existing.get("claimed_at", 0))
                    active = existing.get("status") != "reserved" or time.time() - claimed_at <= 300
                    if active:
                        return None
                    path.unlink(missing_ok=True)
                lease_token = uuid.uuid4().hex
                record = {
                    "server_id": server_id,
                    "workflow_id": workflow_id,
                    "idempotency_key": idempotency_key,
                    "owner_id": owner_id,
                    "prompt_id": "",
                    "status": "reserved",
                    "request_digest": request_digest,
                    "claimed_at": time.time(),
                    "client_id": client_id,
                    "lease_token": lease_token,
                }
                with path.open("x", encoding="utf-8") as file:
                    json.dump(record, file, ensure_ascii=False)
                    file.flush()
                    os.fsync(file.fileno())
                self._bump_generation()
                return lease_token

    def get_claim(self, server_id: str, key: str, owner_id: str = "") -> dict[str, Any] | None:
        path = self._idempotency_path(server_id, key, owner_id)
        with self._migration_lock, self._retention_lock:
            self._assert_active()
            with self._lock(path):
                return self._read_record(path)

    def release_claim(
        self,
        server_id: str,
        key: str,
        request_digest: str,
        lease_token: str,
        owner_id: str = "",
    ) -> None:
        if not key:
            return
        path = self._idempotency_path(server_id, key, owner_id)
        with self._migration_lock, self._retention_lock:
            self._assert_active()
            with self._lock(path):
                record = self._read_record(path)
                if (
                    record
                    and not record.get("prompt_id")
                    and record.get("request_digest") == request_digest
                    and record.get("lease_token") == lease_token
                ):
                    path.unlink(missing_ok=True)
                    self._bump_generation()

    def mark_submission_unknown(
        self,
        server_id: str,
        key: str,
        lease_token: str,
        owner_id: str = "",
    ) -> None:
        if not key:
            return
        path = self._idempotency_path(server_id, key, owner_id)
        with self._migration_lock, self._retention_lock:
            self._assert_active()
            with self._lock(path):
                record = self._read_record(path)
                if record and record.get("lease_token") == lease_token:
                    record["status"] = "submission_unknown"
                    self._atomic_write(path, record)
                    self._bump_generation()

    @staticmethod
    def request_digest(workflow_id: str, arguments: dict[str, Any]) -> str:
        payload = json.dumps(
            {"workflow_id": workflow_id, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def save(self, job: Job, *, lease_token: str = "") -> None:
        with self._migration_lock, self._retention_lock:
            self._assert_active()
            self._save_locked(job, lease_token=lease_token)
            self._bump_generation()

    def _save_locked(self, job: Job, *, lease_token: str) -> None:
        prompt_path = self._prompt_path(job.server_id, job.prompt_id)
        if job.idempotency_key:
            idempotency_path = self._idempotency_path(
                job.server_id, job.idempotency_key, job.owner_id
            )
            with self._lock(idempotency_path):
                current = self._read_record(idempotency_path)
                current_lease = str((current or {}).get("lease_token", ""))
                same_finalized = bool(
                    current
                    and current.get("prompt_id") == job.prompt_id
                    and current.get("request_digest") == job.request_digest
                    and current.get("owner_id", "") == job.owner_id
                    and current.get("workflow_id") == job.workflow_id
                )
                if current_lease:
                    if not lease_token or current_lease != lease_token:
                        raise RuntimeError("Idempotency lease is no longer owned")
                elif current is not None:
                    if not same_finalized:
                        raise RuntimeError("Idempotency record was finalized differently")
                    if lease_token:
                        return
                elif current is None:
                    raise RuntimeError("Idempotency lease no longer exists")
                if current is not None and _STATUS_PRIORITY.get(
                    str(current.get("status", "")), 0
                ) > _STATUS_PRIORITY.get(job.status, 0):
                    return
                self._atomic_write(prompt_path, self._serialize(job))
                self._atomic_write(idempotency_path, self._serialize(job))
            return
        with self._lock(prompt_path):
            current = self._read_record(prompt_path)
            if current is not None and _STATUS_PRIORITY.get(
                str(current.get("status", "")), 0
            ) > _STATUS_PRIORITY.get(job.status, 0):
                return
            self._atomic_write(prompt_path, self._serialize(job))

    def get(self, server_id: str, prompt_id: str) -> Job | None:
        with self._migration_lock:
            self._assert_active()
            return self._read_job(self._prompt_path(server_id, prompt_id))

    def get_by_idempotency(self, server_id: str, key: str, owner_id: str = "") -> Job | None:
        with self._migration_lock:
            self._assert_active()
            return self._read_job(self._idempotency_path(server_id, key, owner_id))

    def list_jobs(
        self,
        owner_id: str,
        *,
        limit: int,
        status: str = "",
        workflow_id: str = "",
        server_id: str = "",
        created_after: str = "",
        after_created_at: str = "",
        after_job_id: str = "",
    ) -> list[dict[str, str]]:
        raise NotImplementedError("job listing is unsupported for file-backed run repositories")

    def _assert_active(self) -> None:
        assert_file_store_active(
            self._base_dir,
            frozenset({"job", "execution_attempt", "idempotency_record", "artifact"}),
        )

    def _bump_generation(self) -> None:
        temporary = self._generation_path.with_name(
            f".{self._generation_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(uuid.uuid4().hex, encoding="ascii")
            os.replace(temporary, self._generation_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _serialize(job: Job) -> dict[str, Any]:
        return {
            "prompt_id": job.prompt_id,
            "server_id": job.server_id,
            "workflow_id": job.workflow_id,
            "status": job.status,
            "outputs": list(job.outputs),
            "error": job.error,
            "idempotency_key": job.idempotency_key,
            "client_id": job.client_id,
            "request_digest": job.request_digest,
            "owner_id": job.owner_id,
        }

    def _read_job(self, path: Path) -> Job | None:
        data = self._read_record(path)
        if not data or not data.get("prompt_id"):
            return None
        data["outputs"] = tuple(data.get("outputs", []))
        return Job(**{key: data[key] for key in Job.__dataclass_fields__ if key in data})

    @staticmethod
    def _read_record(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _atomic_write(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _prompt_path(self, server_id: str, prompt_id: str) -> Path:
        return self._root / self._digest(server_id) / "prompts" / f"{self._digest(prompt_id)}.json"

    def _idempotency_path(self, server_id: str, key: str, owner_id: str = "") -> Path:
        namespace = f"{owner_id}\0{key}"
        return (
            self._root / self._digest(server_id) / "idempotency" / f"{self._digest(namespace)}.json"
        )

    @staticmethod
    def _lock(path: Path) -> FileLock:
        path.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(f"{path}.lock", timeout=10)

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
