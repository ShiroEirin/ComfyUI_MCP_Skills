"""SQLite control-plane schema and transaction contracts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import comfyui_mcp_skills.infrastructure.persistence.control_plane as control_plane_module
from comfyui_mcp_skills.domain.control_plane import derive_legacy_attempt_id
from comfyui_mcp_skills.infrastructure.persistence.control_plane import (
    SchemaMigration,
    SchemaMigrationError,
    SQLiteControlPlaneStore,
)


def _table_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }


def test_initialize_creates_versioned_control_plane_schema(tmp_path: Path) -> None:
    database = tmp_path / "control-plane.sqlite3"
    store = SQLiteControlPlaneStore(database)

    store.initialize()

    assert _table_names(database) >= {
        "schema_migrations",
        "store_migrations",
        "workflows",
        "workflow_revisions",
        "workflow_deployments",
        "execution_plans",
        "jobs",
        "execution_attempts",
        "idempotency_records",
        "assets",
        "artifacts",
        "legacy_resource_aliases",
        "test_aggregates",
        "work_items",
        "domain_events",
        "outbox",
        "operation_work_items",
        "work_leases",
        "server_generation_observations",
        "workflow_change_plans",
        "workflow_rollback_requests",
    }
    with sqlite3.connect(database) as connection:
        applied = connection.execute(
            """
            SELECT version, name, length(checksum), up_supported,
                   down_supported, length(feasibility_note) > 0
            FROM schema_migrations
            """
        ).fetchall()
        switched_count = connection.execute(
            "SELECT count(*) FROM store_migrations WHERE status = 'switched'"
        ).fetchone()[0]
    assert applied == [
        (1, "initial-control-plane", 64, 1, 1, 1),
        (2, "g1-job-asset-facts", 64, 1, 0, 1),
        (3, "g5-event-orchestrator", 64, 1, 0, 1),
        (4, "g5-upstream-identity-merge", 64, 1, 0, 1),
        (5, "phase-j-workflow-change-plans", 64, 1, 0, 1),
        (6, "phase-l-asset-library", 64, 1, 0, 1),
        (7, "phase-m-experiments", 64, 1, 0, 1),
        (8, "phase-n-diagnostic-recovery", 64, 1, 0, 1),
    ]
    assert switched_count == 0


def test_initialize_is_idempotent_and_detects_checksum_drift(tmp_path: Path) -> None:
    database = tmp_path / "control-plane.sqlite3"
    store = SQLiteControlPlaneStore(database)
    store.initialize()
    store.initialize()

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone() == (8,)
        connection.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = 1", ("0" * 64,)
        )

    with pytest.raises(SchemaMigrationError, match="checksum mismatch"):
        store.initialize()
    assert "jobs" in _table_names(database)


def test_initialize_rejects_unknown_schema_migration(tmp_path: Path) -> None:
    database = tmp_path / "control-plane.sqlite3"
    store = SQLiteControlPlaneStore(database)
    store.initialize()
    with sqlite3.connect(database) as connection:
        existing = connection.execute(
            """
            SELECT name, checksum, up_supported, down_supported,
                   feasibility_note, schema_fingerprint, applied_at
            FROM schema_migrations WHERE version = 1
            """
        ).fetchone()
        connection.execute(
            """
            INSERT INTO schema_migrations(
                version, name, checksum, up_supported, down_supported,
                feasibility_note, schema_fingerprint, applied_at
            ) VALUES (999, 'future', ?, ?, ?, ?, ?, ?)
            """,
            existing[1:],
        )

    with pytest.raises(SchemaMigrationError, match="unknown schema migration"):
        store.initialize()


def test_g1_schema_is_forward_only(tmp_path: Path) -> None:
    database = tmp_path / "control-plane.sqlite3"
    store = SQLiteControlPlaneStore(database)
    store.initialize()

    with pytest.raises(SchemaMigrationError, match="cannot be rolled back"):
        store.rollback_schema(target_version=1)

    assert "jobs" in _table_names(database)


def test_g1_forward_migration_drops_legacy_plan_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "control-plane.sqlite3"
    store = SQLiteControlPlaneStore(database)
    migrations = control_plane_module._MIGRATIONS
    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", migrations[:1])
    store.initialize()
    created_at = "2026-07-30T00:00:00.000000Z"
    revision_id = "revision_" + "a" * 32
    deployment_id = "deployment_" + "b" * 32
    plan_id = "plan_" + "c" * 32
    job_id = "job_" + "d" * 32
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO workflows(workflow_id, created_at) VALUES ('portrait', ?)",
            (created_at,),
        )
        connection.execute(
            """
            INSERT INTO workflow_revisions(
                revision_id, workflow_id, graph_json, parameter_schema_json,
                dependency_contract_json, content_digest, created_at
            ) VALUES (?, 'portrait', '{}', '{}', '{}', ?, ?)
            """,
            (revision_id, "a" * 64, created_at),
        )
        connection.execute(
            """
            INSERT INTO workflow_deployments(
                deployment_id, workflow_id, revision_id, server_id, enabled,
                validation_status, published, created_at
            ) VALUES (?, 'portrait', ?, 'local', 1, 'valid', 1, ?)
            """,
            (deployment_id, revision_id, created_at),
        )
        connection.execute(
            """
            INSERT INTO execution_plans(
                plan_id, workflow_id, revision_id, deployment_id, server_id,
                resolved_inputs_json, input_digest, plan_digest, created_at
            ) VALUES (?, 'portrait', ?, ?, 'local', '{}', ?, ?, ?)
            """,
            (plan_id, revision_id, deployment_id, "b" * 64, "c" * 64, created_at),
        )
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, workflow_id, plan_id, revision_id, deployment_id,
                owner_id, status, created_at, created_at_source, legacy_migrated
            ) VALUES (?, 'portrait', ?, ?, ?, 'owner', 'completed', ?, 'runtime', 1)
            """,
            (job_id, plan_id, revision_id, deployment_id, created_at),
        )

    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", migrations)
    store.initialize()

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT plan_id, revision_id, deployment_id, execution_origin FROM jobs "
            "WHERE job_id = ?",
            (job_id,),
        ).fetchone() == (None, None, None, "legacy_migrated")


