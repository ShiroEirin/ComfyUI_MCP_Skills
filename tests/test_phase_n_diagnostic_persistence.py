"""Phase N owner-bound SQLite diagnostic and retry persistence contracts."""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from comfyui_mcp_skills.infrastructure.persistence import control_plane as control_plane_module
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.sqlite_diagnostics import (
    SQLiteDiagnosticRetryRepository,
)


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _store(tmp_path: Path) -> SQLiteControlPlaneStore:
    store = SQLiteControlPlaneStore((tmp_path / "control-plane.sqlite3").resolve())
    store.initialize()
    return store


def _job(
    store: SQLiteControlPlaneStore,
    suffix: str = "1",
    *,
    owner: str = "owner-a",
    retry_of: str | None = None,
    arguments: dict[str, object] | None = None,
) -> dict[str, str]:
    arguments = arguments or {"seed": 1, "steps": 20}
    tag = suffix * 64
    workflow_id = f"workflow-{suffix}"
    revision_id = "revision_" + tag
    deployment_id = "deployment_" + tag
    plan_id = "plan_" + tag
    job_id = "job_" + tag
    created_at = "2026-08-03T00:00:00+00:00"
    snapshot = {"arguments": arguments, "resolved_inputs": arguments}
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT OR IGNORE INTO workflows(workflow_id,created_at) VALUES(?,?)",
            (workflow_id, created_at),
        )
        connection.execute(
            """INSERT OR IGNORE INTO workflow_revisions(
                   revision_id,workflow_id,graph_json,parameter_schema_json,
                   dependency_contract_json,content_digest,created_at
               ) VALUES(?,?, '{}','{}','{}',?,?)""",
            (revision_id, workflow_id, tag, created_at),
        )
        connection.execute(
            """INSERT OR IGNORE INTO workflow_deployments(
                   deployment_id,workflow_id,revision_id,server_id,enabled,
                   validation_status,published,created_at
               ) VALUES(?,?,?,'local',1,'valid',1,?)""",
            (deployment_id, workflow_id, revision_id, created_at),
        )
        connection.execute(
            """INSERT OR IGNORE INTO execution_plans(
                   plan_id,workflow_id,revision_id,deployment_id,server_id,
                   resolved_inputs_json,input_digest,plan_digest,created_at,raw_arguments_digest
               ) VALUES(?,?,?,?,'local',?,?,?,?,?)""",
            (
                plan_id,
                workflow_id,
                revision_id,
                deployment_id,
                _canonical(snapshot),
                _digest(snapshot),
                tag,
                created_at,
                _digest(snapshot),
            ),
        )
        connection.execute(
            """INSERT INTO jobs(
                   job_id,workflow_id,plan_id,revision_id,deployment_id,owner_id,
                   server_id,status,error,outputs_json,retry_of,created_at,
                   created_at_source,legacy_migrated,execution_origin
               ) VALUES(?,?,?,?,?,?,'local','error','out of memory','[]',?,?,'runtime',0,'planned')""",
            (job_id, workflow_id, plan_id, revision_id, deployment_id, owner, retry_of, created_at),
        )
    return {
        "job_id": job_id,
        "workflow_id": workflow_id,
        "revision_id": revision_id,
        "deployment_id": deployment_id,
        "plan_id": plan_id,
        "content_digest": tag,
        "owner_id": owner,
    }


def _report(job: dict[str, str]) -> dict[str, object]:
    diagnostic_id = "diagnostic_" + "a" * 64
    return {
        "diagnostic_id": diagnostic_id,
        "owner_id": job["owner_id"],
        "registry_version": "diagnostic-rules-v1",
        "subject_uri": f"comfyui://jobs/{job['job_id']}",
        "classification": "OUT_OF_MEMORY",
        "rule_id": "out-of-memory",
        "retryable": True,
        "evidence": {
            "status": "error",
            "failed_node": {},
            "events": [],
            "log_window": ["out of memory"],
        },
        "safe_actions": [
            {
                "tool": "comfyui.job.retry.plan",
                "name": "retry",
                "required_arguments": {"job_id": job["job_id"]},
                "risk": "safe",
            }
        ],
        "approval_actions": [],
        "created_at": "2026-08-03T00:01:00+00:00",
        "resource_uri": f"comfyui://diagnostics/{diagnostic_id}",
    }


