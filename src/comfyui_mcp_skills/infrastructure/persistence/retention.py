"""Conservative retention for MCP run and asset metadata."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock

from comfyui_mcp_skills.infrastructure.persistence.migration_lock import (
    project_migration_lock,
)
from comfyui_mcp_skills.infrastructure.persistence.repository_factory import (
    create_repository_bundle,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_asset_library import (
    SQLiteAssetLibraryRepository,
)

_TERMINAL = {"completed", "error", "interrupted", "cancelled"}


class SQLiteRetentionService:
    """Apply due SQLite media lifecycle work without deleting logical identities."""

    def __init__(self, repository: SQLiteAssetLibraryRepository) -> None:
        self._repository = repository

    def prune(self, *, now: datetime | None = None) -> dict[str, int]:
        return self._repository.apply_retention(now=now or datetime.now(timezone.utc))


class FileRetentionService:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir.resolve()
        self._data_root = (base_dir.resolve() / "data").resolve()
        self._data_root.mkdir(parents=True, exist_ok=True)
        self._runs_root = self._data_root / "runs"
        self._assets_root = self._data_root / "assets"
        self._generation_path = self._data_root / ".retention-generation"
        self._lock = FileLock(str(self._data_root / ".retention.lock"), timeout=10)
        self._migration_lock = project_migration_lock(base_dir)

    def prune(
        self,
        *,
        run_days: int,
        asset_days: int,
        max_history_records: int,
        preserve_runs: bool = False,
        preserve_assets: bool = False,
    ) -> dict[str, int]:
        with self._migration_lock:
            return self._prune_locked(
                run_days=run_days,
                asset_days=asset_days,
                max_history_records=max_history_records,
                preserve_runs=preserve_runs,
                preserve_assets=preserve_assets,
            )

    def prune_switch_aware(
        self,
        *,
        run_days: int,
        asset_days: int,
        max_history_records: int,
    ) -> dict[str, int]:
        """Read cutover state and prune legacy files under the same migration lock."""
        with self._migration_lock:
            repositories = create_repository_bundle(self._base_dir)
            file_result = self._prune_locked(
                run_days=run_days,
                asset_days=asset_days,
                max_history_records=max_history_records,
                preserve_runs=repositories.run_store == "sqlite",
                preserve_assets=repositories.asset_store == "sqlite",
            )
            if repositories.store is not None and (
                repositories.run_store == "sqlite" or repositories.asset_store == "sqlite"
            ):
                sqlite_result = SQLiteRetentionService(
                    SQLiteAssetLibraryRepository(repositories.store)
                ).prune()
                file_result["assets_deleted"] += sqlite_result["assets_tombstoned"]
            return file_result

    def _prune_locked(
        self,
        *,
        run_days: int,
        asset_days: int,
        max_history_records: int,
        preserve_runs: bool,
        preserve_assets: bool,
    ) -> dict[str, int]:
        if run_days < 0 or asset_days < 0 or max_history_records < 0:
            raise ValueError("Retention values must be non-negative")
        now = time.time()
        prompt_records = self._prompt_records()
        with self._lock:
            referenced = self._referenced_prompt_ids()
            reference_generation = self._generation()
        deletable = [
            (path, record)
            for path, record in prompt_records
            if str(record.get("status", "")) in _TERMINAL
            and str(record.get("prompt_id", "")) not in referenced
        ]
        deletable.sort(key=lambda item: self._mtime(item[0]), reverse=True)
        over_limit = {path for path, _record in deletable[max_history_records:]}
        run_cutoff = now - run_days * 86_400
        candidates = (
            []
            if preserve_runs
            else [
                path
                for path, _record in deletable
                if path in over_limit or self._mtime(path) < run_cutoff
            ]
        )

        runs_deleted = 0
        batch_size = 256
        for offset in range(0, len(candidates), batch_size):
            with self._lock:
                current_generation = self._generation()
                if current_generation != reference_generation:
                    referenced = self._referenced_prompt_ids()
                    reference_generation = current_generation
                for path in candidates[offset : offset + batch_size]:
                    current = self._read(path)
                    if (
                        current is None
                        or str(current.get("status", "")) not in _TERMINAL
                        or str(current.get("prompt_id", "")) in referenced
                    ):
                        continue
                    path.unlink(missing_ok=True)
                    runs_deleted += 1

        assets_deleted = 0
        asset_cutoff = now - asset_days * 86_400
        asset_candidates = []
        if not preserve_assets and self._assets_root.exists():
            asset_candidates = [
                path
                for path in self._assets_root.glob("*.json")
                if self._mtime(path) < asset_cutoff
            ]
        for offset in range(0, len(asset_candidates), batch_size):
            with self._lock:
                active = any(
                    str(record.get("status", "")) not in _TERMINAL
                    for _path, record in self._prompt_records()
                )
                if active:
                    break
                for path in asset_candidates[offset : offset + batch_size]:
                    try:
                        if path.stat().st_mtime >= asset_cutoff:
                            continue
                        path.unlink(missing_ok=True)
                        assets_deleted += 1
                    except FileNotFoundError:
                        continue
        return {
            "runs_deleted": runs_deleted,
            "assets_deleted": assets_deleted,
        }

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except FileNotFoundError:
            return float("inf")

    def _generation(self) -> str:
        try:
            return self._generation_path.read_text(encoding="ascii")
        except (FileNotFoundError, OSError):
            return ""

    def _prompt_records(self) -> list[tuple[Path, dict[str, Any]]]:
        if not self._runs_root.exists():
            return []
        result: list[tuple[Path, dict[str, Any]]] = []
        for path in self._runs_root.glob("*/prompts/*.json"):
            record = self._read(path)
            if record is not None and record.get("prompt_id"):
                result.append((path, record))
        return result

    def _referenced_prompt_ids(self) -> set[str]:
        if not self._runs_root.exists():
            return set()
        result: set[str] = set()
        for path in self._runs_root.glob("*/idempotency/*.json"):
            record = self._read(path)
            prompt_id = (record or {}).get("prompt_id")
            if isinstance(prompt_id, str) and prompt_id:
                result.add(prompt_id)
        return result

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None
