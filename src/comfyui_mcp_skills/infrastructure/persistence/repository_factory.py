"""Select production repositories from atomic aggregate cutover evidence."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from comfyui_mcp_skills.application.ports import AssetRepository, RunRepository, WorkflowRepository
from comfyui_mcp_skills.infrastructure.persistence.assets import FileAssetRepository
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.migration_lock import (
    project_migration_lock,
)
from comfyui_mcp_skills.infrastructure.persistence.runs import FileRunRepository
from comfyui_mcp_skills.infrastructure.persistence.sqlite_assets import SQLiteAssetRepository
from comfyui_mcp_skills.infrastructure.persistence.sqlite_diagnostics import (
    SQLiteDiagnosticRetryRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_experiments import (
    SQLiteExperimentRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_runs import SQLiteRunRepository
from comfyui_mcp_skills.infrastructure.persistence.sqlite_workflows import SQLiteWorkflowRepository
from comfyui_mcp_skills.infrastructure.persistence.workflows import FileWorkflowRepository

StoreBackend = Literal["file", "sqlite"]

_WORKFLOW_AGGREGATES = frozenset({"workflow", "revision", "deployment"})
_JOB_AGGREGATES = frozenset({"job", "execution_attempt", "idempotency_record", "artifact"})
_ASSET_AGGREGATES = frozenset({"asset"})
_ROUTED_AGGREGATES = _WORKFLOW_AGGREGATES | _JOB_AGGREGATES | _ASSET_AGGREGATES


class StoreRoutingError(RuntimeError):
    """The durable cutover evidence cannot produce a safe repository choice."""


@dataclass(frozen=True, slots=True)
class RepositoryBundle:
    """Repositories and the immutable backend choice made for this process."""

    workflows: WorkflowRepository
    runs: RunRepository
    assets: AssetRepository
    workflow_store: StoreBackend
    run_store: StoreBackend
    asset_store: StoreBackend
    store: SQLiteControlPlaneStore | None
    experiments: SQLiteExperimentRepository | None = None
    diagnostics: SQLiteDiagnosticRetryRepository | None = None
    retries: SQLiteDiagnosticRetryRepository | None = None


@dataclass(frozen=True, slots=True)
class _MigrationState:
    aggregate_kind: str
    version: int
    status: str
    checksum: str
    switched_at: str | None


def create_repository_bundle(base_dir: Path) -> RepositoryBundle:
    """Route Run and Asset ports without ever falling back after cutover evidence."""
    project_root = base_dir.resolve()
    with project_migration_lock(project_root):
        return _create_repository_bundle_locked(project_root)


def _create_repository_bundle_locked(project_root: Path) -> RepositoryBundle:
    database_path = project_root / "data" / "control-plane.sqlite3"
    if not database_path.exists():
        return RepositoryBundle(
            workflows=FileWorkflowRepository(project_root),
            runs=FileRunRepository(project_root),
            assets=FileAssetRepository(project_root),
            workflow_store="file",
            run_store="file",
            asset_store="file",
            store=None,
            experiments=None,
            diagnostics=None,
            retries=None,
        )

    store = SQLiteControlPlaneStore(database_path)
    try:
        store.initialize()
        states = _read_migration_states(store.path)
        workflow_store = _backend_for_group("workflow", _WORKFLOW_AGGREGATES, states)
        run_store = _backend_for_group("job", _JOB_AGGREGATES, states)
        asset_store = _backend_for_group("asset", _ASSET_AGGREGATES, states)
    except StoreRoutingError:
        raise
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        raise StoreRoutingError(f"cannot establish repository routing: {exc}") from exc

    workflows: WorkflowRepository
    runs: RunRepository
    assets: AssetRepository
    if workflow_store == "sqlite":
        workflows = SQLiteWorkflowRepository(store)
    else:
        workflows = FileWorkflowRepository(project_root)
    if run_store == "sqlite":
        runs = SQLiteRunRepository(store)
    else:
        runs = FileRunRepository(project_root)
    if asset_store == "sqlite":
        assets = SQLiteAssetRepository(store)
    else:
        assets = FileAssetRepository(project_root)
    experiments = SQLiteExperimentRepository(store)
    diagnostics = (
        SQLiteDiagnosticRetryRepository(store)
        if run_store == "sqlite" and workflow_store == "sqlite"
        else None
    )
    return RepositoryBundle(
        workflows=workflows,
        runs=runs,
        assets=assets,
        workflow_store=workflow_store,
        run_store=run_store,
        asset_store=asset_store,
        store=store,
        experiments=experiments,
        diagnostics=diagnostics,
        retries=diagnostics,
    )


def _read_migration_states(database_path: Path) -> tuple[_MigrationState, ...]:
    connection = sqlite3.connect(database_path, isolation_level=None, timeout=5.0)
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("BEGIN")
        rows = connection.execute(
            """
            SELECT aggregate_kind, version, status, checksum, switched_at
            FROM store_migrations
            WHERE aggregate_kind IN (?, ?, ?, ?, ?, ?, ?, ?)
            ORDER BY aggregate_kind, version
            """,
            tuple(sorted(_ROUTED_AGGREGATES)),
        ).fetchall()
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return tuple(_MigrationState(*row) for row in rows)


def _backend_for_group(
    label: str,
    required: frozenset[str],
    states: tuple[_MigrationState, ...],
) -> StoreBackend:
    group = tuple(state for state in states if state.aggregate_kind in required)
    active = tuple(state for state in group if state.status == "switched")
    active_kinds = {state.aggregate_kind for state in active}
    if active and active_kinds != required:
        missing = sorted(required - active_kinds)
        raise StoreRoutingError(f"partial {label} store switch; missing switched rows: {missing}")
    if active:
        versions = {state.version for state in active}
        checksums = {state.checksum for state in active}
        if len(versions) != 1 or len(checksums) != 1:
            raise StoreRoutingError(f"conflicting {label} store switch versions or checksums")
        _validate_cutover_history(label, required, group)
        return "sqlite"

    if any(state.switched_at is not None for state in group):
        raise StoreRoutingError(
            f"{label} cutover evidence exists without a complete active switch; "
            "refusing File fallback"
        )
    return "file"


def _validate_cutover_history(
    label: str,
    required: frozenset[str],
    states: tuple[_MigrationState, ...],
) -> None:
    evidence = tuple(state for state in states if state.switched_at is not None)
    cohorts: dict[tuple[int, str], list[_MigrationState]] = {}
    for state in evidence:
        cohorts.setdefault((state.version, state.checksum), []).append(state)
    for (version, checksum), cohort in cohorts.items():
        kinds = {state.aggregate_kind for state in cohort}
        if kinds != required:
            raise StoreRoutingError(
                f"partial {label} cutover evidence for version {version} checksum {checksum}"
            )
        statuses = {state.status for state in cohort}
        if len(statuses) != 1:
            raise StoreRoutingError(
                f"conflicting {label} cutover evidence for version {version} checksum {checksum}"
            )