def test_schema_migration_checksum_covers_down_contract() -> None:
    first = SchemaMigration(
        1,
        "example",
        ("CREATE TABLE example(id INTEGER)",),
        ("DROP TABLE example",),
    )
    changed_down = SchemaMigration(1, "example", first.up, ("DROP TABLE IF EXISTS example",))
    changed_bootstrap = SchemaMigration(
        1,
        "example",
        first.up,
        first.down,
        bootstrap_sql="CREATE TABLE schema_migrations(version INTEGER)",
    )

    assert first.checksum != changed_down.checksum
    assert first.checksum != changed_bootstrap.checksum


def test_schema_rejects_null_ids_non_hex_digests_and_exposes_job_indexes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control-plane.sqlite3"
    store = SQLiteControlPlaneStore(database)
    store.initialize()

    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO workflows(workflow_id, created_at) VALUES (NULL, ?)",
                ("2026-07-30T00:00:00+00:00",),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE version = 1",
                ("z" * 64,),
            )
        indexes = {
            str(row[1]): tuple(
                column[2] for column in connection.execute(f"PRAGMA index_info('{row[1]}')")
            )
            for row in connection.execute("PRAGMA index_list('jobs')")
        }

    assert indexes["ix_jobs_owner_created"] == ("owner_id", "created_at", "job_id")
    assert indexes["ix_jobs_owner_status_created"] == (
        "owner_id",
        "status",
        "created_at",
        "job_id",
    )
    assert indexes["ix_jobs_owner_workflow_created"] == (
        "owner_id",
        "workflow_id",
        "created_at",
        "job_id",
    )