def _repair_plan(
    job: dict[str, str], *, expires_at: str = "2026-08-03T01:00:00+00:00"
) -> dict[str, object]:
    original = {"seed": 1, "steps": 20}
    changes = {"steps": 16}
    resulting = {"seed": 1, "steps": 16}
    diff = [{"path": "/arguments/steps", "operation": "replace", "before": 20, "after": 16}]
    immutable = {
        "owner_id": job["owner_id"],
        "original_job_id": job["job_id"],
        "workflow_id": job["workflow_id"],
        "server_id": "local",
        "pinned_plan_id": job["plan_id"],
        "pinned_revision_id": job["revision_id"],
        "pinned_deployment_id": job["deployment_id"],
        "pinned_content_digest": job["content_digest"],
        "original_arguments_snapshot": original,
        "original_arguments_digest": _digest(original),
        "normalized_changes": changes,
        "resulting_arguments": resulting,
        "resulting_arguments_digest": _digest(resulting),
        "diff": diff,
        "created_at": "2026-08-03T00:00:00+00:00",
        "expires_at": expires_at,
    }
    plan_digest = _digest(immutable)
    repair_plan_id = "repair_plan_" + plan_digest
    return {
        "repair_plan_id": repair_plan_id,
        "plan_digest": plan_digest,
        "resource_uri": f"comfyui://plans/{repair_plan_id}",
        "status": "planned",
        **immutable,
    }


def test_phase_n_migration_upgrades_populated_v7_without_mutating_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteControlPlaneStore((tmp_path / "control-plane.sqlite3").resolve())
    migrations = control_plane_module._MIGRATIONS
    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", migrations[:7])
    store.initialize()
    original = _job(store)

    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", migrations)
    store.initialize()

    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone() == (13,)
        assert connection.execute("SELECT job_id FROM jobs").fetchone() == (original["job_id"],)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
    assert {
        "diagnostic_rule_versions",
        "diagnostic_reports",
        "repair_plans",
        "repair_plan_commits",
        "repair_plan_commit_intents",
    } <= tables


