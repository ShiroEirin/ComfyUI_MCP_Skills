"""SQLite schema and transaction boundary for the agent-native control plane."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from comfyui_mcp_skills.application.control_plane_ports import (
        ControlPlaneUnitOfWork,
    )


class SchemaMigrationError(RuntimeError):
    """Raised when an applied schema migration differs from local code."""


@dataclass(frozen=True, slots=True)
class SchemaMigration:
    version: int
    name: str
    up: tuple[str, ...]
    down: tuple[str, ...]
    feasibility_note: str = "transactional DDL before aggregate cutover"
    bootstrap_sql: str = ""

    @property
    def up_supported(self) -> bool:
        return bool(self.up)

    @property
    def down_supported(self) -> bool:
        return bool(self.down)

    @property
    def checksum(self) -> str:
        payload = json.dumps(
            {
                "version": self.version,
                "name": self.name,
                "up": self.up,
                "down": self.down,
                "up_supported": self.up_supported,
                "down_supported": self.down_supported,
                "feasibility_note": self.feasibility_note,
                "bootstrap_sql": self.bootstrap_sql,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def _sha256_check(column: str) -> str:
    return (
        f"typeof({column}) = 'text' AND length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"
    )


def _typed_id_check(column: str, prefix: str) -> str:
    prefix_length = len(prefix)
    return (
        f"typeof({column}) = 'text' "
        f"AND substr({column}, 1, {prefix_length}) = '{prefix}' "
        f"AND length({column}) IN ({prefix_length + 32}, {prefix_length + 64}) "
        f"AND substr({column}, {prefix_length + 1}) NOT GLOB '*[^0-9a-f]*'"
    )


def _safe_identifier_check(column: str) -> str:
    return (
        f"typeof({column}) = 'text' "
        f"AND length({column}) BETWEEN 1 AND 128 "
        f"AND substr({column}, 1, 1) GLOB '[A-Za-z0-9]' "
        f"AND {column} NOT GLOB '*[^A-Za-z0-9_-]*'"
    )


def _workflow_id_check(column: str) -> str:
    other_typed_ids = " AND ".join(
        f"NOT ({_typed_id_check(column, prefix)})"
        for prefix in (
            "revision_",
            "deployment_",
            "plan_",
            "job_",
            "attempt_",
            "asset_",
            "artifact_",
        )
    )
    return f"({_safe_identifier_check(column)}) AND {other_typed_ids}"


_SCHEMA_MIGRATIONS_SQL = f"""
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER NOT NULL PRIMARY KEY CHECK(typeof(version) = 'integer' AND version > 0),
    name TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL CHECK({_sha256_check("checksum")}),
    up_supported INTEGER NOT NULL CHECK(
        typeof(up_supported) = 'integer' AND up_supported IN (0, 1)
    ),
    down_supported INTEGER NOT NULL CHECK(
        typeof(down_supported) = 'integer' AND down_supported IN (0, 1)
    ),
    feasibility_note TEXT NOT NULL,
    schema_fingerprint TEXT NOT NULL CHECK({_sha256_check("schema_fingerprint")}),
    applied_at TEXT NOT NULL
)
"""

_INITIAL_UP = (
    f"""
    CREATE TABLE store_migrations (
        aggregate_kind TEXT NOT NULL,
        version INTEGER NOT NULL CHECK(typeof(version) = 'integer' AND version > 0),
        status TEXT NOT NULL CHECK(
            status IN ('pending', 'migrating', 'switched', 'failed', 'superseded')
        ),
        checksum TEXT NOT NULL CHECK({_sha256_check("checksum")}),
        switched_at TEXT,
        PRIMARY KEY (aggregate_kind, version),
        CHECK(aggregate_kind IN (
            'workflow', 'revision', 'deployment', 'plan', 'job',
            'execution_attempt', 'idempotency_record', 'asset', 'artifact'
        )),
        CHECK(
            (status IN ('switched', 'superseded')) = (switched_at IS NOT NULL)
        )
    )
    """,
    """
    CREATE TRIGGER tr_store_migrations_preserve_cutover_update
    BEFORE UPDATE ON store_migrations
    WHEN OLD.switched_at IS NOT NULL AND (
        NEW.switched_at IS NOT OLD.switched_at OR
        NEW.aggregate_kind != OLD.aggregate_kind OR
        NEW.version != OLD.version OR NEW.checksum != OLD.checksum OR
        NOT (
            NEW.status = OLD.status OR
            (OLD.status = 'switched' AND NEW.status = 'superseded')
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'store migration cutover evidence is immutable');
    END
    """,
    """
    CREATE TRIGGER tr_store_migrations_preserve_cutover_delete
    BEFORE DELETE ON store_migrations
    WHEN OLD.switched_at IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'store migration cutover evidence is immutable');
    END
    """,
    """
    CREATE UNIQUE INDEX uq_store_migrations_switched_kind
    ON store_migrations(aggregate_kind) WHERE status = 'switched'
    """,
    f"""
    CREATE TABLE workflows (
        workflow_id TEXT NOT NULL PRIMARY KEY CHECK({_workflow_id_check("workflow_id")}),
        created_at TEXT NOT NULL
    )
    """,
    f"""
    CREATE TABLE workflow_revisions (
        revision_id TEXT NOT NULL PRIMARY KEY CHECK({_typed_id_check("revision_id", "revision_")}),
        workflow_id TEXT NOT NULL,
        graph_json TEXT NOT NULL,
        parameter_schema_json TEXT NOT NULL,
        dependency_contract_json TEXT NOT NULL,
        content_digest TEXT NOT NULL CHECK({_sha256_check("content_digest")}),
        created_at TEXT NOT NULL,
        FOREIGN KEY(workflow_id) REFERENCES workflows(workflow_id) ON DELETE RESTRICT,
        UNIQUE(workflow_id, content_digest),
        UNIQUE(workflow_id, revision_id),
        CHECK(typeof(workflow_id) = 'text')
    )
    """,
    """
    CREATE TRIGGER tr_workflow_revisions_immutable
    BEFORE UPDATE ON workflow_revisions
    BEGIN
        SELECT RAISE(ABORT, 'workflow revision is immutable');
    END
    """,
    """
    CREATE TRIGGER tr_workflow_revisions_no_delete
    BEFORE DELETE ON workflow_revisions
    BEGIN
        SELECT RAISE(ABORT, 'workflow revision is immutable');
    END
    """,
    "CREATE INDEX ix_workflow_revisions_workflow ON workflow_revisions(workflow_id)",
    f"""
    CREATE TABLE workflow_deployments (
        deployment_id TEXT NOT NULL PRIMARY KEY CHECK(
            {_typed_id_check("deployment_id", "deployment_")}
        ),
        workflow_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        server_id TEXT NOT NULL CHECK({_safe_identifier_check("server_id")}),
        enabled INTEGER NOT NULL CHECK(typeof(enabled) = 'integer' AND enabled IN (0, 1)),
        validation_status TEXT NOT NULL CHECK(length(validation_status) > 0),
        published INTEGER NOT NULL CHECK(typeof(published) = 'integer' AND published IN (0, 1)),
        created_at TEXT NOT NULL,
        FOREIGN KEY(workflow_id, revision_id)
            REFERENCES workflow_revisions(workflow_id, revision_id) ON DELETE RESTRICT,
        UNIQUE(revision_id, server_id),
        UNIQUE(deployment_id, workflow_id, revision_id, server_id),
        CHECK(typeof(workflow_id) = 'text' AND typeof(revision_id) = 'text')
    )
    """,
    """
    CREATE UNIQUE INDEX uq_workflow_deployments_published
    ON workflow_deployments(workflow_id, server_id) WHERE published = 1
    """,
    """
    CREATE INDEX ix_workflow_deployments_revision
    ON workflow_deployments(workflow_id, revision_id)
    """,
    f"""
    CREATE TABLE execution_plans (
        plan_id TEXT NOT NULL PRIMARY KEY CHECK({_typed_id_check("plan_id", "plan_")}),
        workflow_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        deployment_id TEXT NOT NULL,
        server_id TEXT NOT NULL CHECK({_safe_identifier_check("server_id")}),
        resolved_inputs_json TEXT NOT NULL,
        input_digest TEXT NOT NULL CHECK({_sha256_check("input_digest")}),
        plan_digest TEXT NOT NULL UNIQUE CHECK({_sha256_check("plan_digest")}),
        created_at TEXT NOT NULL,
        FOREIGN KEY(deployment_id, workflow_id, revision_id, server_id)
            REFERENCES workflow_deployments(
                deployment_id, workflow_id, revision_id, server_id
            ) ON DELETE RESTRICT,
        UNIQUE(plan_id, workflow_id, revision_id, deployment_id),
        CHECK(
            typeof(workflow_id) = 'text' AND typeof(revision_id) = 'text' AND
            typeof(deployment_id) = 'text'
        )
    )
    """,
    """
    CREATE TRIGGER tr_execution_plans_immutable
    BEFORE UPDATE ON execution_plans
    BEGIN
        SELECT RAISE(ABORT, 'execution plan is immutable');
    END
    """,
    """
    CREATE TRIGGER tr_execution_plans_no_delete
    BEFORE DELETE ON execution_plans
    BEGIN
        SELECT RAISE(ABORT, 'execution plan is immutable');
    END
    """,
    """
    CREATE INDEX ix_execution_plans_deployment
    ON execution_plans(deployment_id, workflow_id, revision_id, server_id)
    """,
    f"""
    CREATE TABLE jobs (
        job_id TEXT NOT NULL PRIMARY KEY CHECK({_typed_id_check("job_id", "job_")}),
        workflow_id TEXT NOT NULL CHECK({_workflow_id_check("workflow_id")}),
        plan_id TEXT,
        revision_id TEXT,
        deployment_id TEXT,
        owner_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(length(status) > 0),
        retry_of TEXT,
        created_at TEXT NOT NULL,
        created_at_source TEXT NOT NULL,
        legacy_migrated INTEGER NOT NULL DEFAULT 0 CHECK(
            typeof(legacy_migrated) = 'integer' AND legacy_migrated IN (0, 1)
        ),
        CHECK(retry_of IS NULL OR retry_of != job_id),
        CHECK(
            (
                plan_id IS NULL AND revision_id IS NULL AND deployment_id IS NULL AND
                legacy_migrated = 1
            ) OR (
                plan_id IS NOT NULL AND revision_id IS NOT NULL AND deployment_id IS NOT NULL
            )
        ),
        FOREIGN KEY(plan_id, workflow_id, revision_id, deployment_id)
            REFERENCES execution_plans(
                plan_id, workflow_id, revision_id, deployment_id
            ) ON DELETE RESTRICT
        ,UNIQUE(job_id, owner_id)
        ,UNIQUE(job_id, owner_id, workflow_id)
        ,FOREIGN KEY(retry_of, owner_id, workflow_id)
            REFERENCES jobs(job_id, owner_id, workflow_id) ON DELETE RESTRICT
        ,CHECK(typeof(owner_id) = 'text')
        ,CHECK(retry_of IS NULL OR typeof(retry_of) = 'text')
        ,CHECK(plan_id IS NULL OR typeof(plan_id) = 'text')
        ,CHECK(revision_id IS NULL OR typeof(revision_id) = 'text')
        ,CHECK(deployment_id IS NULL OR typeof(deployment_id) = 'text')
    )
    """,
    """
    CREATE TRIGGER tr_jobs_execution_identity_immutable
    BEFORE UPDATE OF
        job_id, workflow_id, plan_id, revision_id, deployment_id, owner_id,
        retry_of, created_at, created_at_source, legacy_migrated
    ON jobs
    BEGIN
        SELECT RAISE(ABORT, 'job execution identity is immutable');
    END
    """,
    """
    CREATE TRIGGER tr_jobs_plan_binding_server_consistency
    BEFORE UPDATE OF plan_id, revision_id, deployment_id, workflow_id ON jobs
    WHEN NEW.plan_id IS NOT NULL AND (
        EXISTS (
            SELECT 1 FROM execution_attempts, execution_plans
            WHERE execution_attempts.job_id = NEW.job_id AND
                  execution_plans.plan_id = NEW.plan_id AND
                  execution_attempts.server_id != execution_plans.server_id
        ) OR EXISTS (
            SELECT 1 FROM artifacts, execution_plans
            WHERE artifacts.job_id = NEW.job_id AND
                  execution_plans.plan_id = NEW.plan_id AND
                  artifacts.server_id != execution_plans.server_id
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'job plan server conflicts with existing execution data');
    END
    """,
    "CREATE INDEX ix_jobs_owner_created ON jobs(owner_id, created_at DESC, job_id)",
    """
    CREATE INDEX ix_jobs_owner_status_created
    ON jobs(owner_id, status, created_at DESC, job_id)
    """,
    """
    CREATE INDEX ix_jobs_owner_workflow_created
    ON jobs(owner_id, workflow_id, created_at DESC, job_id)
    """,
    """
    CREATE INDEX ix_jobs_plan_binding
    ON jobs(plan_id, workflow_id, revision_id, deployment_id)
    """,
    "CREATE INDEX ix_jobs_retry_of ON jobs(retry_of) WHERE retry_of IS NOT NULL",
    f"""
    CREATE TABLE execution_attempts (
        attempt_id TEXT NOT NULL PRIMARY KEY CHECK({_typed_id_check("attempt_id", "attempt_")}),
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
        attempt INTEGER NOT NULL CHECK(typeof(attempt) = 'integer' AND attempt > 0),
        server_id TEXT NOT NULL CHECK({_safe_identifier_check("server_id")}),
        upstream_prompt_id TEXT CHECK(
            upstream_prompt_id IS NULL OR {_safe_identifier_check("upstream_prompt_id")}
        ),
        upstream_job_id TEXT CHECK(
            upstream_job_id IS NULL OR {_safe_identifier_check("upstream_job_id")}
        ),
        client_id TEXT NOT NULL,
        submission_state TEXT NOT NULL CHECK(
            submission_state IN ('submission_unknown', 'submitted')
        ),
        created_at TEXT NOT NULL,
        UNIQUE(job_id, attempt),
        CHECK(typeof(job_id) = 'text'),
        CHECK(upstream_prompt_id IS NULL OR typeof(upstream_prompt_id) = 'text'),
        CHECK(upstream_job_id IS NULL OR typeof(upstream_job_id) = 'text'),
        CHECK(
            (submission_state = 'submission_unknown' AND
             upstream_prompt_id IS NULL AND upstream_job_id IS NULL) OR
            (submission_state = 'submitted' AND
             (upstream_prompt_id IS NOT NULL OR upstream_job_id IS NOT NULL))
        )
    )
    """,
    """
    CREATE UNIQUE INDEX uq_execution_attempts_upstream_prompt
    ON execution_attempts(server_id, upstream_prompt_id)
    WHERE upstream_prompt_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX uq_execution_attempts_upstream_job
    ON execution_attempts(server_id, upstream_job_id)
    WHERE upstream_job_id IS NOT NULL
    """,
    "CREATE INDEX ix_execution_attempts_job ON execution_attempts(job_id)",
    """
    CREATE TRIGGER tr_execution_attempts_plan_server_insert
    BEFORE INSERT ON execution_attempts
    WHEN EXISTS (
        SELECT 1 FROM jobs JOIN execution_plans USING(plan_id)
        WHERE jobs.job_id = NEW.job_id AND execution_plans.server_id != NEW.server_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'attempt server must match execution plan');
    END
    """,
    """
    CREATE TRIGGER tr_execution_attempts_identity_immutable
    BEFORE UPDATE OF attempt_id, job_id, attempt, server_id, client_id, created_at
    ON execution_attempts
    BEGIN
        SELECT RAISE(ABORT, 'execution attempt is immutable');
    END
    """,
    """
    CREATE TRIGGER tr_execution_attempts_reconciliation_guard
    BEFORE UPDATE OF upstream_prompt_id, upstream_job_id, submission_state
    ON execution_attempts
    WHEN NOT (
        OLD.submission_state = 'submission_unknown' AND
        OLD.upstream_prompt_id IS NULL AND OLD.upstream_job_id IS NULL AND
        NEW.submission_state = 'submitted' AND
        (NEW.upstream_prompt_id IS NOT NULL OR NEW.upstream_job_id IS NOT NULL)
    )
    BEGIN
        SELECT RAISE(ABORT, 'execution attempt reconciliation is append-once');
    END
    """,
    """
    CREATE TRIGGER tr_execution_attempts_immutable_delete
    BEFORE DELETE ON execution_attempts
    BEGIN
        SELECT RAISE(ABORT, 'execution attempt is immutable');
    END
    """,
    f"""
    CREATE TABLE idempotency_records (
        owner_id TEXT NOT NULL,
        scope TEXT NOT NULL,
        key TEXT NOT NULL,
        request_digest TEXT NOT NULL CHECK({_sha256_check("request_digest")}),
        state TEXT NOT NULL CHECK(
            state IN ('reserved', 'submission_unknown', 'resolved', 'expired')
        ),
        job_id TEXT,
        client_id TEXT NOT NULL,
        claimed_at TEXT NOT NULL,
        expires_at TEXT,
        PRIMARY KEY(owner_id, scope, key),
        CHECK(state != 'expired' OR job_id IS NULL),
        CHECK(state != 'reserved' OR expires_at IS NOT NULL)
        ,FOREIGN KEY(job_id, owner_id) REFERENCES jobs(job_id, owner_id) ON DELETE RESTRICT
        ,CHECK(typeof(owner_id) = 'text' AND typeof(scope) = 'text' AND typeof(key) = 'text')
        ,CHECK(job_id IS NULL OR typeof(job_id) = 'text')
    )
    """,
    """
    CREATE INDEX ix_idempotency_records_job ON idempotency_records(job_id)
    WHERE job_id IS NOT NULL
    """,
    """
    CREATE INDEX ix_idempotency_records_reserved_expiry
    ON idempotency_records(expires_at) WHERE state = 'reserved'
    """,
    f"""
    CREATE TABLE assets (
        asset_id TEXT NOT NULL PRIMARY KEY CHECK({_typed_id_check("asset_id", "asset_")}),
        owner_id TEXT NOT NULL,
        server_id TEXT NOT NULL CHECK({_safe_identifier_check("server_id")}),
        name TEXT NOT NULL CHECK(length(name) > 0),
        subfolder TEXT NOT NULL,
        media_type TEXT NOT NULL CHECK(media_type IN ('image', 'audio', 'video')),
        mime_type TEXT NOT NULL CHECK(length(mime_type) > 0),
        size_bytes INTEGER NOT NULL CHECK(typeof(size_bytes) = 'integer' AND size_bytes >= 0),
        sha256 TEXT NOT NULL CHECK({_sha256_check("sha256")}),
        source_type TEXT NOT NULL CHECK(length(source_type) > 0),
        comfyui_ref TEXT NOT NULL CHECK(length(comfyui_ref) > 0),
        created_at TEXT NOT NULL,
        expires_at TEXT,
        CHECK(typeof(owner_id) = 'text')
    )
    """,
    "CREATE INDEX ix_assets_owner_created ON assets(owner_id, created_at DESC, asset_id)",
    f"""
    CREATE TABLE artifacts (
        artifact_id TEXT NOT NULL PRIMARY KEY CHECK({_typed_id_check("artifact_id", "artifact_")}),
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
        server_id TEXT NOT NULL CHECK({_safe_identifier_check("server_id")}),
        upstream_node_id TEXT NOT NULL CHECK({_safe_identifier_check("upstream_node_id")}),
        output_key TEXT NOT NULL CHECK({_safe_identifier_check("output_key")}),
        upstream_output_index INTEGER NOT NULL CHECK(
            typeof(upstream_output_index) = 'integer' AND
            upstream_output_index BETWEEN 0 AND 2147483647
        ),
        filename TEXT NOT NULL CHECK(length(filename) > 0),
        subfolder TEXT NOT NULL,
        storage_type TEXT NOT NULL CHECK(storage_type = 'output'),
        media_type TEXT NOT NULL CHECK(media_type IN ('image', 'audio', 'video')),
        digest TEXT NOT NULL CHECK({_sha256_check("digest")}),
        created_at TEXT NOT NULL,
        UNIQUE(
            job_id, upstream_node_id, output_key, upstream_output_index,
            filename, subfolder, storage_type
        ),
        CHECK(typeof(job_id) = 'text')
        ,CHECK(
            typeof(filename) = 'text' AND instr(filename, char(0)) = 0 AND
            typeof(subfolder) = 'text' AND instr(subfolder, char(0)) = 0 AND
            typeof(storage_type) = 'text'
        )
    )
    """,
    "CREATE INDEX ix_artifacts_job ON artifacts(job_id, artifact_id)",
    """
    CREATE TRIGGER tr_artifacts_plan_server_insert
    BEFORE INSERT ON artifacts
    WHEN EXISTS (
        SELECT 1 FROM jobs JOIN execution_plans USING(plan_id)
        WHERE jobs.job_id = NEW.job_id AND execution_plans.server_id != NEW.server_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'artifact server must match execution plan');
    END
    """,
    """
    CREATE TRIGGER tr_artifacts_immutable_update
    BEFORE UPDATE ON artifacts
    BEGIN
        SELECT RAISE(ABORT, 'artifact is immutable');
    END
    """,
    """
    CREATE TRIGGER tr_artifacts_immutable_delete
    BEFORE DELETE ON artifacts
    BEGIN
        SELECT RAISE(ABORT, 'artifact is immutable');
    END
    """,
    """
    CREATE TABLE legacy_resource_aliases (
        alias_uri TEXT NOT NULL PRIMARY KEY,
        canonical_uri TEXT NOT NULL,
        object_kind TEXT NOT NULL CHECK(object_kind IN ('workflow', 'asset', 'job', 'output')),
        workflow_id TEXT REFERENCES workflows(workflow_id) ON DELETE RESTRICT,
        asset_id TEXT REFERENCES assets(asset_id) ON DELETE RESTRICT,
        job_id TEXT REFERENCES jobs(job_id) ON DELETE RESTRICT,
        artifact_id TEXT REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
        created_at TEXT NOT NULL,
        CHECK(
            (
                object_kind = 'workflow' AND workflow_id IS NOT NULL AND
                asset_id IS NULL AND job_id IS NULL AND artifact_id IS NULL
            ) OR (
                object_kind = 'asset' AND workflow_id IS NULL AND
                asset_id IS NOT NULL AND job_id IS NULL AND artifact_id IS NULL
            ) OR (
                object_kind = 'job' AND workflow_id IS NULL AND asset_id IS NULL AND
                job_id IS NOT NULL AND artifact_id IS NULL
            ) OR (
                object_kind = 'output' AND workflow_id IS NULL AND asset_id IS NULL AND
                job_id IS NULL AND artifact_id IS NOT NULL
            )
        ),
        CHECK(
            (object_kind = 'workflow' AND canonical_uri = 'comfyui://workflows/' || workflow_id) OR
            (object_kind = 'asset' AND canonical_uri = 'comfyui://assets/' || asset_id) OR
            (object_kind = 'job' AND canonical_uri = 'comfyui://jobs/' || job_id) OR
            (object_kind = 'output' AND canonical_uri = 'comfyui://artifacts/' || artifact_id)
        )
        ,CHECK(
            typeof(alias_uri) = 'text' AND typeof(canonical_uri) = 'text' AND
            typeof(object_kind) = 'text' AND
            (workflow_id IS NULL OR typeof(workflow_id) = 'text') AND
            (asset_id IS NULL OR typeof(asset_id) = 'text') AND
            (job_id IS NULL OR typeof(job_id) = 'text') AND
            (artifact_id IS NULL OR typeof(artifact_id) = 'text')
        )
    )
    """,
    """
    CREATE TRIGGER tr_legacy_resource_aliases_immutable_update
    BEFORE UPDATE ON legacy_resource_aliases
    BEGIN
        SELECT RAISE(ABORT, 'legacy resource alias is immutable');
    END
    """,
    """
    CREATE TRIGGER tr_legacy_resource_aliases_immutable_delete
    BEFORE DELETE ON legacy_resource_aliases
    BEGIN
        SELECT RAISE(ABORT, 'legacy resource alias is immutable');
    END
    """,
    """
    CREATE INDEX ix_legacy_resource_aliases_workflow
    ON legacy_resource_aliases(workflow_id) WHERE workflow_id IS NOT NULL
    """,
    """
    CREATE INDEX ix_legacy_resource_aliases_asset
    ON legacy_resource_aliases(asset_id) WHERE asset_id IS NOT NULL
    """,
    """
    CREATE INDEX ix_legacy_resource_aliases_job
    ON legacy_resource_aliases(job_id) WHERE job_id IS NOT NULL
    """,
    """
    CREATE INDEX ix_legacy_resource_aliases_artifact
    ON legacy_resource_aliases(artifact_id) WHERE artifact_id IS NOT NULL
    """,
    """
    CREATE TABLE test_migration_database_role (
        singleton INTEGER NOT NULL PRIMARY KEY CHECK(singleton = 1),
        role TEXT NOT NULL CHECK(role IN ('g0_isolated_rehearsal', 'g0_contract_harness')),
        nonce TEXT NOT NULL CHECK(
            typeof(nonce) = 'text' AND length(nonce) = 64 AND
            nonce NOT GLOB '*[^0-9a-f]*'
        )
    )
    """,
    f"""
    CREATE TABLE test_migration_switches (
        rehearsal_name TEXT NOT NULL PRIMARY KEY,
        version INTEGER NOT NULL CHECK(typeof(version) = 'integer' AND version > 0),
        checksum TEXT NOT NULL CHECK({_sha256_check("checksum")}),
        status TEXT NOT NULL CHECK(status = 'switched'),
        switched_at TEXT NOT NULL CHECK(typeof(switched_at) = 'text'),
        CHECK(typeof(rehearsal_name) = 'text')
    )
    """,
    """
    CREATE TABLE test_aggregates (
        aggregate_id TEXT NOT NULL PRIMARY KEY,
        payload_json TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(typeof(revision) = 'integer' AND revision >= 0),
        created_at TEXT NOT NULL,
        CHECK(typeof(aggregate_id) = 'text')
    )
    """,
    """
    CREATE TABLE work_items (
        work_item_id TEXT NOT NULL PRIMARY KEY,
        aggregate_id TEXT NOT NULL REFERENCES test_aggregates(aggregate_id) ON DELETE RESTRICT,
        work_type TEXT NOT NULL CHECK(length(work_type) > 0),
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(length(status) > 0),
        created_at TEXT NOT NULL,
        CHECK(typeof(work_item_id) = 'text' AND typeof(aggregate_id) = 'text')
    )
    """,
    """
    CREATE INDEX ix_work_items_aggregate
    ON work_items(aggregate_id, created_at, work_item_id)
    """,
    """
    CREATE TABLE domain_events (
        event_id TEXT NOT NULL PRIMARY KEY,
        event_type TEXT NOT NULL CHECK(length(event_type) > 0),
        subject_uri TEXT NOT NULL CHECK(length(subject_uri) > 0),
        sequence INTEGER NOT NULL CHECK(typeof(sequence) = 'integer' AND sequence > 0),
        occurred_at TEXT NOT NULL,
        principal_id TEXT NOT NULL,
        correlation_id TEXT NOT NULL,
        data_json TEXT NOT NULL,
        UNIQUE(subject_uri, sequence),
        CHECK(typeof(event_id) = 'text' AND typeof(subject_uri) = 'text')
    )
    """,
    """
    CREATE TRIGGER tr_domain_events_immutable_update
    BEFORE UPDATE ON domain_events
    BEGIN
        SELECT RAISE(ABORT, 'domain event is immutable');
    END
    """,
    """
    CREATE TRIGGER tr_domain_events_immutable_delete
    BEFORE DELETE ON domain_events
    BEGIN
        SELECT RAISE(ABORT, 'domain event is immutable');
    END
    """,
    """
    CREATE TABLE outbox (
        outbox_id TEXT NOT NULL PRIMARY KEY,
        event_id TEXT NOT NULL UNIQUE REFERENCES domain_events(event_id) ON DELETE RESTRICT,
        topic TEXT NOT NULL CHECK(length(topic) > 0),
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(
            status IN ('pending', 'delivering', 'delivered', 'failed')
        ),
        created_at TEXT NOT NULL,
        delivered_at TEXT,
        CHECK((status = 'delivered') = (delivered_at IS NOT NULL)),
        CHECK(typeof(outbox_id) = 'text' AND typeof(event_id) = 'text')
    )
    """,
    """
    CREATE INDEX ix_outbox_pending
    ON outbox(created_at, outbox_id) WHERE status = 'pending'
    """,
)

_INITIAL_DOWN = tuple(
    f"DROP TABLE IF EXISTS {name}"
    for name in (
        "outbox",
        "domain_events",
        "work_items",
        "test_migration_database_role",
        "test_migration_switches",
        "test_aggregates",
        "legacy_resource_aliases",
        "artifacts",
        "assets",
        "idempotency_records",
        "execution_attempts",
        "jobs",
        "execution_plans",
        "workflow_deployments",
        "workflow_revisions",
        "workflows",
        "store_migrations",
    )
)

_JOBS_V2_SQL = f"""
CREATE TABLE jobs_v2 (
    job_id TEXT NOT NULL PRIMARY KEY CHECK({_typed_id_check("job_id", "job_")}),
    workflow_id TEXT NOT NULL CHECK({_workflow_id_check("workflow_id")}),
    plan_id TEXT,
    revision_id TEXT,
    deployment_id TEXT,
    owner_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(length(status) > 0),
    error TEXT NOT NULL DEFAULT '' CHECK(typeof(error) = 'text'),
    outputs_json TEXT NOT NULL DEFAULT '[]' CHECK(typeof(outputs_json) = 'text'),
    retry_of TEXT,
    created_at TEXT NOT NULL,
    created_at_source TEXT NOT NULL,
    legacy_migrated INTEGER NOT NULL DEFAULT 0 CHECK(
        typeof(legacy_migrated) = 'integer' AND legacy_migrated IN (0, 1)
    ),
    execution_origin TEXT NOT NULL CHECK(
        execution_origin IN ('legacy_migrated', 'pre_g4_runtime', 'planned')
    ),
    CHECK(retry_of IS NULL OR retry_of != job_id),
    CHECK(
        (execution_origin = 'legacy_migrated' AND legacy_migrated = 1 AND
         plan_id IS NULL AND revision_id IS NULL AND deployment_id IS NULL) OR
        (execution_origin = 'pre_g4_runtime' AND legacy_migrated = 0 AND
         plan_id IS NULL AND revision_id IS NULL AND deployment_id IS NULL) OR
        (execution_origin = 'planned' AND legacy_migrated = 0 AND
         plan_id IS NOT NULL AND revision_id IS NOT NULL AND deployment_id IS NOT NULL)
    ),
    FOREIGN KEY(plan_id, workflow_id, revision_id, deployment_id)
        REFERENCES execution_plans(plan_id, workflow_id, revision_id, deployment_id)
        ON DELETE RESTRICT,
    UNIQUE(job_id, owner_id),
    UNIQUE(job_id, owner_id, workflow_id),
    FOREIGN KEY(retry_of, owner_id, workflow_id)
        REFERENCES jobs_v2(job_id, owner_id, workflow_id) ON DELETE RESTRICT,
    CHECK(typeof(owner_id) = 'text'),
    CHECK(retry_of IS NULL OR typeof(retry_of) = 'text')
)
"""

_G1_SCHEMA_UP = (
    "DROP TRIGGER IF EXISTS tr_execution_attempts_plan_server_insert",
    "DROP TRIGGER IF EXISTS tr_artifacts_plan_server_insert",
    "DROP TRIGGER IF EXISTS tr_jobs_plan_binding_server_consistency",
    _JOBS_V2_SQL,
    """
    INSERT INTO jobs_v2(
        job_id, workflow_id, plan_id, revision_id, deployment_id, owner_id,
        status, error, outputs_json, retry_of, created_at, created_at_source,
        legacy_migrated, execution_origin
    )
    SELECT job_id, workflow_id,
           CASE WHEN legacy_migrated = 1 THEN NULL ELSE plan_id END,
           CASE WHEN legacy_migrated = 1 THEN NULL ELSE revision_id END,
           CASE WHEN legacy_migrated = 1 THEN NULL ELSE deployment_id END,
           owner_id, status, '', '[]', retry_of, created_at, created_at_source,
           legacy_migrated,
           CASE
               WHEN legacy_migrated = 1 THEN 'legacy_migrated'
               ELSE 'planned'
           END
    FROM jobs
    """,
    "ALTER TABLE idempotency_records ADD COLUMN lease_token TEXT",
    "CREATE TEMP TABLE g1_reserved_guard(value INTEGER NOT NULL CHECK(value = 0))",
    "INSERT INTO g1_reserved_guard SELECT count(*) FROM idempotency_records "
    "WHERE state = 'reserved' AND "
    "(expires_at IS NULL OR julianday(expires_at) IS NULL OR "
    "julianday(expires_at) >= julianday('now'))",
    "DROP TABLE g1_reserved_guard",
    "UPDATE idempotency_records SET state = 'expired', job_id = NULL, expires_at = NULL "
    "WHERE state = 'reserved'",
    "ALTER TABLE idempotency_records ADD COLUMN workflow_id TEXT NOT NULL DEFAULT ''",
    "UPDATE idempotency_records SET workflow_id = COALESCE("
    "(SELECT workflow_id FROM jobs_v2 WHERE jobs_v2.job_id = idempotency_records.job_id), '')",
    "DROP TABLE jobs",
    "ALTER TABLE jobs_v2 RENAME TO jobs",
    """
    ALTER TABLE artifacts ADD COLUMN mime_type TEXT NOT NULL
    DEFAULT 'application/octet-stream'
    CHECK(typeof(mime_type) = 'text' AND length(mime_type) > 0)
    """,
    """
    CREATE TRIGGER tr_jobs_execution_identity_immutable
    BEFORE UPDATE OF
        job_id, workflow_id, plan_id, revision_id, deployment_id, owner_id,
        retry_of, created_at, created_at_source, legacy_migrated, execution_origin
    ON jobs
    BEGIN
        SELECT RAISE(ABORT, 'job execution identity is immutable');
    END
    """,
    """
    CREATE TRIGGER tr_jobs_plan_binding_server_consistency
    BEFORE UPDATE OF plan_id, revision_id, deployment_id, workflow_id ON jobs
    WHEN NEW.plan_id IS NOT NULL AND (
        EXISTS (
            SELECT 1 FROM execution_attempts, execution_plans
            WHERE execution_attempts.job_id = NEW.job_id AND
                  execution_plans.plan_id = NEW.plan_id AND
                  execution_attempts.server_id != execution_plans.server_id
        ) OR EXISTS (
            SELECT 1 FROM artifacts, execution_plans
            WHERE artifacts.job_id = NEW.job_id AND
                  execution_plans.plan_id = NEW.plan_id AND
                  artifacts.server_id != execution_plans.server_id
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'job plan server conflicts with existing execution data');
    END
    """,
    """
    CREATE TRIGGER tr_execution_attempts_plan_server_insert
    BEFORE INSERT ON execution_attempts
    WHEN EXISTS (
        SELECT 1 FROM jobs JOIN execution_plans USING(plan_id)
        WHERE jobs.job_id = NEW.job_id AND execution_plans.server_id != NEW.server_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'attempt server must match execution plan');
    END
    """,
    """
    CREATE TRIGGER tr_artifacts_plan_server_insert
    BEFORE INSERT ON artifacts
    WHEN EXISTS (
        SELECT 1 FROM jobs JOIN execution_plans USING(plan_id)
        WHERE jobs.job_id = NEW.job_id AND execution_plans.server_id != NEW.server_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'artifact server must match execution plan');
    END
    """,
    "CREATE INDEX ix_jobs_owner_created ON jobs(owner_id, created_at DESC, job_id)",
    """
    CREATE UNIQUE INDEX uq_idempotency_records_resolved_job
    ON idempotency_records(job_id) WHERE state = 'resolved'
    """,
    "CREATE INDEX ix_jobs_owner_status_created ON jobs(owner_id, status, created_at DESC, job_id)",
    "CREATE INDEX ix_jobs_owner_workflow_created "
    "ON jobs(owner_id, workflow_id, created_at DESC, job_id)",
    "CREATE INDEX ix_jobs_plan_binding ON jobs(plan_id, workflow_id, revision_id, deployment_id)",
    "CREATE INDEX ix_jobs_retry_of ON jobs(retry_of) WHERE retry_of IS NOT NULL",
    """
    CREATE TRIGGER tr_idempotency_lease_shape_insert
    BEFORE INSERT ON idempotency_records
    WHEN NOT (
        (NEW.state IN ('reserved', 'submission_unknown') AND
         typeof(NEW.lease_token) = 'text' AND length(NEW.lease_token) = 64 AND
         NEW.lease_token NOT GLOB '*[^0-9a-f]*') OR
        (NEW.state = 'submission_unknown' AND NEW.lease_token IS NULL) OR
        (NEW.state NOT IN ('reserved', 'submission_unknown') AND NEW.lease_token IS NULL)
    )
    BEGIN
        SELECT RAISE(ABORT, 'idempotency lease shape is invalid');
    END
    """,
    """
    CREATE TRIGGER tr_idempotency_lease_shape_update
    BEFORE UPDATE OF state, lease_token ON idempotency_records
    WHEN NOT (
        (NEW.state IN ('reserved', 'submission_unknown') AND
         typeof(NEW.lease_token) = 'text' AND length(NEW.lease_token) = 64 AND
         NEW.lease_token NOT GLOB '*[^0-9a-f]*') OR
        (NEW.state = 'submission_unknown' AND NEW.lease_token IS NULL) OR
        (NEW.state NOT IN ('reserved', 'submission_unknown') AND NEW.lease_token IS NULL)
    )
    BEGIN
        SELECT RAISE(ABORT, 'idempotency lease shape is invalid');
    END
    """,
)

_G5_ORCHESTRATION_UP = (
    """
    CREATE TABLE operation_work_items (
        work_item_id TEXT NOT NULL PRIMARY KEY,
        subject_uri TEXT NOT NULL,
        work_type TEXT NOT NULL CHECK(length(work_type) > 0),
        payload_json TEXT NOT NULL,
        checkpoint_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed')),
        next_attempt_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(subject_uri, work_type),
        CHECK(typeof(work_item_id) = 'text' AND length(work_item_id) > 0),
        CHECK(typeof(subject_uri) = 'text' AND length(subject_uri) > 0)
    )
    """,
    """
    CREATE INDEX ix_operation_work_items_ready
    ON operation_work_items(status, next_attempt_at, created_at, work_item_id)
    """,
    """
    CREATE TABLE work_leases (
        work_item_id TEXT NOT NULL PRIMARY KEY
            REFERENCES operation_work_items(work_item_id) ON DELETE RESTRICT,
        worker_id TEXT NOT NULL CHECK(length(worker_id) > 0),
        fencing_token INTEGER NOT NULL CHECK(
            typeof(fencing_token) = 'integer' AND fencing_token > 0
        ),
        acquired_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX ix_work_leases_expiry ON work_leases(expires_at, work_item_id)",
    """
    CREATE TABLE server_generation_observations (
        server_id TEXT NOT NULL PRIMARY KEY,
        generation TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        CHECK(typeof(server_id) = 'text' AND length(server_id) > 0)
    )
    """,
)

_G5_IDENTITY_MERGE_UP = (
    "DROP TRIGGER IF EXISTS tr_execution_attempts_reconciliation_guard",
    """
    CREATE TRIGGER tr_execution_attempts_reconciliation_guard
    BEFORE UPDATE OF upstream_prompt_id, upstream_job_id, submission_state
    ON execution_attempts
    WHEN NOT (
        (
            OLD.submission_state = 'submission_unknown' AND
            OLD.upstream_prompt_id IS NULL AND OLD.upstream_job_id IS NULL AND
            NEW.submission_state = 'submitted' AND
            (NEW.upstream_prompt_id IS NOT NULL OR NEW.upstream_job_id IS NOT NULL)
        ) OR (
            OLD.submission_state = 'submitted' AND NEW.submission_state = 'submitted' AND
            (OLD.upstream_prompt_id IS NEW.upstream_prompt_id OR
             (OLD.upstream_prompt_id IS NULL AND NEW.upstream_prompt_id IS NOT NULL)) AND
            (OLD.upstream_job_id IS NEW.upstream_job_id OR
             (OLD.upstream_job_id IS NULL AND NEW.upstream_job_id IS NOT NULL)) AND
            (OLD.upstream_prompt_id IS NOT NEW.upstream_prompt_id OR
             OLD.upstream_job_id IS NOT NEW.upstream_job_id)
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'execution attempt reconciliation is append-once');
    END
    """,
)


_MIGRATIONS = (
    SchemaMigration(
        1,
        "initial-control-plane",
        _INITIAL_UP,
        _INITIAL_DOWN,
        bootstrap_sql=_SCHEMA_MIGRATIONS_SQL,
    ),
    SchemaMigration(
        2,
        "g1-job-asset-facts",
        _G1_SCHEMA_UP,
        (),
        feasibility_note="transactional forward-only migration before G1 aggregate cutover",
    ),
    SchemaMigration(
        3,
        "g5-event-orchestrator",
        _G5_ORCHESTRATION_UP,
        (),
        feasibility_note="transactional forward-only migration for durable G5 orchestration",
    ),
    SchemaMigration(
        4,
        "g5-upstream-identity-merge",
        _G5_IDENTITY_MERGE_UP,
        (),
        feasibility_note="forward-only append-safe upstream identity reconciliation",
    ),
)


class SQLiteControlPlaneStore:
    """Own the SQLite database and apply immutable schema migrations."""

    def __init__(self, path: str | Path) -> None:
        candidate = Path(path)
        if not candidate.is_absolute():
            raise ValueError("control-plane database path must be absolute")
        if any(part.exists() and part.is_symlink() for part in (candidate, *candidate.parents)):
            raise ValueError("control-plane database path must not contain symbolic links")
        if candidate.exists() and not candidate.is_file():
            raise ValueError("control-plane database path must be a regular file")
        self.path = candidate.resolve(strict=False)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _migration_map()
        connection = self._connect(require_wal=False)
        try:
            self.path.chmod(0o600)
        except OSError as exc:
            failure = SchemaMigrationError("cannot secure control-plane database permissions")
            _close_preserving_error(connection, failure)
            raise failure from exc
        try:
            journal_mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
            if journal_mode.lower() != "wal":
                raise SchemaMigrationError("SQLite WAL journal mode is required")
            connection.execute("PRAGMA foreign_keys = OFF")
            if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
                raise SchemaMigrationError("schema migration requires foreign keys to be disabled")
            connection.execute("BEGIN IMMEDIATE")
            bootstrap_sql = _MIGRATIONS[0].bootstrap_sql if _MIGRATIONS else ""
            if not bootstrap_sql:
                raise SchemaMigrationError("schema migration bootstrap is missing")
            connection.execute(bootstrap_sql)
            applied_rows = connection.execute(
                """
                SELECT version, name, checksum, up_supported, down_supported,
                       feasibility_note, schema_fingerprint
                FROM schema_migrations ORDER BY version
                """
            ).fetchall()
            if len(applied_rows) > len(_MIGRATIONS):
                raise SchemaMigrationError("database contains an unknown schema migration")
            for index, row in enumerate(applied_rows):
                migration = _MIGRATIONS[index]
                expected = (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    int(migration.up_supported),
                    int(migration.down_supported),
                    migration.feasibility_note,
                )
                if tuple(row)[:6] != expected:
                    raise SchemaMigrationError("schema migration checksum mismatch or history gap")
            if not applied_rows:
                if _schema_fingerprint(connection) != _bootstrap_fingerprint(bootstrap_sql):
                    raise SchemaMigrationError("schema fingerprint mismatch before first migration")
            if applied_rows:
                stored_fingerprints = {str(row["schema_fingerprint"]) for row in applied_rows}
                if stored_fingerprints != {_schema_fingerprint(connection)}:
                    raise SchemaMigrationError("schema fingerprint mismatch")
            for migration in _MIGRATIONS[len(applied_rows) :]:
                if not migration.up_supported:
                    raise SchemaMigrationError(
                        f"schema migration {migration.version} cannot be applied"
                    )
                for statement in migration.up:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO schema_migrations(
                        version, name, checksum, up_supported, down_supported,
                        feasibility_note, schema_fingerprint, applied_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        int(migration.up_supported),
                        int(migration.down_supported),
                        migration.feasibility_note,
                        "0" * 64,
                        _utc_now(),
                    ),
                )
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise SchemaMigrationError("schema migration produced foreign key violations")
            current_fingerprint = _schema_fingerprint(connection)
            connection.execute(
                "UPDATE schema_migrations SET schema_fingerprint = ?",
                (current_fingerprint,),
            )
            connection.commit()
            connection.execute("PRAGMA foreign_keys = ON")
            if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
                raise SchemaMigrationError("foreign keys could not be restored after migration")
        except BaseException as exc:
            _rollback_preserving_error(connection, exc)
            _close_preserving_error(connection, exc)
            raise
        else:
            connection.close()

    def unit_of_work(self) -> ControlPlaneUnitOfWork:
        """Create an unentered Unit of Work bound to this database."""
        from comfyui_mcp_skills.infrastructure.persistence.control_plane_uow import (
            SQLiteControlPlaneUnitOfWork,
        )

        return SQLiteControlPlaneUnitOfWork(self._connect)

    def rollback_schema(self, *, target_version: int) -> None:
        """Apply declared down migrations before any aggregate has switched."""
        if target_version < 0:
            raise ValueError("target_version must be non-negative")
        migrations_by_version = _migration_map()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            all_rows = connection.execute(
                """
                SELECT version, name, checksum, up_supported, down_supported,
                       feasibility_note, schema_fingerprint
                FROM schema_migrations ORDER BY version
                """
            ).fetchall()
            if len(all_rows) > len(_MIGRATIONS):
                raise SchemaMigrationError("database contains an unknown schema migration")
            for index, row in enumerate(all_rows):
                migration = _MIGRATIONS[index]
                expected = (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    int(migration.up_supported),
                    int(migration.down_supported),
                    migration.feasibility_note,
                )
                if tuple(row)[:6] != expected:
                    raise SchemaMigrationError("schema migration checksum mismatch or history gap")
            if all_rows:
                stored_fingerprints = {str(row["schema_fingerprint"]) for row in all_rows}
                if stored_fingerprints != {_schema_fingerprint(connection)}:
                    raise SchemaMigrationError("schema fingerprint mismatch")
            applied = [row for row in reversed(all_rows) if int(row["version"]) > target_version]
            if (
                applied
                and connection.execute(
                    "SELECT 1 FROM store_migrations WHERE switched_at IS NOT NULL LIMIT 1"
                ).fetchone()
            ):
                raise SchemaMigrationError(
                    "schema rollback is forbidden after an aggregate has switched"
                )
            for row in applied:
                migration = migrations_by_version[int(row["version"])]
                if not migration.down_supported:
                    raise SchemaMigrationError(
                        f"schema migration {row['version']} cannot be rolled back"
                    )
                for statement in migration.down:
                    connection.execute(statement)
                connection.execute(
                    "DELETE FROM schema_migrations WHERE version = ?",
                    (migration.version,),
                )
            if all_rows and len(applied) < len(all_rows):
                connection.execute(
                    "UPDATE schema_migrations SET schema_fingerprint = ?",
                    (_schema_fingerprint(connection),),
                )
            connection.commit()
        except BaseException as exc:
            _rollback_preserving_error(connection, exc)
            _close_preserving_error(connection, exc)
            raise
        else:
            connection.close()

    def _connect(self, *, require_wal: bool = True) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA trusted_schema = OFF")
            settings = (
                int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
                int(connection.execute("PRAGMA busy_timeout").fetchone()[0]),
                int(connection.execute("PRAGMA synchronous").fetchone()[0]),
                int(connection.execute("PRAGMA trusted_schema").fetchone()[0]),
            )
            if settings != (1, 5000, 2, 0):
                raise SchemaMigrationError("SQLite connection safety settings are unavailable")
            if require_wal:
                journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                if journal_mode != "wal":
                    raise SchemaMigrationError("SQLite WAL journal mode is required")
            return connection
        except BaseException as exc:
            _close_preserving_error(connection, exc)
            raise


def _migration_map() -> dict[int, SchemaMigration]:
    versions = [migration.version for migration in _MIGRATIONS]
    if any(version <= 0 for version in versions) or versions != sorted(set(versions)):
        raise SchemaMigrationError(
            "schema migration versions must be positive, unique, and strictly increasing"
        )
    return {migration.version: migration for migration in _MIGRATIONS}


def _bootstrap_fingerprint(bootstrap_sql: str) -> str:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(bootstrap_sql)
        return _schema_fingerprint(connection)
    finally:
        connection.close()


def _schema_fingerprint(connection: sqlite3.Connection) -> str:
    objects = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    payload = json.dumps(
        [tuple(row) for row in objects],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _rollback_preserving_error(connection: sqlite3.Connection, original: BaseException) -> None:
    try:
        connection.rollback()
    except BaseException as cleanup_error:
        _add_cleanup_note(original, "rollback", cleanup_error)


def _close_preserving_error(connection: sqlite3.Connection, original: BaseException) -> None:
    try:
        connection.close()
    except BaseException as cleanup_error:
        _add_cleanup_note(original, "connection close", cleanup_error)


def _add_cleanup_note(
    original: BaseException, operation: str, cleanup_error: BaseException
) -> None:
    add_note = getattr(original, "add_note", None)
    if add_note is not None:
        add_note(f"{operation} also failed: {cleanup_error!r}")
        return
    cleanup_error.__context__ = original.__context__
    original.__context__ = cleanup_error


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
