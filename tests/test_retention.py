"""Retention policy contracts for MCP metadata."""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import json
import os
import time
from pathlib import Path

from filelock import FileLock

from comfyui_mcp_skills.domain.models import Asset, Job
from comfyui_mcp_skills.infrastructure.persistence.assets import FileAssetRepository
from comfyui_mcp_skills.infrastructure.persistence.runs import FileRunRepository
from comfyui_mcp_skills.infrastructure.persistence.retention import FileRetentionService


def _record(path: Path, payload: dict[str, object], *, age_days: int = 30) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    old = time.time() - age_days * 86_400
    os.utime(path, (old, old))


def test_retention_preserves_active_and_idempotency_referenced_metadata(
    tmp_path: Path,
) -> None:
    prompts = tmp_path / "data" / "runs" / "server" / "prompts"
    idempotency = tmp_path / "data" / "runs" / "server" / "idempotency"
    assets = tmp_path / "data" / "assets"
    _record(prompts / "active.json", {"prompt_id": "active", "status": "running"})
    _record(prompts / "kept.json", {"prompt_id": "kept", "status": "completed"})
    _record(prompts / "expired.json", {"prompt_id": "expired", "status": "error"})
    _record(idempotency / "key.json", {"prompt_id": "kept", "status": "completed"})
    _record(assets / "asset_old.json", {"asset_id": "asset_old"})
    service = FileRetentionService(tmp_path)

    first = service.prune(run_days=7, asset_days=7, max_history_records=100)

    assert first == {"runs_deleted": 1, "assets_deleted": 0}
    assert (prompts / "active.json").exists()
    assert (prompts / "kept.json").exists()
    assert (assets / "asset_old.json").exists()

    (prompts / "active.json").unlink()
    (idempotency / "key.json").unlink()
    second = service.prune(run_days=7, asset_days=7, max_history_records=100)

    assert second == {"runs_deleted": 1, "assets_deleted": 1}
    assert not (prompts / "kept.json").exists()
    assert not (assets / "asset_old.json").exists()


def test_run_get_remains_available_while_retention_lock_is_held(tmp_path: Path) -> None:
    repository = FileRunRepository(tmp_path)
    repository.save(Job("prompt-old", "server", "workflow", "completed"))
    lock = FileLock(str(tmp_path / "data" / ".retention.lock"), timeout=10)

    with lock, ThreadPoolExecutor(max_workers=1) as executor:
        get_future = executor.submit(repository.get, "server", "prompt-old")
        assert get_future.result(timeout=0.5) is not None


def test_prune_rechecks_active_job_saved_while_waiting_for_coordination_lock(
    tmp_path: Path,
) -> None:
    repository = FileRunRepository(tmp_path)
    service = FileRetentionService(tmp_path)
    asset_path = tmp_path / "data" / "assets" / "asset_old.json"
    _record(asset_path, {"asset_id": "asset_old"})
    write_started = Event()
    release_write = Event()
    original_write = repository._atomic_write

    def blocking_write(path: Path, data: dict[str, object]) -> None:
        write_started.set()
        assert release_write.wait(timeout=2)
        original_write(path, data)

    repository._atomic_write = blocking_write  # type: ignore[method-assign]

    with ThreadPoolExecutor(max_workers=2) as executor:
        save_future = executor.submit(
            repository.save,
            Job("prompt-active", "server", "workflow", "running"),
        )
        assert write_started.wait(timeout=2)
        prune_future = executor.submit(
            service.prune,
            run_days=7,
            asset_days=7,
            max_history_records=100,
        )
        try:
            time.sleep(0.2)
            assert not prune_future.done()
        finally:
            release_write.set()

        save_future.result(timeout=2)
        assert prune_future.result(timeout=2) == {
            "runs_deleted": 0,
            "assets_deleted": 0,
        }
        assert repository.get("server", "prompt-active") is not None
        assert asset_path.exists()