def test_diagnostic_reports_are_owner_bound_bounded_immutable_and_idempotent(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    job = _job(store)
    repository = SQLiteDiagnosticRetryRepository(store)
    report = _report(job)

    assert repository.save_diagnostic(report) == report
    assert repository.save_diagnostic(report) == report
    assert repository.get_diagnostic(str(report["diagnostic_id"]), "owner-b") is None
    assert repository.get_diagnostic(str(report["diagnostic_id"]), job["owner_id"]) == report

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE diagnostic_reports SET classification='OTHER'")
        forged = dict(report)
        forged["diagnostic_id"] = "diagnostic_" + "b" * 64
        forged["resource_uri"] = f"comfyui://diagnostics/{forged['diagnostic_id']}"
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO diagnostic_reports(
                       diagnostic_id,owner_id,registry_version,subject_uri,subject_kind,
                       job_id,server_id,classification,rule_id,retryable,evidence_json,
                       safe_actions_json,approval_actions_json,created_at,resource_uri
                   ) VALUES(?,?,?,?, 'job',?,NULL,?,?,1,?,?,?, ?,?)""",
                (
                    forged["diagnostic_id"],
                    "owner-b",
                    forged["registry_version"],
                    forged["subject_uri"],
                    job["job_id"],
                    forged["classification"],
                    forged["rule_id"],
                    _canonical(forged["evidence"]),
                    _canonical(forged["safe_actions"]),
                    _canonical(forged["approval_actions"]),
                    forged["created_at"],
                    forged["resource_uri"],
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE diagnostic_reports SET evidence_json=?",
                (
                    _canonical(
                        {
                            "status": "error",
                            "failed_node": {},
                            "events": [],
                            "log_window": ["x" * 70_000],
                        }
                    ),
                ),
            )


def test_sqlite_context_classifies_path_qualified_model_before_redaction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    job = _job(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE jobs SET error=? WHERE job_id=?",
            ("FileNotFoundError: C:\\models\\portrait.ckpt", job["job_id"]),
        )
    repository = SQLiteDiagnosticRetryRepository(store)

    from comfyui_mcp_skills.application.diagnostics import DiagnosticService

    report = DiagnosticService(repository).diagnose_job(job["job_id"], job["owner_id"])

    assert report["classification"] == "missing_model"
    assert "C:\\models" not in repr(report["evidence"])


def test_repair_plan_rejects_cross_owner_mutation_and_inexact_diff(tmp_path: Path) -> None:
    store = _store(tmp_path)
    job = _job(store)
    repository = SQLiteDiagnosticRetryRepository(store)
    plan = _repair_plan(job)

    assert repository.save_repair_plan(plan) == plan
    assert repository.get_repair_plan(str(plan["repair_plan_id"]), "owner-b") is None
    assert repository.save_repair_plan(plan) == plan

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE repair_plans SET resulting_arguments_json='{}'")
        columns = connection.execute("PRAGMA table_info(repair_plans)").fetchall()
        stored = connection.execute("SELECT * FROM repair_plans").fetchone()
        values = dict(zip((row[1] for row in columns), stored, strict=True))
        values["repair_plan_id"] = "repair_plan_" + "b" * 64
        values["plan_digest"] = "b" * 64
        values["diff_json"] = "[]"
        with pytest.raises(sqlite3.IntegrityError, match="diff"):
            connection.execute(
                f"INSERT INTO repair_plans({','.join(values)}) VALUES({','.join('?' for _ in values)})",
                tuple(values.values()),
            )


def test_commit_is_expiry_checked_aggregate_bound_and_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = _job(store)
    repository = SQLiteDiagnosticRetryRepository(store)
    plan = repository.save_repair_plan(_repair_plan(original))
    repository.reserve_repair_plan_commit(
        str(plan["repair_plan_id"]),
        str(plan["plan_digest"]),
        original["owner_id"],
        now=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )
    retry = _job(store, "2", owner=original["owner_id"], arguments={"seed": 1, "steps": 16})

    with pytest.raises(ValueError, match="binding"):
        repository.mark_repair_plan_committed(
            str(plan["repair_plan_id"]),
            str(plan["plan_digest"]),
            original["owner_id"],
            retry["job_id"],
            now=datetime(2026, 8, 3, tzinfo=timezone.utc),
        )

    retry_id = "job_" + "3" * 64
    retry_plan_id = "plan_" + "3" * 64
    resulting_snapshot = {
        "arguments": plan["resulting_arguments"],
        "resolved_inputs": plan["resulting_arguments"],
    }
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """INSERT INTO execution_plans(
                   plan_id,workflow_id,revision_id,deployment_id,server_id,
                   resolved_inputs_json,input_digest,plan_digest,created_at,raw_arguments_digest
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                retry_plan_id,
                original["workflow_id"],
                original["revision_id"],
                original["deployment_id"],
                "local",
                _canonical(resulting_snapshot),
                _digest(resulting_snapshot),
                "3" * 64,
                "2026-08-03T00:03:00+00:00",
                _digest(resulting_snapshot),
            ),
        )
        connection.execute(
            """INSERT INTO jobs(job_id,workflow_id,plan_id,revision_id,deployment_id,
                   owner_id,server_id,status,error,outputs_json,retry_of,created_at,
                   created_at_source,legacy_migrated,execution_origin)
               VALUES(?,?,?,?,?,?,'local','reserved','','[]',?,?,'runtime',0,'planned')""",
            (
                retry_id,
                original["workflow_id"],
                retry_plan_id,
                original["revision_id"],
                original["deployment_id"],
                original["owner_id"],
                original["job_id"],
                "2026-08-03T00:03:00+00:00",
            ),
        )
    committed = repository.mark_repair_plan_committed(
        str(plan["repair_plan_id"]),
        str(plan["plan_digest"]),
        original["owner_id"],
        retry_id,
        now=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )
    assert (
        repository.mark_repair_plan_committed(
            str(plan["repair_plan_id"]),
            str(plan["plan_digest"]),
            original["owner_id"],
            retry_id,
            now=datetime(2026, 8, 5, tzinfo=timezone.utc),
        )
        == committed
    )
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT retry_of FROM jobs WHERE job_id=?", (retry_id,)
        ).fetchone() == (original["job_id"],)
        assert connection.execute("SELECT count(*) FROM repair_plan_commits").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM repair_plan_commit_intents").fetchone() == (
            1,
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM repair_plan_commits")