def test_schema_rejects_cross_workflow_revision_deployment_binding(tmp_path: Path) -> None:
    database = tmp_path / "control-plane.sqlite3"
    SQLiteControlPlaneStore(database).initialize()
    created_at = "2026-07-30T00:00:00+00:00"
    workflow_a = "workflow-a"
    workflow_b = "workflow-b"
    revision_b = "revision_" + "b" * 32

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executemany(
            "INSERT INTO workflows(workflow_id, created_at) VALUES (?, ?)",
            [(workflow_a, created_at), (workflow_b, created_at)],
        )
        connection.execute(
            """
            INSERT INTO workflow_revisions(
                revision_id, workflow_id, graph_json, parameter_schema_json,
                dependency_contract_json, content_digest, created_at
            ) VALUES (?, ?, '{}', '{}', '{}', ?, ?)
            """,
            (revision_b, workflow_b, "b" * 64, created_at),
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE workflow_revisions SET graph_json = '{\"changed\":true}' "
                "WHERE revision_id = ?",
                (revision_b,),
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO workflow_deployments(
                    deployment_id, workflow_id, revision_id, server_id,
                    enabled, validation_status, published, created_at
                ) VALUES (?, ?, ?, 'local', 1, 'valid', 1, ?)
                """,
                ("deployment_" + "d" * 32, workflow_a, revision_b, created_at),
            )


def test_schema_rollback_refuses_after_any_aggregate_switch(tmp_path: Path) -> None:
    database = tmp_path / "control-plane.sqlite3"
    store = SQLiteControlPlaneStore(database)
    store.initialize()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO store_migrations(
                aggregate_kind, version, status, checksum, switched_at
            ) VALUES ('job', 1, 'switched', ?, ?)
            """,
            ("a" * 64, "2026-07-30T00:00:00+00:00"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO store_migrations(
                    aggregate_kind, version, status, checksum, switched_at
                ) VALUES ('typo', 1, 'pending', ?, NULL)
                """,
                ("b" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO store_migrations(
                    aggregate_kind, version, status, checksum, switched_at
                ) VALUES ('job', 2, 'switched', ?, ?)
                """,
                ("c" * 64, "2026-07-30T00:00:00+00:00"),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE store_migrations SET switched_at = ? WHERE aggregate_kind = 'job'",
                ("2026-07-31T00:00:00+00:00",),
            )
        connection.execute(
            "UPDATE store_migrations SET status = 'superseded' WHERE aggregate_kind = 'job'"
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE store_migrations SET status = 'switched' WHERE aggregate_kind = 'job'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM store_migrations WHERE aggregate_kind = 'job'")
    with pytest.raises(SchemaMigrationError, match="switched"):
        store.rollback_schema(target_version=0)

    assert "jobs" in _table_names(database)


def test_job_plan_binding_is_all_or_none_and_matches_the_plan(tmp_path: Path) -> None:
    database = tmp_path / "control-plane.sqlite3"
    SQLiteControlPlaneStore(database).initialize()
    created_at = "2026-07-30T00:00:00.000000Z"
    revision_id = "revision_" + "a" * 32
    deployment_id = "deployment_" + "b" * 32
    plan_id = "plan_" + "c" * 32

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO workflows(workflow_id, created_at) VALUES ('portrait', ?)",
            (created_at,),
        )
        connection.execute(
            """
            INSERT INTO workflow_revisions(
                revision_id, workflow_id, graph_json, parameter_schema_json,
                dependency_contract_json, content_digest, created_at
            ) VALUES (?, 'portrait', '{}', '{}', '{}', ?, ?)
            """,
            (revision_id, "a" * 64, created_at),
        )
        connection.execute(
            """
            INSERT INTO workflow_deployments(
                deployment_id, workflow_id, revision_id, server_id,
                enabled, validation_status, published, created_at
            ) VALUES (?, 'portrait', ?, 'local', 1, 'valid', 1, ?)
            """,
            (deployment_id, revision_id, created_at),
        )
        connection.execute(
            """
            INSERT INTO execution_plans(
                plan_id, workflow_id, revision_id, deployment_id, server_id,
                resolved_inputs_json, input_digest, plan_digest, created_at
            ) VALUES (?, 'portrait', ?, ?, 'local', '{}', ?, ?, ?)
            """,
            (plan_id, revision_id, deployment_id, "b" * 64, "c" * 64, created_at),
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE execution_plans SET resolved_inputs_json = '{\"changed\":true}' "
                "WHERE plan_id = ?",
                (plan_id,),
            )

        pre_g4_job_id = "job_" + "0" * 32
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, workflow_id, owner_id, status, created_at,
                created_at_source, legacy_migrated, execution_origin
            ) VALUES (?, 'portrait', 'owner', 'queued', ?, 'runtime', 0,
                      'pre_g4_runtime')
            """,
            (pre_g4_job_id, created_at),
        )
        assert connection.execute(
            "SELECT plan_id, revision_id, deployment_id, execution_origin FROM jobs "
            "WHERE job_id = ?",
            (pre_g4_job_id,),
        ).fetchone() == (None, None, None, "pre_g4_runtime")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, workflow_id, plan_id, revision_id, deployment_id,
                    owner_id, status, created_at, created_at_source, legacy_migrated,
                    execution_origin
                ) VALUES (?, 'portrait', ?, NULL, ?, 'owner', 'queued', ?, 'runtime', 0,
                          'planned')
                """,
                ("job_" + "d" * 32, plan_id, deployment_id, created_at),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, workflow_id, plan_id, revision_id, deployment_id,
                    owner_id, status, created_at, created_at_source, legacy_migrated,
                    execution_origin
                ) VALUES (?, 'other', ?, ?, ?, 'owner', 'queued', ?, 'runtime', 0,
                          'planned')
                """,
                ("job_" + "e" * 32, plan_id, revision_id, deployment_id, created_at),
            )
        self_job_id = "job_" + "f" * 32
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, workflow_id, owner_id, status, retry_of, created_at,
                    created_at_source, legacy_migrated, execution_origin
                ) VALUES (?, 'portrait', 'owner', 'queued', ?, ?, 'legacy_file_mtime', 1,
                          'legacy_migrated')
                """,
                (self_job_id, self_job_id, created_at),
            )
        bound_job_id = "job_" + "1" * 32
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, workflow_id, plan_id, revision_id, deployment_id,
                owner_id, status, created_at, created_at_source, legacy_migrated,
                execution_origin
            ) VALUES (?, 'portrait', ?, ?, ?, 'owner', 'queued', ?, 'runtime', 0,
                      'planned')
            """,
            (bound_job_id, plan_id, revision_id, deployment_id, created_at),
        )
        with pytest.raises(sqlite3.IntegrityError, match="server"):
            connection.execute(
                """
                INSERT INTO execution_attempts(
                    attempt_id, job_id, attempt, server_id, client_id,
                    submission_state, created_at
                ) VALUES (?, ?, 1, 'other', 'client', 'submitted', ?)
                """,
                ("attempt_" + "2" * 32, bound_job_id, created_at),
            )
        attempt_id = "attempt_" + "3" * 32
        for attempt_id_value, state, prompt_id in (
            ("attempt_" + "5" * 32, "submitted", None),
            ("attempt_" + "6" * 32, "submission_unknown", "prompt-unknown"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO execution_attempts(
                        attempt_id, job_id, attempt, server_id, upstream_prompt_id,
                        client_id, submission_state, created_at
                    ) VALUES (?, ?, 3, 'local', ?, 'client', ?, ?)
                    """,
                    (attempt_id_value, bound_job_id, prompt_id, state, created_at),
                )
        connection.execute(
            """
            INSERT INTO execution_attempts(
                attempt_id, job_id, attempt, server_id, upstream_prompt_id,
                client_id, submission_state, created_at
            ) VALUES (?, ?, 1, 'local', 'prompt-submitted', 'client', 'submitted', ?)
            """,
            (attempt_id, bound_job_id, created_at),
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE execution_attempts SET client_id = 'changed' WHERE attempt_id = ?",
                (attempt_id,),
            )
        unknown_attempt_id = "attempt_" + "4" * 32
        connection.execute(
            """
            INSERT INTO execution_attempts(
                attempt_id, job_id, attempt, server_id, client_id,
                submission_state, created_at
            ) VALUES (?, ?, 2, 'local', 'client-unknown', 'submission_unknown', ?)
            """,
            (unknown_attempt_id, bound_job_id, created_at),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE execution_attempts
                SET upstream_prompt_id = '', submission_state = 'submitted'
                WHERE attempt_id = ?
                """,
                (unknown_attempt_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE execution_attempts
                SET upstream_prompt_id = 'prompt-1', submission_state = 'garbage'
                WHERE attempt_id = ?
                """,
                (unknown_attempt_id,),
            )
        connection.execute(
            """
            UPDATE execution_attempts
            SET upstream_prompt_id = 'prompt-1', submission_state = 'submitted'
            WHERE attempt_id = ?
            """,
            (unknown_attempt_id,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-once"):
            connection.execute(
                "UPDATE execution_attempts SET upstream_prompt_id = 'prompt-2' "
                "WHERE attempt_id = ?",
                (unknown_attempt_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM execution_attempts WHERE attempt_id = ?", (attempt_id,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE jobs SET owner_id = 'changed' WHERE job_id = ?", (bound_job_id,)
            )
        for assignment in (
            "job_id = 'job_22222222222222222222222222222222'",
            "created_at = '2026-07-31T00:00:00.000000Z'",
            "created_at_source = 'changed'",
            "legacy_migrated = 1",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(
                    f"UPDATE jobs SET {assignment} WHERE job_id = ?", (bound_job_id,)
                )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO idempotency_records(
                    owner_id, scope, key, request_digest, state, job_id,
                    client_id, claimed_at
                ) VALUES ('other-owner', 'execute', 'key', ?, 'resolved', ?, 'client', ?)
                """,
                ("a" * 64, bound_job_id, created_at),
            )


def test_artifact_uniqueness_matches_the_full_deterministic_tuple(tmp_path: Path) -> None:
    database = tmp_path / "control-plane.sqlite3"
    SQLiteControlPlaneStore(database).initialize()
    created_at = "2026-07-30T00:00:00.000000Z"
    job_id = "job_" + "a" * 32
    base = (job_id, "local", "9", "images", 0)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, workflow_id, owner_id, status, created_at,
                    created_at_source, legacy_migrated, execution_origin
                ) VALUES ('job_not-hex', 'portrait', 'owner', 'completed', ?,
                          'legacy_file_mtime', 1, 'legacy_migrated')
                """,
                (created_at,),
            )
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, workflow_id, owner_id, status, created_at,
                created_at_source, legacy_migrated, execution_origin
            ) VALUES (?, 'portrait', 'owner', 'completed', ?, 'legacy_file_mtime', 1,
                      'legacy_migrated')
            """,
            (job_id, created_at),
        )
        connection.execute(
            """
            INSERT INTO artifacts(
                artifact_id, job_id, server_id, upstream_node_id, output_key,
                upstream_output_index, filename, subfolder, storage_type,
                media_type, digest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'first.png', '', 'output', 'image', ?, ?)
            """,
            ("artifact_" + "b" * 32, *base, "b" * 64, created_at),
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE artifacts SET filename = 'changed.png' WHERE artifact_id = ?",
                ("artifact_" + "b" * 32,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM artifacts WHERE artifact_id = ?", ("artifact_" + "b" * 32,)
            )
        connection.execute(
            """
            INSERT INTO artifacts(
                artifact_id, job_id, server_id, upstream_node_id, output_key,
                upstream_output_index, filename, subfolder, storage_type,
                media_type, digest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'second.png', '', 'output', 'image', ?, ?)
            """,
            ("artifact_" + "c" * 32, *base, "c" * 64, created_at),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO artifacts(
                    artifact_id, job_id, server_id, upstream_node_id, output_key,
                    upstream_output_index, filename, subfolder, storage_type,
                    media_type, digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'first.png', '', 'output', 'image', ?, ?)
                """,
                ("artifact_" + "d" * 32, *base, "d" * 64, created_at),
            )


def test_initialize_rejects_existing_schema_tampering(tmp_path: Path) -> None:
    database = tmp_path / "control-plane.sqlite3"
    store = SQLiteControlPlaneStore(database)
    store.initialize()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE outbox")

    with pytest.raises(SchemaMigrationError, match="schema fingerprint"):
        store.initialize()


def test_schema_rejects_blob_values_in_text_identity_constraints(tmp_path: Path) -> None:
    database = tmp_path / "control-plane.sqlite3"
    SQLiteControlPlaneStore(database).initialize()
    created_at = "2026-07-30T00:00:00.000000Z"

    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO workflows(workflow_id, created_at) VALUES (?, ?)",
                (sqlite3.Binary(b"portrait"), created_at),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, workflow_id, owner_id, status, created_at,
                    created_at_source, legacy_migrated, execution_origin
                ) VALUES (?, 'portrait', 'owner', 'queued', ?, 'legacy_file_mtime', 1,
                          'legacy_migrated')
                """,
                (sqlite3.Binary(("job_" + "a" * 32).encode()), created_at),
            )


def test_immutable_snapshots_and_events_reject_delete(tmp_path: Path) -> None:
    database = tmp_path / "control-plane.sqlite3"
    SQLiteControlPlaneStore(database).initialize()
    created_at = "2026-07-30T00:00:00.000000Z"
    revision_id = "revision_" + "a" * 32

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO workflows(workflow_id, created_at) VALUES ('portrait', ?)",
            (created_at,),
        )
        connection.execute(
            """
            INSERT INTO workflow_revisions(
                revision_id, workflow_id, graph_json, parameter_schema_json,
                dependency_contract_json, content_digest, created_at
            ) VALUES (?, 'portrait', '{}', '{}', '{}', ?, ?)
            """,
            (revision_id, "a" * 64, created_at),
        )
        connection.execute(
            """
            INSERT INTO domain_events(
                event_id, event_type, subject_uri, sequence, occurred_at,
                principal_id, correlation_id, data_json
            ) VALUES ('event-1', 'test.created', 'comfyui://tests/1', 1, ?,
                      'principal', 'correlation', '{}')
            """,
            (created_at,),
        )

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM workflow_revisions WHERE revision_id = ?", (revision_id,)
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM domain_events WHERE event_id = 'event-1'")


def test_store_rejects_relative_and_symbolic_link_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        SQLiteControlPlaneStore(Path("relative.sqlite3"))

    target = tmp_path / "target.sqlite3"
    target.touch()
    link = tmp_path / "linked.sqlite3"
    try:
        link.symlink_to(target)
    except OSError:
        return
    with pytest.raises(ValueError, match="symbolic"):
        SQLiteControlPlaneStore(link)


def test_forward_migration_refreshes_fingerprints_and_multilevel_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "control-plane.sqlite3"
    store = SQLiteControlPlaneStore(database)
    store.initialize()
    migration_v9 = SchemaMigration(
        9,
        "add-probe-table",
        ("CREATE TABLE migration_probe (value TEXT NOT NULL)",),
        ("DROP TABLE migration_probe",),
    )
    monkeypatch.setattr(
        control_plane_module,
        "_MIGRATIONS",
        (*control_plane_module._MIGRATIONS, migration_v9),
    )
    store.initialize()

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT version, schema_fingerprint FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [row[0] for row in rows] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
        assert len({row[1] for row in rows}) == 1
        assert "migration_probe" in _table_names(database)
    with pytest.raises(SchemaMigrationError, match="cannot be rolled back"):
        store.rollback_schema(target_version=0)

    assert "jobs" in _table_names(database)
    assert "migration_probe" in _table_names(database)


def test_initialize_rejects_zero_fingerprint_and_schema_drift(tmp_path: Path) -> None:
    database = tmp_path / "control-plane.sqlite3"
    store = SQLiteControlPlaneStore(database)
    store.initialize()
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE schema_migrations SET schema_fingerprint = ?", ("0" * 64,))
        connection.execute("DROP TABLE outbox")

    with pytest.raises(SchemaMigrationError, match="schema fingerprint mismatch"):
        store.initialize()


def test_legacy_attempt_identity_is_deterministic() -> None:
    first = derive_legacy_attempt_id("job_" + "a" * 64, "local", 1)

    assert first == "attempt_a58870e656184f60c13594e3fadbbfbf78e65695e1668c235bf038cd901a15b0"
    assert derive_legacy_attempt_id("job_" + "b" * 64, "local", 1) != first
    assert derive_legacy_attempt_id("job_" + "a" * 64, "other", 1) != first
    assert derive_legacy_attempt_id("job_" + "a" * 64, "local", 2) != first
    for invalid in (True, 0, -1, 2**63):
        with pytest.raises(ValueError, match="attempt"):
            derive_legacy_attempt_id("job_" + "a" * 64, "local", invalid)


def test_g1_schema_preserves_runtime_and_migration_facts(tmp_path: Path) -> None:
    database = tmp_path / "control-plane.sqlite3"
    SQLiteControlPlaneStore(database).initialize()

    with sqlite3.connect(database) as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        job_columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        idempotency_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(idempotency_records)").fetchall()
        }
        artifact_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(artifacts)").fetchall()
        }

    assert versions == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,)]
    assert job_columns["execution_origin"][3] == 1
    assert "lease_token" in idempotency_columns
    assert "mime_type" in artifact_columns


def test_identity_and_foreign_key_columns_reject_blob_storage(tmp_path: Path) -> None:
    database = tmp_path / "control-plane.sqlite3"
    SQLiteControlPlaneStore(database).initialize()
    created_at = "2026-07-30T00:00:00.000000Z"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO domain_events(
                event_id, event_type, subject_uri, sequence, occurred_at,
                principal_id, correlation_id, data_json
            ) VALUES ('event-1', 'test.created', 'comfyui://tests/1', 1, ?, 'p', 'c', '{}')
            """,
            (created_at,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO outbox(
                    outbox_id, event_id, topic, payload_json, status, created_at
                ) VALUES ('outbox-1', ?, 'test', '{}', 'pending', ?)
                """,
                (sqlite3.Binary(b"event-1"), created_at),
            )


def _insert_historical_plans(
    connection: sqlite3.Connection,
    *,
    plans: tuple[tuple[str, str, str], ...],
    existing_asset_ids: tuple[str, ...],
    consumer_class: str = "LoadImage",
) -> tuple[str, str, str]:
    created_at = "2026-07-31T00:00:00.000000Z"
    revision_id = "revision_" + "1" * 32
    deployment_id = "deployment_" + "2" * 32
    graph = {"7": {"class_type": consumer_class, "inputs": {"image": "default.png"}}}
    schema = {"parameters": {"source": {"type": "image", "node_id": "7", "field": "image"}}}
    connection.execute(
        "INSERT INTO workflows(workflow_id,created_at) VALUES('historical-plan',?)",
        (created_at,),
    )
    connection.execute(
        """INSERT INTO workflow_revisions(
               revision_id,workflow_id,graph_json,parameter_schema_json,
               dependency_contract_json,content_digest,created_at
           ) VALUES(?,'historical-plan',?,?,?,?,?)""",
        (
            revision_id,
            json.dumps(graph, sort_keys=True, separators=(",", ":")),
            json.dumps(schema, sort_keys=True, separators=(",", ":")),
            "{}",
            "3" * 64,
            created_at,
        ),
    )
    connection.execute(
        """INSERT INTO workflow_deployments(
               deployment_id,workflow_id,revision_id,server_id,enabled,
               validation_status,published,created_at
           ) VALUES(?,'historical-plan',?,'local',1,'valid',1,?)""",
        (deployment_id, revision_id, created_at),
    )
    for asset_id in existing_asset_ids:
        connection.execute(
            """INSERT INTO assets(
                   asset_id,owner_id,server_id,name,subfolder,media_type,mime_type,
                   size_bytes,sha256,source_type,comfyui_ref,created_at,expires_at
               ) VALUES(?,'owner-a','local','source.png','inputs','image','image/png',
                        3,?,'upload','inputs/source.png',?,NULL)""",
            (asset_id, "4" * 64, created_at),
        )
    for index, (plan_id, job_id, asset_id) in enumerate(plans, start=1):
        snapshot = json.dumps(
            {"source": asset_id},
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            """INSERT INTO execution_plans(
                   plan_id,workflow_id,revision_id,deployment_id,server_id,
                   resolved_inputs_json,input_digest,plan_digest,created_at
               ) VALUES(?,'historical-plan',?,?,'local',?,?,?,?)""",
            (
                plan_id,
                revision_id,
                deployment_id,
                snapshot,
                hashlib.sha256(snapshot.encode()).hexdigest(),
                f"{index + 100:064x}",
                created_at,
            ),
        )
        connection.execute(
            """INSERT INTO jobs(
                   job_id,workflow_id,plan_id,revision_id,deployment_id,owner_id,
                   status,error,outputs_json,retry_of,created_at,created_at_source,
                   legacy_migrated,execution_origin
               ) VALUES(?,'historical-plan',?,?,?,'owner-a','completed','','[]',NULL,
                        ?,'runtime',0,'planned')""",
            (job_id, plan_id, revision_id, deployment_id, created_at),
        )
    return revision_id, deployment_id, created_at


def test_phase_l_upgrade_backfills_historical_execution_plan_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "control-plane.sqlite3"
    store = SQLiteControlPlaneStore(database)
    migrations = control_plane_module._MIGRATIONS
    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", migrations[:5])
    store.initialize()
    plan_id = "plan_" + "5" * 32
    job_id = "job_" + "6" * 32
    asset_id = "asset_" + "7" * 32
    with sqlite3.connect(database) as connection:
        revision_id, deployment_id, created_at = _insert_historical_plans(
            connection,
            plans=((plan_id, job_id, asset_id),),
            existing_asset_ids=(asset_id,),
        )
    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", migrations)

    store.initialize()
    store.initialize()

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            """SELECT owner_id,revision_id,deployment_id,parameter_name,
                      consumer_node_id,consumer_input_name,consumer_class,source_kind,
                      asset_id,artifact_id,source_job_id,reuse_strategy,source_digest,created_at
               FROM execution_plan_inputs WHERE plan_id=?""",
            (plan_id,),
        ).fetchone() == (
            "owner-a",
            revision_id,
            deployment_id,
            "source",
            "7",
            "image",
            "LoadImage",
            "asset",
            asset_id,
            None,
            None,
            "direct",
            "4" * 64,
            created_at,
        )
        assert connection.execute(
            """SELECT status,incomplete_count,completed_at IS NOT NULL,failure_code
               FROM phase_l_backfill_state
               WHERE backfill_name='execution_plan_inputs'"""
        ).fetchone() == ("complete", 0, 1, None)
        assert connection.execute(
            "SELECT count(*) FROM execution_plan_inputs WHERE plan_id=?", (plan_id,)
        ).fetchone() == (1,)


def test_phase_l_upgrade_backfills_historical_artifact_plan_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "control-plane.sqlite3"
    store = SQLiteControlPlaneStore(database)
    migrations = control_plane_module._MIGRATIONS
    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", migrations[:5])
    store.initialize()
    plan_id = "plan_" + "e" * 32
    plan_job_id = "job_" + "e" * 32
    source_job_id = "job_" + "f" * 32
    artifact_id = "artifact_" + "e" * 32
    artifact_uri = f"comfyui://artifacts/{artifact_id}"
    with sqlite3.connect(database) as connection:
        revision_id, deployment_id, _ = _insert_historical_plans(
            connection,
            plans=((plan_id, plan_job_id, artifact_uri),),
            existing_asset_ids=(),
            consumer_class="LoadImageOutput",
        )
        connection.execute(
            """INSERT INTO jobs(
                   job_id,workflow_id,plan_id,revision_id,deployment_id,owner_id,
                   status,error,outputs_json,retry_of,created_at,created_at_source,
                   legacy_migrated,execution_origin
               ) VALUES(?,'historical-plan',NULL,NULL,NULL,'owner-a','completed','','[]',
                        NULL,?,'legacy',1,'legacy_migrated')""",
            (source_job_id, "2026-07-31T00:00:00.000000Z"),
        )
        connection.execute(
            """INSERT INTO artifacts(
                   artifact_id,job_id,server_id,upstream_node_id,output_key,
                   upstream_output_index,filename,subfolder,storage_type,media_type,
                   digest,created_at,mime_type
               ) VALUES(?,?,'local','9','images',0,'source.png','renders','output',
                        'image',?,?,'image/png')""",
            (
                artifact_id,
                source_job_id,
                "a" * 64,
                "2026-07-31T00:00:00.000000Z",
            ),
        )
    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", migrations)

    store.initialize()
    store.initialize()

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            """SELECT owner_id,revision_id,deployment_id,parameter_name,
                      consumer_node_id,consumer_input_name,consumer_class,source_kind,
                      asset_id,artifact_id,source_job_id,reuse_strategy,source_digest
               FROM execution_plan_inputs WHERE plan_id=?""",
            (plan_id,),
        ).fetchone() == (
            "owner-a",
            revision_id,
            deployment_id,
            "source",
            "7",
            "image",
            "LoadImageOutput",
            "artifact",
            None,
            artifact_id,
            source_job_id,
            "direct",
            "a" * 64,
        )
        assert connection.execute(
            """SELECT status,incomplete_count,failure_code
               FROM phase_l_backfill_state
               WHERE backfill_name='execution_plan_inputs'"""
        ).fetchone() == ("complete", 0, None)


def test_phase_l_plan_input_backfill_fails_atomically_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "control-plane.sqlite3"
    store = SQLiteControlPlaneStore(database)
    migrations = control_plane_module._MIGRATIONS
    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", migrations[:5])
    store.initialize()
    good_plan_id = "plan_" + "8" * 32
    bad_plan_id = "plan_" + "9" * 32
    good_asset_id = "asset_" + "c" * 32
    missing_asset_id = "asset_" + "d" * 32
    with sqlite3.connect(database) as connection:
        _insert_historical_plans(
            connection,
            plans=(
                (good_plan_id, "job_" + "a" * 32, good_asset_id),
                (bad_plan_id, "job_" + "b" * 32, missing_asset_id),
            ),
            existing_asset_ids=(good_asset_id,),
        )
    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", migrations)

    store.initialize()
    store.initialize()

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            """SELECT status,incomplete_count,completed_at,failure_code
               FROM phase_l_backfill_state
               WHERE backfill_name='execution_plan_inputs'"""
        ).fetchone() == (
            "failed",
            2,
            None,
            "execution_plan_inputs_unreconstructable",
        )
        assert connection.execute("SELECT count(*) FROM execution_plan_inputs").fetchone() == (0,)
        connection.execute(
            """INSERT INTO assets(
                   asset_id,owner_id,server_id,name,subfolder,media_type,mime_type,
                   size_bytes,sha256,source_type,comfyui_ref,created_at,expires_at
               ) VALUES(?,'owner-a','local','source.png','inputs','image','image/png',
                        3,?,'upload','inputs/source.png',?,NULL)""",
            (missing_asset_id, "4" * 64, "2026-07-31T00:00:00.000000Z"),
        )

    store.initialize()
    store.initialize()

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            """SELECT status,incomplete_count,completed_at IS NOT NULL,failure_code
               FROM phase_l_backfill_state
               WHERE backfill_name='execution_plan_inputs'"""
        ).fetchone() == ("complete", 0, 1, None)
        assert connection.execute(
            """SELECT plan_id,asset_id FROM execution_plan_inputs
               ORDER BY plan_id"""
        ).fetchall() == [
            (good_plan_id, good_asset_id),
            (bad_plan_id, missing_asset_id),
        ]


def test_phase_l_fresh_database_marks_backfills_complete(tmp_path: Path) -> None:
    database = tmp_path / "control-plane.sqlite3"
    SQLiteControlPlaneStore(database).initialize()

    with sqlite3.connect(database) as connection:
        states = connection.execute(
            """SELECT backfill_name,status,incomplete_count,completed_at IS NOT NULL
               FROM phase_l_backfill_state ORDER BY backfill_name"""
        ).fetchall()

    assert states == [
        ("artifact_outputs", "complete", 0, 1),
        ("execution_plan_inputs", "complete", 0, 1),
    ]


def test_phase_l_upgrade_marks_outputful_jobs_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "control-plane.sqlite3"
    migrations = control_plane_module._MIGRATIONS
    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", migrations[:5])
    SQLiteControlPlaneStore(database).initialize()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO jobs(
                job_id,workflow_id,plan_id,revision_id,deployment_id,owner_id,
                status,error,outputs_json,retry_of,created_at,created_at_source,
                legacy_migrated,execution_origin
            ) VALUES(?, 'legacy-workflow', NULL, NULL, NULL, 'owner-a',
                     'completed', '', '[{"filename":"old.png"}]', NULL, ?,
                     'legacy', 1, 'legacy_migrated')""",
            ("job_" + "a" * 32, "2026-07-31T00:00:00+00:00"),
        )
    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", migrations)

    SQLiteControlPlaneStore(database).initialize()

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            """SELECT status,incomplete_count,completed_at
               FROM phase_l_backfill_state WHERE backfill_name='artifact_outputs'"""
        ).fetchone() == ("pending", 1, None)
        assert connection.execute(
            "SELECT status FROM job_artifact_collections WHERE job_id=?",
            ("job_" + "a" * 32,),
        ).fetchone() == ("needs_backfill",)