def test_prune_waits_for_asset_get_and_save_without_deadlock(tmp_path: Path) -> None:
    repository = FileAssetRepository(tmp_path)
    old_asset = Asset(
        "asset_old",
        "server",
        "old.png",
        "old.png",
        "",
        "image",
        "image/png",
        1,
        "old-sha",
    )
    new_asset = Asset(
        "asset_new",
        "server",
        "new.png",
        "new.png",
        "",
        "image",
        "image/png",
        1,
        "new-sha",
    )
    repository.save(old_asset)
    old_path = tmp_path / "data" / "assets" / "asset_old.json"
    old = time.time() - 30 * 86_400
    os.utime(old_path, (old, old))
    service = FileRetentionService(tmp_path)
    read_started = Event()
    release_read = Event()
    prune_started = Event()
    save_started = Event()
    original_path = repository._path

    def blocking_path(asset_id: str) -> Path:
        if asset_id == "asset_old":
            read_started.set()
            assert release_read.wait(timeout=2)
        return original_path(asset_id)

    repository._path = blocking_path  # type: ignore[method-assign]

    def prune() -> dict[str, int]:
        prune_started.set()
        return service.prune(
            run_days=7,
            asset_days=7,
            max_history_records=100,
        )

    def save() -> None:
        save_started.set()
        repository.save(new_asset)

    with ThreadPoolExecutor(max_workers=3) as executor:
        get_future = executor.submit(repository.get, "asset_old")
        assert read_started.wait(timeout=2)
        prune_future = executor.submit(prune)
        save_future = executor.submit(save)
        assert prune_started.wait(timeout=2)
        assert save_started.wait(timeout=2)
        try:
            time.sleep(0.2)
            assert not prune_future.done()
            assert not save_future.done()
        finally:
            release_read.set()

        assert get_future.result(timeout=2) == old_asset
        assert prune_future.result(timeout=2) == {
            "runs_deleted": 0,
            "assets_deleted": 0,
        }
        assert old_path.exists()
        save_future.result(timeout=2)
        assert repository.get("asset_new") == new_asset


def test_prune_coordinates_with_claim_without_deadlock(tmp_path: Path) -> None:
    repository = FileRunRepository(tmp_path)
    service = FileRetentionService(tmp_path)
    prune_read_started = Event()
    release_prune = Event()
    claim_started = Event()
    original_prompt_records = service._prompt_records

    def blocking_prompt_records() -> list[tuple[Path, dict[str, object]]]:
        prune_read_started.set()
        assert release_prune.wait(timeout=2)
        return original_prompt_records()

    def claim() -> str | None:
        claim_started.set()
        return repository.claim("server", "workflow", "request", {})

    service._prompt_records = blocking_prompt_records  # type: ignore[method-assign]
    with ThreadPoolExecutor(max_workers=2) as executor:
        prune_future = executor.submit(
            service.prune,
            run_days=7,
            asset_days=7,
            max_history_records=100,
        )
        assert prune_read_started.wait(timeout=2)
        claim_future = executor.submit(claim)
        assert claim_started.wait(timeout=2)
        try:
            assert not claim_future.done()
        finally:
            release_prune.set()

        assert prune_future.result(timeout=2) == {
            "runs_deleted": 0,
            "assets_deleted": 0,
        }
        lease_token = claim_future.result(timeout=2)
        assert lease_token
        claim_record = repository.get_claim("server", "request")
        assert claim_record is not None
        assert claim_record["lease_token"] == lease_token


def test_prune_scans_references_once_without_concurrent_changes(tmp_path: Path) -> None:
    prompts = tmp_path / "data" / "runs" / "server" / "prompts"
    for index in range(600):
        _record(
            prompts / f"prompt-{index}.json",
            {"prompt_id": f"prompt-{index}", "status": "completed"},
        )
    service = FileRetentionService(tmp_path)
    scans = 0
    original = service._referenced_prompt_ids

    def counted() -> set[str]:
        nonlocal scans
        scans += 1
        return original()

    service._referenced_prompt_ids = counted  # type: ignore[method-assign]

    result = service.prune(run_days=7, asset_days=7, max_history_records=0)

    assert result["runs_deleted"] == 600
    assert scans == 1


def test_prune_rescans_when_reference_generation_changes(tmp_path: Path) -> None:
    prompt = tmp_path / "data" / "runs" / "server" / "prompts" / "kept.json"
    reference = tmp_path / "data" / "runs" / "server" / "idempotency" / "key.json"
    _record(prompt, {"prompt_id": "kept", "status": "completed"})
    service = FileRetentionService(tmp_path)
    generation_reads = 0

    def changing_generation() -> str:
        nonlocal generation_reads
        generation_reads += 1
        if generation_reads == 2:
            _record(reference, {"prompt_id": "kept", "status": "completed"}, age_days=0)
        return str(generation_reads)

    service._generation = changing_generation  # type: ignore[method-assign]

    result = service.prune(run_days=7, asset_days=7, max_history_records=0)

    assert result["runs_deleted"] == 0
    assert prompt.exists()


def test_asset_prune_uses_bounded_lock_batches(tmp_path: Path) -> None:
    assets = tmp_path / "data" / "assets"
    for index in range(600):
        _record(assets / f"asset-{index}.json", {"asset_id": f"asset-{index}"})
    service = FileRetentionService(tmp_path)
    lock_entries = 0
    real_lock = service._lock

    class CountingLock:
        def __enter__(self):
            nonlocal lock_entries
            lock_entries += 1
            return real_lock.__enter__()

        def __exit__(self, exc_type, exc_value, traceback):
            return real_lock.__exit__(exc_type, exc_value, traceback)

    service._lock = CountingLock()  # type: ignore[assignment]

    result = service.prune(run_days=7, asset_days=7, max_history_records=100)

    assert result["assets_deleted"] == 600
    assert lock_entries >= 4