def test_direct_sql_rejects_fractionally_inexact_hour_lifetime(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = _job(store)
    repository = SQLiteDiagnosticRetryRepository(store)
    plan = repository.save_repair_plan(_repair_plan(original))
    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO repair_plans
                   SELECT ?,?,owner_id,?,original_job_id,workflow_id,server_id,pinned_plan_id,
                          pinned_revision_id,pinned_deployment_id,pinned_content_digest,
                          original_arguments_json,original_arguments_digest,normalized_changes_json,
                          resulting_arguments_json,resulting_arguments_digest,diff_json,status,?,?
                   FROM repair_plans WHERE repair_plan_id=?""",
                (
                    "repair_plan_" + "9" * 64,
                    "9" * 64,
                    "comfyui://plans/repair_plan_" + "9" * 64,
                    "2026-08-03T00:00:00.900000+00:00",
                    "2026-08-03T01:00:00.100000+00:00",
                    plan["repair_plan_id"],
                ),
            )


def test_expired_cleanup_retains_durable_commit_intents(tmp_path: Path) -> None:
    store = _store(tmp_path)
    repository = SQLiteDiagnosticRetryRepository(store)
    reserved_job = _job(store, "1")
    removable_job = _job(store, "2")
    reserved = repository.save_repair_plan(_repair_plan(reserved_job))
    removable = repository.save_repair_plan(_repair_plan(removable_job))
    repository.reserve_repair_plan_commit(
        str(reserved["repair_plan_id"]),
        str(reserved["plan_digest"]),
        reserved_job["owner_id"],
        now=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )

    removed = repository.cleanup_expired_repair_plans(now=datetime(2026, 8, 4, tzinfo=timezone.utc))

    assert removed == 1
    assert repository.get_repair_plan(str(reserved["repair_plan_id"]), reserved_job["owner_id"])
    assert (
        repository.get_repair_plan(str(removable["repair_plan_id"]), removable_job["owner_id"])
        is None
    )


def test_expired_and_non_hour_plan_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = _job(store)
    repository = SQLiteDiagnosticRetryRepository(store)
    plan = repository.save_repair_plan(_repair_plan(original))
    with pytest.raises(ValueError, match="expired"):
        repository.reserve_repair_plan_commit(
            str(plan["repair_plan_id"]),
            str(plan["plan_digest"]),
            original["owner_id"],
            now=datetime(2026, 8, 4, tzinfo=timezone.utc),
        )
    with pytest.raises(ValueError, match="exactly one hour"):
        repository.save_repair_plan(_repair_plan(original, expires_at="2026-08-03T02:00:00+00:00"))

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="cycle|append-once"):
            connection.execute(
                "UPDATE jobs SET retry_of=job_id WHERE job_id=?", (original["job_id"],)
            )
