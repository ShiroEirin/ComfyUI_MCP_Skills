"""SQLite schema and transaction boundary for the agent-native control plane."""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from comfyui_mcp_skills.infrastructure.persistence.authoring_hardening_schema import (
    PHASE_J_HARDENING_UP,
)
from comfyui_mcp_skills.infrastructure.persistence.provisioning_schema import (
    PHASE_O_PROVISIONING_UP,
)
from comfyui_mcp_skills.infrastructure.persistence.routing_commit_schema import (
    PHASE_Q_ROUTING_COMMIT_UP,
)
from comfyui_mcp_skills.infrastructure.persistence.routing_schema import PHASE_K_ROUTING_UP

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


_PHASE_J_WORKFLOW_CHANGE_UP = (
    f"""
    CREATE TABLE workflow_change_plans (
        plan_id TEXT NOT NULL PRIMARY KEY CHECK({_typed_id_check("plan_id", "plan_")}),
        workflow_id TEXT NOT NULL,
        server_id TEXT NOT NULL CHECK({_safe_identifier_check("server_id")}),
        base_revision_id TEXT NOT NULL,
        operations_json TEXT NOT NULL,
        graph_json TEXT NOT NULL,
        parameter_schema_json TEXT NOT NULL,
        dependency_contract_json TEXT NOT NULL,
        content_digest TEXT NOT NULL CHECK({_sha256_check("content_digest")}),
        plan_digest TEXT NOT NULL UNIQUE CHECK({_sha256_check("plan_digest")}),
        diff_json TEXT NOT NULL,
        actor TEXT NOT NULL CHECK(length(actor) BETWEEN 1 AND 128),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        committed_revision_id TEXT,
        FOREIGN KEY(workflow_id, base_revision_id)
            REFERENCES workflow_revisions(workflow_id, revision_id) ON DELETE RESTRICT,
        FOREIGN KEY(workflow_id, committed_revision_id)
            REFERENCES workflow_revisions(workflow_id, revision_id) ON DELETE RESTRICT,
        CHECK(committed_revision_id IS NULL OR typeof(committed_revision_id) = 'text')
    )
    """,
    """
    CREATE INDEX ix_workflow_change_plans_expiry
    ON workflow_change_plans(expires_at, plan_id) WHERE committed_revision_id IS NULL
    """,
    f"""
    CREATE TABLE workflow_rollback_requests (
        actor TEXT NOT NULL CHECK(length(actor) BETWEEN 1 AND 128),
        request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 256),
        request_digest TEXT NOT NULL CHECK({_sha256_check("request_digest")}),
        workflow_id TEXT NOT NULL,
        server_id TEXT NOT NULL CHECK({_safe_identifier_check("server_id")}),
        target_revision_id TEXT NOT NULL,
        replaced_revision_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        deployment_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(actor, request_id),
        FOREIGN KEY(workflow_id, target_revision_id)
            REFERENCES workflow_revisions(workflow_id, revision_id) ON DELETE RESTRICT,
        FOREIGN KEY(workflow_id, replaced_revision_id)
            REFERENCES workflow_revisions(workflow_id, revision_id) ON DELETE RESTRICT,
        FOREIGN KEY(deployment_id, workflow_id, revision_id, server_id)
            REFERENCES workflow_deployments(
                deployment_id, workflow_id, revision_id, server_id
            ) ON DELETE RESTRICT
    )
    """,
)

_PHASE_L_ASSET_LIBRARY_UP = (
    "ALTER TABLE assets ADD COLUMN deleted_at TEXT",
    "CREATE UNIQUE INDEX uq_assets_owner_asset ON assets(owner_id, asset_id)",
    "CREATE UNIQUE INDEX uq_artifacts_artifact_job ON artifacts(artifact_id, job_id)",
    "CREATE UNIQUE INDEX uq_execution_plans_plan_revision_deployment ON execution_plans(plan_id, revision_id, deployment_id)",
    "CREATE INDEX ix_assets_owner_active_created ON assets(owner_id, created_at DESC, asset_id DESC) WHERE deleted_at IS NULL",
    "CREATE INDEX ix_assets_owner_media_created ON assets(owner_id, media_type, created_at DESC, asset_id DESC) WHERE deleted_at IS NULL",
    "CREATE INDEX ix_artifacts_job_created ON artifacts(job_id, created_at DESC, artifact_id DESC)",
    """
    CREATE TABLE asset_collections (
        owner_id TEXT NOT NULL,
        collection TEXT NOT NULL CHECK(length(collection) BETWEEN 1 AND 128),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(owner_id, collection)
    )
    """,
    """
    CREATE TABLE asset_collection_members (
        owner_id TEXT NOT NULL,
        collection TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(owner_id, collection, asset_id),
        FOREIGN KEY(owner_id, collection)
            REFERENCES asset_collections(owner_id, collection) ON DELETE CASCADE,
        FOREIGN KEY(owner_id, asset_id)
            REFERENCES assets(owner_id, asset_id) ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX ix_asset_collection_members_asset ON asset_collection_members(owner_id,asset_id,collection)",
    """
    CREATE TABLE execution_plan_owners (
        plan_id TEXT NOT NULL PRIMARY KEY, owner_id TEXT NOT NULL,
        revision_id TEXT NOT NULL, deployment_id TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(plan_id, owner_id, revision_id, deployment_id),
        FOREIGN KEY(plan_id, revision_id, deployment_id)
            REFERENCES execution_plans(plan_id, revision_id, deployment_id) ON DELETE RESTRICT,
        CHECK(typeof(owner_id) = 'text' AND length(owner_id) > 0)
    )
    """,
    """INSERT INTO execution_plan_owners
       SELECT plans.plan_id,MIN(jobs.owner_id),plans.revision_id,plans.deployment_id,
              plans.created_at
       FROM execution_plans AS plans JOIN jobs ON jobs.plan_id=plans.plan_id
       GROUP BY plans.plan_id,plans.revision_id,plans.deployment_id,plans.created_at
       HAVING count(DISTINCT jobs.owner_id)=1 AND min(length(jobs.owner_id))>0""",
    """
    CREATE TRIGGER tr_jobs_phase_l_plan_owner_insert AFTER INSERT ON jobs WHEN NEW.plan_id IS NOT NULL
    BEGIN
        INSERT INTO execution_plan_owners VALUES(NEW.plan_id,NEW.owner_id,NEW.revision_id,NEW.deployment_id,NEW.created_at)
        ON CONFLICT(plan_id) DO NOTHING;
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM execution_plan_owners
            WHERE plan_id=NEW.plan_id AND owner_id=NEW.owner_id AND revision_id=NEW.revision_id
              AND deployment_id=NEW.deployment_id)
            THEN RAISE(ABORT, 'execution plan owner mismatch') END;
    END
    """,
    "CREATE TRIGGER tr_execution_plan_owners_immutable_update BEFORE UPDATE ON execution_plan_owners BEGIN SELECT RAISE(ABORT, 'execution plan owner is immutable'); END",
    "CREATE TRIGGER tr_execution_plan_owners_immutable_delete BEFORE DELETE ON execution_plan_owners BEGIN SELECT RAISE(ABORT, 'execution plan owner is immutable'); END",
    f"""
    CREATE TABLE execution_plan_inputs (
        plan_id TEXT NOT NULL, owner_id TEXT NOT NULL, revision_id TEXT NOT NULL,
        deployment_id TEXT NOT NULL,
        parameter_name TEXT NOT NULL CHECK({_safe_identifier_check("parameter_name")}),
        consumer_node_id TEXT NOT NULL CHECK({_safe_identifier_check("consumer_node_id")}),
        consumer_input_name TEXT NOT NULL CHECK({_safe_identifier_check("consumer_input_name")}),
        consumer_class TEXT NOT NULL CHECK({_safe_identifier_check("consumer_class")}),
        source_kind TEXT NOT NULL CHECK(source_kind IN ('asset','artifact')),
        asset_id TEXT, artifact_id TEXT, source_job_id TEXT,
        reuse_strategy TEXT NOT NULL CHECK(reuse_strategy IN ('direct','copy','upload')),
        source_digest TEXT NOT NULL CHECK({_sha256_check("source_digest")}), created_at TEXT NOT NULL,
        PRIMARY KEY(plan_id,parameter_name),
        FOREIGN KEY(plan_id,owner_id,revision_id,deployment_id)
            REFERENCES execution_plan_owners(plan_id,owner_id,revision_id,deployment_id) ON DELETE RESTRICT,
        FOREIGN KEY(owner_id,asset_id) REFERENCES assets(owner_id,asset_id) ON DELETE RESTRICT,
        FOREIGN KEY(artifact_id,source_job_id) REFERENCES artifacts(artifact_id,job_id) ON DELETE RESTRICT,
        FOREIGN KEY(source_job_id,owner_id) REFERENCES jobs(job_id,owner_id) ON DELETE RESTRICT,
        CHECK((source_kind='asset' AND asset_id IS NOT NULL AND artifact_id IS NULL AND source_job_id IS NULL) OR
              (source_kind='artifact' AND asset_id IS NULL AND artifact_id IS NOT NULL AND source_job_id IS NOT NULL))
    )
    """,
    "CREATE INDEX ix_execution_plan_inputs_owner_asset ON execution_plan_inputs(owner_id,asset_id,plan_id,parameter_name) WHERE asset_id IS NOT NULL",
    "CREATE INDEX ix_execution_plan_inputs_owner_artifact ON execution_plan_inputs(owner_id,artifact_id,plan_id,parameter_name) WHERE artifact_id IS NOT NULL",
    "CREATE INDEX ix_execution_plan_inputs_owner_plan ON execution_plan_inputs(owner_id,plan_id,parameter_name)",
    "CREATE TRIGGER tr_execution_plan_inputs_immutable_update BEFORE UPDATE ON execution_plan_inputs BEGIN SELECT RAISE(ABORT, 'execution plan input is immutable'); END",
    "CREATE TRIGGER tr_execution_plan_inputs_immutable_delete BEFORE DELETE ON execution_plan_inputs BEGIN SELECT RAISE(ABORT, 'execution plan input is immutable'); END",
    """
    CREATE TABLE phase_l_backfill_state (
        backfill_name TEXT NOT NULL PRIMARY KEY CHECK(backfill_name IN ('artifact_outputs','execution_plan_inputs')),
        status TEXT NOT NULL CHECK(status IN ('pending','running','complete','failed')),
        incomplete_count INTEGER NOT NULL CHECK(typeof(incomplete_count)='integer' AND incomplete_count>=0),
        detected_at TEXT NOT NULL, completed_at TEXT, failure_code TEXT,
        CHECK((status='complete' AND incomplete_count=0 AND completed_at IS NOT NULL AND failure_code IS NULL) OR
              (status='failed' AND incomplete_count>0 AND completed_at IS NULL AND failure_code IS NOT NULL) OR
              (status IN ('pending','running') AND incomplete_count>0 AND completed_at IS NULL AND failure_code IS NULL))
    )
    """,
    """
    INSERT INTO phase_l_backfill_state
    SELECT 'artifact_outputs',CASE WHEN count(*)=0 THEN 'complete' ELSE 'pending' END,count(*),
           strftime('%Y-%m-%dT%H:%M:%fZ','now'),CASE WHEN count(*)=0 THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') END,NULL
    FROM jobs WHERE trim(outputs_json) NOT IN ('','[]','{}','null')
    """,
    """
    INSERT INTO phase_l_backfill_state
    SELECT 'execution_plan_inputs',CASE WHEN count(*)=0 THEN 'complete' ELSE 'pending' END,count(*),
           strftime('%Y-%m-%dT%H:%M:%fZ','now'),CASE WHEN count(*)=0 THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') END,NULL
    FROM execution_plans
    """,
    f"""
    CREATE TABLE job_artifact_collections (
        job_id TEXT NOT NULL PRIMARY KEY REFERENCES jobs(job_id) ON DELETE RESTRICT,
        status TEXT NOT NULL CHECK(status IN ('needs_backfill','complete','failed')),
        artifact_count INTEGER NOT NULL CHECK(typeof(artifact_count)='integer' AND artifact_count>=0),
        output_snapshot_digest TEXT CHECK(output_snapshot_digest IS NULL OR {_sha256_check("output_snapshot_digest")}),
        error_code TEXT, updated_at TEXT NOT NULL,
        CHECK((status='complete' AND error_code IS NULL) OR (status='failed' AND error_code IS NOT NULL) OR
              (status='needs_backfill' AND error_code IS NULL))
    )
    """,
    """
    INSERT INTO job_artifact_collections
    SELECT job_id,CASE WHEN trim(outputs_json) IN ('','[]','{}','null') THEN 'complete' ELSE 'needs_backfill' END,
           (SELECT count(*) FROM artifacts WHERE artifacts.job_id=jobs.job_id),NULL,NULL,
           strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM jobs
    """,
    """
    CREATE TRIGGER tr_jobs_phase_l_collection_insert AFTER INSERT ON jobs
    BEGIN
        INSERT INTO job_artifact_collections VALUES(
            NEW.job_id,CASE WHEN trim(NEW.outputs_json) IN ('','[]','{}','null') THEN 'complete' ELSE 'needs_backfill' END,
            0,NULL,NULL,NEW.created_at);
    END
    """,
    f"""
    CREATE TABLE artifact_completeness (
        artifact_id TEXT NOT NULL PRIMARY KEY REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
        completeness TEXT NOT NULL CHECK(completeness IN ('locator_only','verified')),
        mime_type TEXT, size_bytes INTEGER, sha256 TEXT, legacy_index INTEGER, observed_at TEXT NOT NULL,
        CHECK(mime_type IS NULL OR (typeof(mime_type)='text' AND length(mime_type)>0)),
        CHECK(size_bytes IS NULL OR (typeof(size_bytes)='integer' AND size_bytes>=0)),
        CHECK(sha256 IS NULL OR {_sha256_check("sha256")}),
        CHECK(legacy_index IS NULL OR (typeof(legacy_index)='integer' AND legacy_index BETWEEN 0 AND 2147483647)),
        CHECK((completeness='locator_only' AND size_bytes IS NULL AND sha256 IS NULL) OR
              (completeness='verified' AND size_bytes IS NOT NULL AND sha256 IS NOT NULL))
    )
    """,
    f"""
    CREATE TABLE media_locations (
        location_id TEXT NOT NULL PRIMARY KEY CHECK(
            typeof(location_id)='text' AND length(location_id) BETWEEN 1 AND 256 AND
            instr(location_id,'/')=0 AND instr(location_id,char(92))=0 AND
            instr(location_id,char(0))=0
        ),
        owner_id TEXT NOT NULL, asset_id TEXT, artifact_id TEXT, source_job_id TEXT,
        server_id TEXT NOT NULL CHECK({_safe_identifier_check("server_id")}),
        filename TEXT NOT NULL CHECK(length(filename)>0), subfolder TEXT NOT NULL,
        storage_type TEXT NOT NULL CHECK(storage_type IN ('input','output')),
        state TEXT NOT NULL CHECK(state IN ('available','archived','deleted')),
        size_bytes INTEGER, sha256 TEXT, mime_type TEXT NOT NULL CHECK(length(mime_type)>0),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT, deleted_at TEXT,
        FOREIGN KEY(owner_id,asset_id) REFERENCES assets(owner_id,asset_id) ON DELETE RESTRICT,
        FOREIGN KEY(artifact_id,source_job_id) REFERENCES artifacts(artifact_id,job_id) ON DELETE RESTRICT,
        FOREIGN KEY(source_job_id,owner_id) REFERENCES jobs(job_id,owner_id) ON DELETE RESTRICT,
        CHECK(size_bytes IS NULL OR (typeof(size_bytes)='integer' AND size_bytes>=0)),
        CHECK(sha256 IS NULL OR {_sha256_check("sha256")}),
        CHECK((asset_id IS NOT NULL AND artifact_id IS NULL AND source_job_id IS NULL) OR
              (asset_id IS NULL AND artifact_id IS NOT NULL AND source_job_id IS NOT NULL)),
        CHECK((state='available' AND archived_at IS NULL AND deleted_at IS NULL) OR
              (state='archived' AND archived_at IS NOT NULL AND deleted_at IS NULL) OR
              (state='deleted' AND deleted_at IS NOT NULL))
    )
    """,
    "CREATE INDEX ix_media_locations_owner_asset ON media_locations(owner_id,asset_id,state,location_id) WHERE asset_id IS NOT NULL",
    "CREATE INDEX ix_media_locations_owner_artifact ON media_locations(owner_id,artifact_id,state,location_id) WHERE artifact_id IS NOT NULL",
    "CREATE INDEX ix_media_locations_available_server ON media_locations(owner_id,server_id,storage_type,location_id) WHERE state='available'",
    "CREATE INDEX ix_media_locations_archive_cleanup ON media_locations(state,updated_at,location_id)",
    """
    INSERT INTO media_locations
    SELECT 'asset:'||asset_id,owner_id,asset_id,NULL,NULL,server_id,name,subfolder,'input','available',
           size_bytes,sha256,mime_type,created_at,created_at,NULL,NULL FROM assets
    """,
    """
    INSERT INTO media_locations
    SELECT 'artifact:'||artifacts.artifact_id,jobs.owner_id,NULL,artifacts.artifact_id,artifacts.job_id,
           artifacts.server_id,artifacts.filename,artifacts.subfolder,artifacts.storage_type,'available',
           NULL,NULL,artifacts.mime_type,artifacts.created_at,artifacts.created_at,NULL,NULL
    FROM artifacts JOIN jobs ON jobs.job_id=artifacts.job_id
    """,
    f"""
    CREATE TABLE asset_metadata_extractions (
        asset_id TEXT NOT NULL PRIMARY KEY REFERENCES assets(asset_id) ON DELETE RESTRICT,
        owner_id TEXT NOT NULL, source_sha256 TEXT NOT NULL CHECK({_sha256_check("source_sha256")}),
        format TEXT NOT NULL CHECK(format IN ('png','unsupported')), projection_json TEXT NOT NULL,
        revision_id TEXT REFERENCES workflow_revisions(revision_id) ON DELETE RESTRICT, extracted_at TEXT NOT NULL,
        FOREIGN KEY(owner_id,asset_id) REFERENCES assets(owner_id,asset_id) ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX ix_asset_metadata_owner ON asset_metadata_extractions(owner_id,extracted_at DESC,asset_id)",
    """
    CREATE TABLE asset_artifact_lineage (
        asset_id TEXT NOT NULL PRIMARY KEY REFERENCES assets(asset_id) ON DELETE RESTRICT,
        owner_id TEXT NOT NULL, source_artifact_id TEXT NOT NULL, source_job_id TEXT NOT NULL,
        relationship TEXT NOT NULL CHECK(relationship IN ('import','transfer')), created_at TEXT NOT NULL,
        FOREIGN KEY(owner_id,asset_id) REFERENCES assets(owner_id,asset_id) ON DELETE RESTRICT,
        FOREIGN KEY(source_artifact_id,source_job_id) REFERENCES artifacts(artifact_id,job_id) ON DELETE RESTRICT,
        FOREIGN KEY(source_job_id,owner_id) REFERENCES jobs(job_id,owner_id) ON DELETE RESTRICT,
        UNIQUE(owner_id,source_artifact_id,asset_id)
    )
    """,
    "CREATE INDEX ix_asset_lineage_source ON asset_artifact_lineage(owner_id,source_artifact_id,created_at,asset_id)",
    f"""
    CREATE TABLE asset_delete_plans (
        plan_id TEXT NOT NULL PRIMARY KEY CHECK({_typed_id_check("plan_id", "plan_")}),
        owner_id TEXT NOT NULL, asset_id TEXT NOT NULL,
        plan_digest TEXT NOT NULL CHECK({_sha256_check("plan_digest")}),
        asset_identity_digest TEXT NOT NULL CHECK({_sha256_check("asset_identity_digest")}),
        impact_digest TEXT NOT NULL CHECK({_sha256_check("impact_digest")}), impact_json TEXT NOT NULL,
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL, committed_at TEXT,
        FOREIGN KEY(owner_id,asset_id) REFERENCES assets(owner_id,asset_id) ON DELETE RESTRICT,
        UNIQUE(plan_id,owner_id,asset_id)
    )
    """,
    "CREATE INDEX ix_asset_delete_plans_owner ON asset_delete_plans(owner_id,created_at DESC,plan_id)",
    "CREATE INDEX ix_asset_delete_plans_expiry ON asset_delete_plans(expires_at,plan_id) WHERE committed_at IS NULL",
    f"""
    CREATE TABLE artifact_transfers (
        transfer_id TEXT NOT NULL PRIMARY KEY CHECK({_typed_id_check("transfer_id", "transfer_")}),
        owner_id TEXT NOT NULL, artifact_id TEXT NOT NULL, source_job_id TEXT NOT NULL,
        target_server_id TEXT NOT NULL CHECK({_safe_identifier_check("target_server_id")}),
        target_asset_id TEXT NOT NULL CHECK({_typed_id_check("target_asset_id", "asset_")}),
        operation TEXT NOT NULL CHECK(operation IN ('import','transfer')),
        strategy TEXT NOT NULL CHECK(strategy IN ('copy','upload')),
        state TEXT NOT NULL CHECK(state IN ('planned','transferring','completed','failed')),
        plan_digest TEXT NOT NULL CHECK({_sha256_check("plan_digest")}),
        artifact_identity_digest TEXT NOT NULL CHECK({_sha256_check("artifact_identity_digest")}),
        planned_size_bytes INTEGER NOT NULL CHECK(typeof(planned_size_bytes)='integer' AND planned_size_bytes>=0),
        planned_sha256 TEXT NOT NULL CHECK({_sha256_check("planned_sha256")}),
        planned_mime_type TEXT NOT NULL CHECK(length(planned_mime_type)>0),
        network_policy_json TEXT NOT NULL,
        temporary_policy TEXT NOT NULL CHECK({_safe_identifier_check("temporary_policy")}),
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        lease_token TEXT, lease_expires_at TEXT,
        lease_fence INTEGER NOT NULL DEFAULT 0 CHECK(typeof(lease_fence)='integer' AND lease_fence>=0),
        completed_at TEXT, result_asset_id TEXT, result_size_bytes INTEGER,
        result_sha256 TEXT,
        failure_code TEXT CHECK(failure_code IS NULL OR {_safe_identifier_check("failure_code")}),
        FOREIGN KEY(artifact_id,source_job_id) REFERENCES artifacts(artifact_id,job_id) ON DELETE RESTRICT,
        FOREIGN KEY(source_job_id,owner_id) REFERENCES jobs(job_id,owner_id) ON DELETE RESTRICT,
        FOREIGN KEY(owner_id,result_asset_id) REFERENCES assets(owner_id,asset_id) ON DELETE RESTRICT,
        CHECK(lease_token IS NULL OR (
            typeof(lease_token)='text' AND length(lease_token) BETWEEN 32 AND 256
        )),
        CHECK(result_size_bytes IS NULL OR (typeof(result_size_bytes)='integer' AND result_size_bytes>=0)),
        CHECK(result_sha256 IS NULL OR {_sha256_check("result_sha256")}),
        CHECK((state='planned' AND completed_at IS NULL AND failure_code IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL) OR
              (state='transferring' AND completed_at IS NULL AND failure_code IS NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR
              (state='completed' AND completed_at IS NOT NULL AND failure_code IS NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR
              (state='failed' AND completed_at IS NULL AND failure_code IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)),
        CHECK((state='completed' AND result_asset_id=target_asset_id AND result_size_bytes IS NOT NULL AND result_sha256 IS NOT NULL) OR
              (state!='completed' AND result_asset_id IS NULL AND result_size_bytes IS NULL AND result_sha256 IS NULL))
    )
    """,
    "CREATE UNIQUE INDEX uq_artifact_transfer_plan ON artifact_transfers(owner_id,artifact_id,target_server_id,plan_digest)",
    "CREATE INDEX ix_artifact_transfers_owner ON artifact_transfers(owner_id,created_at DESC,transfer_id DESC)",
    "CREATE INDEX ix_artifact_transfers_claimable ON artifact_transfers(state,lease_expires_at,expires_at,transfer_id)",
    "CREATE INDEX ix_artifact_transfers_result_owner ON artifact_transfers(owner_id,result_asset_id) WHERE result_asset_id IS NOT NULL",
    """
    CREATE TRIGGER tr_artifact_transfers_identity_update BEFORE UPDATE OF
        transfer_id,owner_id,artifact_id,source_job_id,target_server_id,target_asset_id,
        operation,strategy,plan_digest,artifact_identity_digest,planned_size_bytes,planned_sha256,
        planned_mime_type,network_policy_json,temporary_policy,created_at,expires_at ON artifact_transfers
    BEGIN SELECT RAISE(ABORT, 'artifact transfer identity is immutable'); END
    """,
    f"""
    CREATE TABLE media_retention_bindings (
        binding_id TEXT NOT NULL PRIMARY KEY CHECK({_safe_identifier_check("binding_id")}),
        owner_id TEXT NOT NULL,
        asset_id TEXT, artifact_id TEXT, source_job_id TEXT,
        archive_at TEXT, delete_at TEXT, retain_until TEXT,
        legal_hold INTEGER NOT NULL DEFAULT 0 CHECK(legal_hold IN (0,1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        FOREIGN KEY(owner_id,asset_id) REFERENCES assets(owner_id,asset_id) ON DELETE RESTRICT,
        FOREIGN KEY(artifact_id,source_job_id) REFERENCES artifacts(artifact_id,job_id) ON DELETE RESTRICT,
        FOREIGN KEY(source_job_id,owner_id) REFERENCES jobs(job_id,owner_id) ON DELETE RESTRICT,
        CHECK((asset_id IS NOT NULL AND artifact_id IS NULL AND source_job_id IS NULL) OR
              (asset_id IS NULL AND artifact_id IS NOT NULL AND source_job_id IS NOT NULL)),
        CHECK(delete_at IS NULL OR archive_at IS NULL OR delete_at>=archive_at)
    )
    """,
    "CREATE UNIQUE INDEX uq_media_retention_asset ON media_retention_bindings(owner_id,asset_id) WHERE asset_id IS NOT NULL",
    "CREATE UNIQUE INDEX uq_media_retention_artifact ON media_retention_bindings(owner_id,artifact_id) WHERE artifact_id IS NOT NULL",
    "CREATE INDEX ix_media_retention_archive_due ON media_retention_bindings(archive_at,binding_id) WHERE archive_at IS NOT NULL AND legal_hold=0",
    "CREATE INDEX ix_media_retention_delete_due ON media_retention_bindings(delete_at,binding_id) WHERE delete_at IS NOT NULL AND legal_hold=0",
    """
    CREATE TRIGGER tr_assets_identity_update BEFORE UPDATE OF
        asset_id,owner_id,server_id,name,subfolder,media_type,mime_type,size_bytes,
        sha256,source_type,comfyui_ref,created_at ON assets
    BEGIN SELECT RAISE(ABORT, 'asset identity is immutable'); END
    """,
    """
    CREATE TRIGGER tr_assets_media_location_insert AFTER INSERT ON assets
    BEGIN
        INSERT INTO media_locations(
            location_id,owner_id,asset_id,artifact_id,source_job_id,server_id,
            filename,subfolder,storage_type,state,size_bytes,sha256,mime_type,
            created_at,updated_at,archived_at,deleted_at
        ) VALUES(
            'asset:'||NEW.asset_id,NEW.owner_id,NEW.asset_id,NULL,NULL,NEW.server_id,
            NEW.name,NEW.subfolder,'input','available',NEW.size_bytes,NEW.sha256,
            NEW.mime_type,NEW.created_at,NEW.created_at,NULL,NULL
        );
    END
    """,
    """
    CREATE TRIGGER tr_artifacts_media_location_insert AFTER INSERT ON artifacts
    BEGIN
        INSERT INTO media_locations(
            location_id,owner_id,asset_id,artifact_id,source_job_id,server_id,
            filename,subfolder,storage_type,state,size_bytes,sha256,mime_type,
            created_at,updated_at,archived_at,deleted_at
        ) SELECT
            'artifact:'||NEW.artifact_id,jobs.owner_id,NULL,NEW.artifact_id,NEW.job_id,
            NEW.server_id,NEW.filename,NEW.subfolder,NEW.storage_type,'available',
            NULL,NULL,NEW.mime_type,NEW.created_at,NEW.created_at,NULL,NULL
        FROM jobs WHERE jobs.job_id=NEW.job_id;
    END
    """,
    "CREATE TRIGGER tr_asset_lineage_immutable_update BEFORE UPDATE ON asset_artifact_lineage BEGIN SELECT RAISE(ABORT, 'asset lineage is immutable'); END",
    "CREATE TRIGGER tr_asset_lineage_immutable_delete BEFORE DELETE ON asset_artifact_lineage BEGIN SELECT RAISE(ABORT, 'asset lineage is immutable'); END",
    "CREATE TRIGGER tr_assets_immutable_delete BEFORE DELETE ON assets BEGIN SELECT RAISE(ABORT, 'asset identity is immutable'); END",
    "CREATE TRIGGER tr_artifact_transfers_immutable_delete BEFORE DELETE ON artifact_transfers BEGIN SELECT RAISE(ABORT, 'artifact transfer identity is immutable'); END",
    """
    CREATE TRIGGER tr_media_locations_identity_update BEFORE UPDATE OF
        location_id,owner_id,asset_id,artifact_id,source_job_id,server_id,
        filename,subfolder,storage_type,created_at ON media_locations
    BEGIN SELECT RAISE(ABORT, 'media location identity is immutable'); END
    """,
    "CREATE TRIGGER tr_media_locations_immutable_delete BEFORE DELETE ON media_locations BEGIN SELECT RAISE(ABORT, 'media location identity is immutable'); END",
    """
    CREATE TRIGGER tr_asset_delete_plans_identity_update BEFORE UPDATE OF
        plan_id,owner_id,asset_id,plan_digest,asset_identity_digest,impact_digest,
        impact_json,created_at,expires_at ON asset_delete_plans
    BEGIN SELECT RAISE(ABORT, 'asset delete plan identity is immutable'); END
    """,
    "CREATE TRIGGER tr_asset_delete_plans_immutable_delete BEFORE DELETE ON asset_delete_plans BEGIN SELECT RAISE(ABORT, 'asset delete plan identity is immutable'); END",
    """
    CREATE TRIGGER tr_media_retention_binding_identity_update BEFORE UPDATE OF
        binding_id,owner_id,asset_id,artifact_id,source_job_id,created_at
    ON media_retention_bindings
    BEGIN SELECT RAISE(ABORT, 'media retention binding identity is immutable'); END
    """,
    "CREATE TRIGGER tr_media_retention_bindings_immutable_delete BEFORE DELETE ON media_retention_bindings BEGIN SELECT RAISE(ABORT, 'media retention binding identity is immutable'); END",
)

_PHASE_M_EXPERIMENT_AGGREGATES_UP = (
    f"ALTER TABLE execution_plans ADD COLUMN raw_arguments_digest TEXT CHECK(raw_arguments_digest IS NULL OR {_sha256_check('raw_arguments_digest')})",
    "ALTER TABLE jobs ADD COLUMN server_id TEXT NOT NULL DEFAULT 'local' CHECK(typeof(server_id)='text' AND length(server_id) BETWEEN 1 AND 128)",
    "CREATE TRIGGER tr_jobs_server_backfill_guard BEFORE UPDATE OF server_id ON jobs WHEN (SELECT count(DISTINCT server_id) FROM execution_attempts WHERE job_id=OLD.job_id)>1 OR (OLD.plan_id IS NOT NULL AND EXISTS (SELECT 1 FROM execution_attempts JOIN execution_plans ON execution_plans.plan_id=OLD.plan_id WHERE execution_attempts.job_id=OLD.job_id AND execution_attempts.server_id!=execution_plans.server_id)) BEGIN SELECT RAISE(ABORT,'job server backfill is ambiguous'); END",
    "UPDATE jobs SET server_id=COALESCE((SELECT execution_plans.server_id FROM execution_plans WHERE execution_plans.plan_id=jobs.plan_id),(SELECT MIN(execution_attempts.server_id) FROM execution_attempts WHERE execution_attempts.job_id=jobs.job_id),'local')",
    "DROP TRIGGER tr_jobs_server_backfill_guard",
    "CREATE UNIQUE INDEX uq_jobs_owner_workflow_server ON jobs(job_id,owner_id,workflow_id,server_id)",
    "CREATE TRIGGER tr_jobs_server_identity_immutable BEFORE UPDATE OF server_id ON jobs BEGIN SELECT RAISE(ABORT,'job server identity is immutable'); END",
    "CREATE TABLE experiment_server_capacities (server_id TEXT NOT NULL PRIMARY KEY CHECK(typeof(server_id)='text' AND length(server_id) BETWEEN 1 AND 128), execution_slots INTEGER NOT NULL DEFAULT 1 CHECK(typeof(execution_slots)='integer' AND execution_slots BETWEEN 1 AND 64), subject_submission_quota INTEGER NOT NULL DEFAULT 0 CHECK(typeof(subject_submission_quota)='integer' AND subject_submission_quota BETWEEN 0 AND 10000), updated_at TEXT NOT NULL)",
    "CREATE TABLE experiment_capacity_reservations (experiment_id TEXT NOT NULL PRIMARY KEY, owner_id TEXT NOT NULL CHECK(typeof(owner_id)='text' AND length(owner_id) BETWEEN 1 AND 256), server_id TEXT NOT NULL CHECK(typeof(server_id)='text' AND length(server_id) BETWEEN 1 AND 128), execution_slots INTEGER NOT NULL CHECK(typeof(execution_slots)='integer' AND execution_slots BETWEEN 1 AND 64), reserved_at TEXT NOT NULL, released_at TEXT, FOREIGN KEY(experiment_id,owner_id) REFERENCES experiments(experiment_id,owner_id) ON DELETE RESTRICT, UNIQUE(owner_id,server_id,experiment_id), CHECK((released_at IS NULL) OR length(released_at)>0))",
    "CREATE INDEX ix_experiment_capacity_server ON experiment_capacity_reservations(server_id,released_at,experiment_id)",
    "CREATE TRIGGER tr_experiment_capacity_reservation_guard BEFORE INSERT ON experiment_capacity_reservations WHEN NEW.released_at IS NULL AND (SELECT COALESCE(SUM(execution_slots),0) FROM experiment_capacity_reservations WHERE server_id=NEW.server_id AND released_at IS NULL)+NEW.execution_slots > COALESCE((SELECT execution_slots FROM experiment_server_capacities WHERE server_id=NEW.server_id),1) BEGIN SELECT RAISE(ABORT,'experiment server capacity exceeded'); END",
    "CREATE TRIGGER tr_experiment_capacity_reservation_update_guard BEFORE UPDATE OF owner_id,server_id,execution_slots,released_at ON experiment_capacity_reservations WHEN NEW.released_at IS NULL AND (SELECT COALESCE(SUM(execution_slots),0) FROM experiment_capacity_reservations WHERE server_id=NEW.server_id AND released_at IS NULL AND experiment_id!=NEW.experiment_id)+NEW.execution_slots > COALESCE((SELECT execution_slots FROM experiment_server_capacities WHERE server_id=NEW.server_id),1) BEGIN SELECT RAISE(ABORT,'experiment server capacity exceeded'); END",
    "CREATE TRIGGER tr_experiment_capacity_reservation_identity BEFORE UPDATE OF experiment_id,owner_id,server_id,reserved_at ON experiment_capacity_reservations BEGIN SELECT RAISE(ABORT,'experiment capacity reservation identity is immutable'); END",
    "CREATE TRIGGER tr_experiment_capacity_reservation_no_delete BEFORE DELETE ON experiment_capacity_reservations BEGIN SELECT RAISE(ABORT,'experiment capacity reservation is immutable'); END",
    f"""
    CREATE TABLE experiment_plans (
        plan_id TEXT NOT NULL PRIMARY KEY CHECK({_typed_id_check("plan_id", "experiment_plan_")}),
        experiment_id TEXT NOT NULL CHECK({_typed_id_check("experiment_id", "experiment_")}),
        owner_id TEXT NOT NULL CHECK(typeof(owner_id)='text' AND length(owner_id) BETWEEN 1 AND 256),
        workflow_id TEXT NOT NULL CHECK({_workflow_id_check("workflow_id")}),
        server_id TEXT NOT NULL CHECK({_safe_identifier_check("server_id")}),
        plan_digest TEXT NOT NULL CHECK({_sha256_check("plan_digest")}),
        pinned_revision_id TEXT CHECK(pinned_revision_id IS NULL OR {_typed_id_check("pinned_revision_id", "revision_")}),
        pinned_deployment_id TEXT CHECK(pinned_deployment_id IS NULL OR {_typed_id_check("pinned_deployment_id", "deployment_")}),
        pinned_content_digest TEXT CHECK(pinned_content_digest IS NULL OR {_sha256_check("pinned_content_digest")}),
        expansion_json TEXT NOT NULL CHECK(typeof(expansion_json)='text'),
        base_arguments_json TEXT NOT NULL CHECK(typeof(base_arguments_json)='text'),
        budgets_json TEXT NOT NULL CHECK(typeof(budgets_json)='text'),
        budget_totals_json TEXT NOT NULL CHECK(typeof(budget_totals_json)='text'),
        failure_policy TEXT NOT NULL CHECK(failure_policy IN ('continue','stop_new','cancel_queued')),
        concurrency INTEGER NOT NULL CHECK(typeof(concurrency)='integer' AND concurrency BETWEEN 1 AND 64),
        submission_window INTEGER NOT NULL CHECK(typeof(submission_window)='integer' AND submission_window BETWEEN 0 AND 10000),
        execution_slots INTEGER NOT NULL DEFAULT 1 CHECK(typeof(execution_slots)='integer' AND execution_slots BETWEEN 1 AND 64),
        output_cardinality INTEGER NOT NULL CHECK(typeof(output_cardinality)='integer' AND output_cardinality BETWEEN 1 AND 100000),
        trusted_seconds_per_run REAL NOT NULL CHECK(typeof(trusted_seconds_per_run) IN ('integer','real') AND trusted_seconds_per_run>0 AND trusted_seconds_per_run<=31536000),
        variant_count INTEGER NOT NULL CHECK(typeof(variant_count)='integer' AND variant_count BETWEEN 1 AND 10000),
        variants_json TEXT NOT NULL CHECK(typeof(variants_json)='text'),
        variant_overrides_json TEXT NOT NULL CHECK(typeof(variant_overrides_json)='text' AND length(CAST(variant_overrides_json AS BLOB))<=8388608),
        retained_bytes INTEGER NOT NULL CHECK(typeof(retained_bytes)='integer' AND retained_bytes BETWEEN 1 AND 8388608),
        created_at TEXT NOT NULL CHECK(typeof(created_at)='text' AND length(created_at)>0),
        expires_at TEXT NOT NULL CHECK(typeof(expires_at)='text' AND length(expires_at)>0),
        payload_pruned_at TEXT,
        committed_at TEXT,
        committed_experiment_id TEXT,
        FOREIGN KEY(workflow_id) REFERENCES workflows(workflow_id) ON DELETE RESTRICT,
        FOREIGN KEY(workflow_id,pinned_revision_id,pinned_content_digest)
            REFERENCES workflow_revisions(workflow_id,revision_id,content_digest) ON DELETE RESTRICT,
        FOREIGN KEY(pinned_deployment_id,workflow_id,pinned_revision_id,server_id)
            REFERENCES workflow_deployments(deployment_id,workflow_id,revision_id,server_id) ON DELETE RESTRICT,
        UNIQUE(owner_id,plan_digest),
        UNIQUE(plan_id,owner_id),
        UNIQUE(plan_id,owner_id,experiment_id),
        UNIQUE(plan_id,owner_id,experiment_id,workflow_id,server_id),
        UNIQUE(plan_id,owner_id,experiment_id,workflow_id,server_id,pinned_revision_id,pinned_deployment_id,pinned_content_digest),
        CHECK((committed_at IS NULL) = (committed_experiment_id IS NULL)),
        CHECK(committed_experiment_id IS NULL OR committed_experiment_id=experiment_id)
        ,CHECK((pinned_revision_id IS NULL AND pinned_deployment_id IS NULL AND pinned_content_digest IS NULL) OR (pinned_revision_id IS NOT NULL AND pinned_deployment_id IS NOT NULL AND pinned_content_digest IS NOT NULL))
        ,CHECK(payload_pruned_at IS NULL OR committed_at IS NOT NULL)
    )
    """,
    "CREATE INDEX ix_experiment_plans_owner_created ON experiment_plans(owner_id,created_at DESC,plan_id DESC)",
    "CREATE UNIQUE INDEX uq_workflow_revision_pin ON workflow_revisions(workflow_id,revision_id,content_digest)",
    "CREATE INDEX ix_experiment_plans_expiry ON experiment_plans(expires_at,plan_id) WHERE committed_at IS NULL",
    "CREATE INDEX ix_experiment_plans_owner_unpruned ON experiment_plans(owner_id,created_at,plan_id) WHERE payload_pruned_at IS NULL",
    """
    CREATE TRIGGER tr_experiment_plans_identity_update BEFORE UPDATE OF
        plan_id,experiment_id,owner_id,workflow_id,server_id,plan_digest,
        pinned_revision_id,pinned_deployment_id,pinned_content_digest,
        failure_policy,concurrency,execution_slots,submission_window,variant_count,
        output_cardinality,trusted_seconds_per_run,created_at,expires_at
    ON experiment_plans
    BEGIN SELECT RAISE(ABORT,'experiment plan identity is immutable'); END
    """,
    "CREATE TRIGGER tr_experiment_plans_payload_compaction BEFORE UPDATE OF expansion_json,base_arguments_json,budgets_json,budget_totals_json,variants_json,variant_overrides_json,retained_bytes,payload_pruned_at ON experiment_plans WHEN OLD.payload_pruned_at IS NOT NULL OR NEW.payload_pruned_at IS NULL OR OLD.committed_at IS NULL OR NEW.expansion_json!='{}' OR NEW.base_arguments_json!='{}' OR NEW.budgets_json!='{}' OR NEW.budget_totals_json!='{}' OR NEW.variants_json!='[]' OR NEW.variant_overrides_json!='{}' OR NEW.retained_bytes!=12 OR NOT EXISTS (SELECT 1 FROM experiments WHERE experiments.experiment_id=OLD.experiment_id AND experiments.owner_id=OLD.owner_id AND experiments.plan_id=OLD.plan_id AND experiments.status IN ('completed','completed_with_errors','cancelled')) BEGIN SELECT RAISE(ABORT,'experiment plan payload compaction is one-way and terminal-only'); END",
    "CREATE TRIGGER tr_experiment_plans_commit_evidence BEFORE UPDATE OF committed_at,committed_experiment_id ON experiment_plans WHEN OLD.committed_at IS NOT NULL OR NEW.committed_at IS NULL OR NEW.committed_experiment_id!=OLD.experiment_id OR NOT EXISTS (SELECT 1 FROM experiments WHERE experiment_id=NEW.committed_experiment_id AND owner_id=NEW.owner_id AND plan_id=NEW.plan_id AND pinned_revision_id IS NEW.pinned_revision_id AND pinned_deployment_id IS NEW.pinned_deployment_id AND pinned_content_digest IS NEW.pinned_content_digest) OR (SELECT count(*) FROM experiment_variants WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id)!=NEW.variant_count OR EXISTS (SELECT 1 FROM experiment_variants WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id AND (ordinal<0 OR ordinal>=NEW.variant_count)) BEGIN SELECT RAISE(ABORT,'experiment plan commit evidence is append-once and aggregate-bound'); END",
    "CREATE TRIGGER tr_experiment_plans_commit_variant_facts BEFORE UPDATE OF committed_at,committed_experiment_id ON experiment_plans WHEN NEW.committed_at IS NOT NULL AND EXISTS (SELECT 1 FROM experiments AS aggregate WHERE aggregate.experiment_id=NEW.experiment_id AND aggregate.owner_id=NEW.owner_id AND (aggregate.pending_count!=(SELECT count(*) FROM experiment_variants WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id AND status='pending') OR aggregate.submitted_count!=(SELECT count(*) FROM experiment_variants WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id AND status='submitted') OR aggregate.running_count!=(SELECT count(*) FROM experiment_variants WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id AND status='running') OR aggregate.completed_count!=(SELECT count(*) FROM experiment_variants WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id AND status='completed') OR aggregate.failed_count!=(SELECT count(*) FROM experiment_variants WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id AND status='failed') OR aggregate.cancelled_count!=(SELECT count(*) FROM experiment_variants WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id AND status='cancelled') OR aggregate.lost_count!=(SELECT count(*) FROM experiment_variants WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id AND status='lost') OR (aggregate.status IN ('completed','completed_with_errors','cancelled') AND EXISTS (SELECT 1 FROM experiment_variants WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id AND status IN ('pending','submitted','running'))))) BEGIN SELECT RAISE(ABORT,'experiment plan commit evidence conflicts with physical Variant facts'); END",
    "CREATE TRIGGER tr_experiment_plans_no_delete BEFORE DELETE ON experiment_plans WHEN OLD.committed_at IS NOT NULL OR julianday(OLD.expires_at)>julianday('now') BEGIN SELECT RAISE(ABORT,'experiment plan retention is not eligible'); END",
    f"""
    CREATE TABLE experiment_plan_variants (
        plan_id TEXT NOT NULL CHECK({_typed_id_check("plan_id", "experiment_plan_")}),
        owner_id TEXT NOT NULL,
        experiment_id TEXT NOT NULL CHECK({_typed_id_check("experiment_id", "experiment_")}),
        variant_id TEXT NOT NULL CHECK({_typed_id_check("variant_id", "variant_")}),
        ordinal INTEGER NOT NULL CHECK(typeof(ordinal)='integer' AND ordinal>=0),
        overrides_json TEXT NOT NULL CHECK(typeof(overrides_json)='text'),
        parameter_digest TEXT NOT NULL CHECK({_sha256_check("parameter_digest")}),
        created_at TEXT NOT NULL,
        PRIMARY KEY(plan_id,variant_id),
        FOREIGN KEY(plan_id,owner_id,experiment_id) REFERENCES experiment_plans(plan_id,owner_id,experiment_id) ON DELETE CASCADE,
        UNIQUE(plan_id,owner_id,experiment_id,variant_id),
        UNIQUE(plan_id,ordinal)
    )
    """,
    "CREATE INDEX ix_experiment_plan_variants_enrollment ON experiment_plan_variants(plan_id,ordinal,variant_id)",
    "CREATE TRIGGER tr_experiment_plan_variants_identity_update BEFORE UPDATE OF plan_id,owner_id,experiment_id,variant_id,ordinal,parameter_digest,created_at ON experiment_plan_variants BEGIN SELECT RAISE(ABORT,'experiment plan Variant enrollment identity is immutable'); END",
    "CREATE TRIGGER tr_experiment_plan_variants_payload_compaction BEFORE UPDATE OF overrides_json ON experiment_plan_variants WHEN OLD.overrides_json='{}' OR NEW.overrides_json!='{}' OR NOT EXISTS (SELECT 1 FROM experiment_plans JOIN experiments ON experiments.plan_id=experiment_plans.plan_id AND experiments.owner_id=experiment_plans.owner_id AND experiments.experiment_id=experiment_plans.experiment_id WHERE experiment_plans.plan_id=OLD.plan_id AND experiment_plans.owner_id=OLD.owner_id AND experiment_plans.payload_pruned_at IS NOT NULL AND experiments.status IN ('completed','completed_with_errors','cancelled')) BEGIN SELECT RAISE(ABORT,'experiment plan Variant payload compaction is one-way and terminal-only'); END",
    "CREATE TRIGGER tr_experiment_plan_variants_no_delete BEFORE DELETE ON experiment_plan_variants WHEN EXISTS (SELECT 1 FROM experiment_plans WHERE plan_id=OLD.plan_id) AND NOT EXISTS (SELECT 1 FROM experiment_plans WHERE plan_id=OLD.plan_id AND committed_at IS NULL AND julianday(expires_at)<=julianday('now')) BEGIN SELECT RAISE(ABORT,'experiment plan Variant enrollment is immutable'); END",
    """
    CREATE TRIGGER tr_experiment_plan_variants_retained_bytes
    BEFORE INSERT ON experiment_plan_variants
    WHEN (
        SELECT
            length(CAST(plans.plan_id AS BLOB))+
            length(CAST(plans.experiment_id AS BLOB))+
            length(CAST(plans.owner_id AS BLOB))+
            length(CAST(plans.workflow_id AS BLOB))+
            length(CAST(plans.server_id AS BLOB))+
            length(CAST(plans.plan_digest AS BLOB))+
            length(CAST(plans.pinned_revision_id AS BLOB))+
            length(CAST(plans.pinned_deployment_id AS BLOB))+
            length(CAST(plans.pinned_content_digest AS BLOB))+
            length(CAST(plans.expansion_json AS BLOB))+
            length(CAST(plans.base_arguments_json AS BLOB))+
            length(CAST(plans.budgets_json AS BLOB))+
            length(CAST(plans.budget_totals_json AS BLOB))+
            length(CAST(plans.failure_policy AS BLOB))+
            length(CAST(plans.concurrency AS BLOB))+
            length(CAST(plans.execution_slots AS BLOB))+
            length(CAST(plans.submission_window AS BLOB))+
            length(CAST(plans.variant_count AS BLOB))+
            length(CAST(plans.output_cardinality AS BLOB))+
            length(CAST(plans.trusted_seconds_per_run AS BLOB))+
            length(CAST(plans.variants_json AS BLOB))+
            length(CAST(plans.variant_overrides_json AS BLOB))+
            length(CAST(plans.created_at AS BLOB))+
            length(CAST(plans.expires_at AS BLOB))+
            COALESCE((
                SELECT SUM(
                    length(CAST(enrollment.plan_id AS BLOB))+
                    length(CAST(enrollment.owner_id AS BLOB))+
                    length(CAST(enrollment.experiment_id AS BLOB))+
                    length(CAST(enrollment.variant_id AS BLOB))+
                    length(CAST(enrollment.ordinal AS BLOB))+
                    length(CAST(enrollment.overrides_json AS BLOB))+
                    length(CAST(enrollment.parameter_digest AS BLOB))+
                    length(CAST(enrollment.created_at AS BLOB))
                )
                FROM experiment_plan_variants AS enrollment
                WHERE enrollment.plan_id=NEW.plan_id
            ),0)
        FROM experiment_plans AS plans WHERE plans.plan_id=NEW.plan_id
    )+
        length(CAST(NEW.plan_id AS BLOB))+
        length(CAST(NEW.owner_id AS BLOB))+
        length(CAST(NEW.experiment_id AS BLOB))+
        length(CAST(NEW.variant_id AS BLOB))+
        length(CAST(NEW.ordinal AS BLOB))+
        length(CAST(NEW.overrides_json AS BLOB))+
        length(CAST(NEW.parameter_digest AS BLOB))+
        length(CAST(NEW.created_at AS BLOB))
        > (SELECT retained_bytes FROM experiment_plans WHERE plan_id=NEW.plan_id)
    BEGIN SELECT RAISE(ABORT,'experiment plan retained byte accounting conflict'); END
    """,
    f"""
    CREATE TABLE experiments (
        experiment_id TEXT NOT NULL PRIMARY KEY CHECK({_typed_id_check("experiment_id", "experiment_")}),
        owner_id TEXT NOT NULL CHECK(typeof(owner_id)='text' AND length(owner_id) BETWEEN 1 AND 256),
        plan_id TEXT NOT NULL CHECK({_typed_id_check("plan_id", "experiment_plan_")}),
        workflow_id TEXT NOT NULL CHECK({_workflow_id_check("workflow_id")}),
        server_id TEXT NOT NULL CHECK({_safe_identifier_check("server_id")}),
        pinned_revision_id TEXT,
        pinned_deployment_id TEXT,
        pinned_content_digest TEXT,
        status TEXT NOT NULL CHECK(status IN ('queued','running','completed','completed_with_errors','cancelled')),
        failure_policy TEXT NOT NULL CHECK(failure_policy IN ('continue','stop_new','cancel_queued')),
        concurrency INTEGER NOT NULL CHECK(typeof(concurrency)='integer' AND concurrency BETWEEN 1 AND 64),
        execution_slots INTEGER NOT NULL DEFAULT 1 CHECK(typeof(execution_slots)='integer' AND execution_slots BETWEEN 1 AND 64),
        submission_window INTEGER NOT NULL CHECK(typeof(submission_window)='integer' AND submission_window BETWEEN 0 AND 10000),
        variant_count INTEGER NOT NULL CHECK(typeof(variant_count)='integer' AND variant_count BETWEEN 1 AND 10000),
        pending_count INTEGER NOT NULL CHECK(typeof(pending_count)='integer' AND pending_count>=0),
        submitted_count INTEGER NOT NULL CHECK(typeof(submitted_count)='integer' AND submitted_count>=0),
        running_count INTEGER NOT NULL CHECK(typeof(running_count)='integer' AND running_count>=0),
        completed_count INTEGER NOT NULL CHECK(typeof(completed_count)='integer' AND completed_count>=0),
        failed_count INTEGER NOT NULL CHECK(typeof(failed_count)='integer' AND failed_count>=0),
        cancelled_count INTEGER NOT NULL CHECK(typeof(cancelled_count)='integer' AND cancelled_count>=0),
        lost_count INTEGER NOT NULL CHECK(typeof(lost_count)='integer' AND lost_count>=0),
        cancel_mode TEXT CHECK(cancel_mode IS NULL OR cancel_mode IN ('stop_new','cancel_queued')),
        created_at TEXT NOT NULL CHECK(typeof(created_at)='text' AND length(created_at)>0),
        updated_at TEXT NOT NULL CHECK(typeof(updated_at)='text' AND length(updated_at)>0),
        completed_at TEXT,
        FOREIGN KEY(plan_id,owner_id,experiment_id,workflow_id,server_id)
            REFERENCES experiment_plans(plan_id,owner_id,experiment_id,workflow_id,server_id) ON DELETE RESTRICT,
        FOREIGN KEY(workflow_id,pinned_revision_id,pinned_content_digest)
            REFERENCES workflow_revisions(workflow_id,revision_id,content_digest) ON DELETE RESTRICT,
        FOREIGN KEY(pinned_deployment_id,workflow_id,pinned_revision_id,server_id)
            REFERENCES workflow_deployments(deployment_id,workflow_id,revision_id,server_id) ON DELETE RESTRICT,
        UNIQUE(experiment_id,owner_id),
        UNIQUE(experiment_id,owner_id,workflow_id,server_id),
        CHECK(pending_count+submitted_count+running_count+completed_count+failed_count+cancelled_count+lost_count=variant_count),
        CHECK((status IN ('completed','completed_with_errors','cancelled'))=(completed_at IS NOT NULL))
        ,CHECK(status NOT IN ('completed','completed_with_errors','cancelled') OR pending_count+submitted_count+running_count=0)
    )
    """,
    "CREATE INDEX ix_experiments_owner_updated ON experiments(owner_id,updated_at DESC,experiment_id DESC)",
    "CREATE INDEX ix_experiments_terminal_retention ON experiments(completed_at,plan_id) WHERE status IN ('completed','completed_with_errors','cancelled')",
    "CREATE TRIGGER tr_experiments_pin_insert BEFORE INSERT ON experiments WHEN NOT EXISTS (SELECT 1 FROM experiment_plans WHERE plan_id=NEW.plan_id AND owner_id=NEW.owner_id AND experiment_id=NEW.experiment_id AND pinned_revision_id IS NEW.pinned_revision_id AND pinned_deployment_id IS NEW.pinned_deployment_id AND pinned_content_digest IS NEW.pinned_content_digest) BEGIN SELECT RAISE(ABORT,'experiment publication pin conflict'); END",
    "CREATE TRIGGER tr_experiments_count_guard BEFORE UPDATE OF pending_count,submitted_count,running_count,completed_count,failed_count,cancelled_count,lost_count ON experiments WHEN NEW.pending_count!=(SELECT count(*) FROM experiment_variants WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id AND status='pending') OR NEW.submitted_count!=(SELECT count(*) FROM experiment_variants WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id AND status='submitted') OR NEW.running_count!=(SELECT count(*) FROM experiment_variants WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id AND status='running') OR NEW.completed_count!=(SELECT count(*) FROM experiment_variants WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id AND status='completed') OR NEW.failed_count!=(SELECT count(*) FROM experiment_variants WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id AND status='failed') OR NEW.cancelled_count!=(SELECT count(*) FROM experiment_variants WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id AND status='cancelled') OR NEW.lost_count!=(SELECT count(*) FROM experiment_variants WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id AND status='lost') BEGIN SELECT RAISE(ABORT,'experiment aggregate counts conflict with Variants'); END",
    "CREATE TRIGGER tr_experiments_terminal_predicate BEFORE UPDATE OF status ON experiments WHEN NEW.status IN ('completed','completed_with_errors','cancelled') AND EXISTS (SELECT 1 FROM experiment_variants WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id AND status IN ('pending','submitted','running')) BEGIN SELECT RAISE(ABORT,'terminal experiment has unfinished Variants'); END",
    "CREATE TRIGGER tr_experiments_release_capacity AFTER UPDATE OF status ON experiments WHEN NEW.status IN ('completed','completed_with_errors','cancelled') AND OLD.status NOT IN ('completed','completed_with_errors','cancelled') BEGIN UPDATE experiment_capacity_reservations SET released_at=NEW.completed_at WHERE experiment_id=NEW.experiment_id AND released_at IS NULL; END",
    "CREATE TRIGGER tr_experiments_no_delete BEFORE DELETE ON experiments BEGIN SELECT RAISE(ABORT,'experiment is immutable'); END",
    """
    CREATE TRIGGER tr_experiments_terminal_regression BEFORE UPDATE OF status ON experiments
    WHEN OLD.status IN ('completed','completed_with_errors','cancelled') AND NEW.status!=OLD.status
    BEGIN SELECT RAISE(ABORT,'terminal experiment state cannot regress'); END
    """,
    """
    CREATE TRIGGER tr_experiments_identity_update BEFORE UPDATE OF
        experiment_id,owner_id,plan_id,workflow_id,server_id,
        pinned_revision_id,pinned_deployment_id,pinned_content_digest,
        failure_policy,concurrency,execution_slots,submission_window,variant_count,created_at
    ON experiments
    BEGIN SELECT RAISE(ABORT,'experiment identity is immutable'); END
    """,
)

_PHASE_M_EXPERIMENT_VARIANTS_UP = (
    f"""
    CREATE TABLE experiment_variants (
        variant_id TEXT NOT NULL PRIMARY KEY CHECK({_typed_id_check("variant_id", "variant_")}),
        experiment_id TEXT NOT NULL CHECK({_typed_id_check("experiment_id", "experiment_")}),
        owner_id TEXT NOT NULL CHECK(typeof(owner_id)='text' AND length(owner_id) BETWEEN 1 AND 256),
        ordinal INTEGER NOT NULL CHECK(typeof(ordinal)='integer' AND ordinal>=0),
        overrides_json TEXT NOT NULL CHECK(typeof(overrides_json)='text'),
        parameter_digest TEXT NOT NULL CHECK({_sha256_check("parameter_digest")}),
        execution_input_digest TEXT CHECK(execution_input_digest IS NULL OR {_sha256_check("execution_input_digest")}),
        client_id TEXT NOT NULL CHECK(typeof(client_id)='text' AND length(client_id) BETWEEN 1 AND 256),
        idempotency_key TEXT NOT NULL CHECK(typeof(idempotency_key)='text' AND length(idempotency_key) BETWEEN 1 AND 256),
        status TEXT NOT NULL CHECK(status IN ('pending','submitted','running','completed','failed','cancelled','lost')),
        lease_work_item_id TEXT,
        lease_worker_id TEXT,
        lease_token INTEGER NOT NULL DEFAULT 0 CHECK(typeof(lease_token)='integer' AND lease_token>=0),
        lease_expires_at TEXT,
        checkpoint_json TEXT NOT NULL DEFAULT '{{}}' CHECK(typeof(checkpoint_json)='text'),
        payload_compacted_at TEXT,
        measured_pixels INTEGER CHECK(measured_pixels IS NULL OR (typeof(measured_pixels)='integer' AND measured_pixels>=0)),
        measured_outputs INTEGER CHECK(measured_outputs IS NULL OR (typeof(measured_outputs)='integer' AND measured_outputs>=0)),
        measured_seconds REAL CHECK(measured_seconds IS NULL OR (typeof(measured_seconds) IN ('integer','real') AND measured_seconds>=0)),
        error_code TEXT,
        created_at TEXT NOT NULL CHECK(typeof(created_at)='text' AND length(created_at)>0),
        updated_at TEXT NOT NULL CHECK(typeof(updated_at)='text' AND length(updated_at)>0),
        completed_at TEXT,
        FOREIGN KEY(experiment_id,owner_id) REFERENCES experiments(experiment_id,owner_id) ON DELETE RESTRICT,
        FOREIGN KEY(lease_work_item_id) REFERENCES operation_work_items(work_item_id) ON DELETE RESTRICT,
        UNIQUE(experiment_id,owner_id,variant_id),
        UNIQUE(experiment_id,owner_id,ordinal),
        UNIQUE(owner_id,client_id),
        UNIQUE(owner_id,idempotency_key),
        CHECK((lease_worker_id IS NULL)=(lease_expires_at IS NULL)),
        CHECK((lease_worker_id IS NULL)=(lease_work_item_id IS NULL)),
        CHECK((lease_worker_id IS NULL)=(lease_token=0)),
        CHECK(lease_worker_id IS NULL OR length(lease_worker_id)>0),
        CHECK((status IN ('completed','failed','cancelled','lost'))=(completed_at IS NOT NULL))
    )
    """,
    "CREATE INDEX ix_experiment_variants_page ON experiment_variants(experiment_id,owner_id,created_at,variant_id)",
    "CREATE INDEX ix_experiment_variants_claimable ON experiment_variants(status,lease_expires_at,experiment_id,ordinal,variant_id) WHERE status='pending'",
    "CREATE INDEX ix_experiment_variants_worker ON experiment_variants(experiment_id,owner_id,ordinal,variant_id) WHERE status IN ('pending','submitted','running')",
    "CREATE INDEX ix_experiment_variants_terminal_retention ON experiment_variants(completed_at,variant_id) WHERE status IN ('completed','failed','cancelled','lost') AND checkpoint_json!='{}'",
    "CREATE TRIGGER tr_experiment_variants_enrollment_insert BEFORE INSERT ON experiment_variants WHEN NEW.status!='pending' OR NEW.completed_at IS NOT NULL OR NEW.execution_input_digest IS NOT NULL OR NOT EXISTS (SELECT 1 FROM experiments JOIN experiment_plan_variants AS enrollment ON enrollment.plan_id=experiments.plan_id AND enrollment.owner_id=experiments.owner_id AND enrollment.experiment_id=experiments.experiment_id WHERE experiments.experiment_id=NEW.experiment_id AND experiments.owner_id=NEW.owner_id AND enrollment.variant_id=NEW.variant_id AND enrollment.ordinal=NEW.ordinal AND enrollment.overrides_json=NEW.overrides_json AND enrollment.parameter_digest=NEW.parameter_digest) OR (SELECT count(*) FROM experiment_variants WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id)>=(SELECT variant_count FROM experiments WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id) BEGIN SELECT RAISE(ABORT,'experiment Variant enrollment conflict'); END",
    "CREATE TRIGGER tr_experiment_variants_lease_mutation BEFORE UPDATE OF status,checkpoint_json,measured_pixels,measured_outputs,measured_seconds,error_code ON experiment_variants WHEN OLD.lease_token>0 AND NOT (OLD.status IN ('completed','failed','cancelled','lost') AND NEW.status=OLD.status AND NEW.checkpoint_json='{}' AND NEW.payload_compacted_at IS NOT NULL) AND (julianday(NEW.updated_at)<julianday(OLD.updated_at) OR NOT EXISTS (SELECT 1 FROM work_leases JOIN operation_work_items USING(work_item_id) WHERE work_leases.work_item_id=OLD.lease_work_item_id AND work_leases.worker_id=OLD.lease_worker_id AND work_leases.fencing_token=OLD.lease_token AND julianday(work_leases.expires_at)>julianday('now') AND operation_work_items.status='running')) BEGIN SELECT RAISE(ABORT,'experiment Variant lease is expired or fenced'); END",
    """
    CREATE TRIGGER tr_experiment_variants_identity_update BEFORE UPDATE OF
        variant_id,experiment_id,owner_id,ordinal,parameter_digest,
        client_id,idempotency_key,created_at
    ON experiment_variants
    BEGIN SELECT RAISE(ABORT,'experiment variant identity is immutable'); END
    """,
    "CREATE TRIGGER tr_experiment_variants_payload_compaction BEFORE UPDATE OF overrides_json ON experiment_variants WHEN OLD.overrides_json='{}' OR NEW.overrides_json!='{}' OR NEW.payload_compacted_at IS NULL OR OLD.status NOT IN ('completed','failed','cancelled','lost') OR NEW.status!=OLD.status OR NOT EXISTS (SELECT 1 FROM experiments JOIN experiment_plans ON experiment_plans.plan_id=experiments.plan_id AND experiment_plans.owner_id=experiments.owner_id WHERE experiments.experiment_id=OLD.experiment_id AND experiments.owner_id=OLD.owner_id AND experiments.status IN ('completed','completed_with_errors','cancelled') AND experiment_plans.payload_pruned_at IS NOT NULL) BEGIN SELECT RAISE(ABORT,'experiment Variant payload compaction is one-way and terminal-only'); END",
    "CREATE TRIGGER tr_experiment_variants_execution_digest_append BEFORE UPDATE OF execution_input_digest ON experiment_variants WHEN OLD.execution_input_digest IS NOT NULL OR NEW.execution_input_digest IS NULL OR OLD.status!='pending' OR NEW.status!='submitted' OR julianday(NEW.updated_at)<julianday(OLD.updated_at) OR NOT EXISTS (SELECT 1 FROM work_leases JOIN operation_work_items USING(work_item_id) WHERE work_leases.work_item_id=NEW.lease_work_item_id AND work_leases.worker_id=NEW.lease_worker_id AND work_leases.fencing_token=NEW.lease_token AND julianday(work_leases.expires_at)>julianday('now') AND operation_work_items.status='running') BEGIN SELECT RAISE(ABORT,'experiment Variant execution digest is append-once and lease-bound'); END",
    """
    CREATE TRIGGER tr_experiment_variants_transition BEFORE UPDATE OF status ON experiment_variants
    WHEN NOT (
        NEW.status=OLD.status OR
        (OLD.status='pending' AND NEW.status IN ('submitted','running','completed','failed','cancelled','lost')) OR
        (OLD.status='submitted' AND NEW.status IN ('running','completed','failed','cancelled','lost')) OR
        (OLD.status='running' AND NEW.status IN ('completed','failed','cancelled','lost'))
    )
    BEGIN SELECT RAISE(ABORT,'invalid experiment variant state transition'); END
    """,
    """
    CREATE TRIGGER tr_experiment_variants_count_state AFTER UPDATE OF status ON experiment_variants
    WHEN NEW.status!=OLD.status
    BEGIN
        UPDATE experiments SET
            pending_count=pending_count-(OLD.status='pending')+(NEW.status='pending'),
            submitted_count=submitted_count-(OLD.status='submitted')+(NEW.status='submitted'),
            running_count=running_count-(OLD.status='running')+(NEW.status='running'),
            completed_count=completed_count-(OLD.status='completed')+(NEW.status='completed'),
            failed_count=failed_count-(OLD.status='failed')+(NEW.status='failed'),
            cancelled_count=cancelled_count-(OLD.status='cancelled')+(NEW.status='cancelled'),
            lost_count=lost_count-(OLD.status='lost')+(NEW.status='lost'),
            updated_at=NEW.updated_at
        WHERE experiment_id=NEW.experiment_id AND owner_id=NEW.owner_id;
    END
    """,
    "CREATE TRIGGER tr_experiment_variants_no_delete BEFORE DELETE ON experiment_variants BEGIN SELECT RAISE(ABORT,'experiment variant is immutable'); END",
    f"""
    CREATE TABLE experiment_variant_jobs (
        experiment_id TEXT NOT NULL CHECK({_typed_id_check("experiment_id", "experiment_")}),
        variant_id TEXT NOT NULL CHECK({_typed_id_check("variant_id", "variant_")}),
        owner_id TEXT NOT NULL,
        job_id TEXT NOT NULL CHECK({_typed_id_check("job_id", "job_")}),
        workflow_id TEXT NOT NULL,
        server_id TEXT NOT NULL,
        plan_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        deployment_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        linked_at TEXT NOT NULL,
        PRIMARY KEY(experiment_id,variant_id),
        FOREIGN KEY(experiment_id,owner_id,variant_id)
            REFERENCES experiment_variants(experiment_id,owner_id,variant_id) ON DELETE RESTRICT,
        FOREIGN KEY(job_id,owner_id) REFERENCES jobs(job_id,owner_id) ON DELETE RESTRICT,
        FOREIGN KEY(attempt_id) REFERENCES execution_attempts(attempt_id) ON DELETE RESTRICT,
        UNIQUE(owner_id,job_id)
    )
    """,
    "CREATE INDEX ix_experiment_variant_jobs_job ON experiment_variant_jobs(owner_id,job_id)",
    "CREATE INDEX ix_experiment_variant_jobs_attempt ON experiment_variant_jobs(attempt_id,job_id) WHERE attempt_id IS NOT NULL",
    """
    CREATE TRIGGER tr_experiment_variant_jobs_binding_insert
    BEFORE INSERT ON experiment_variant_jobs
    WHEN NOT EXISTS (
        SELECT 1
        FROM experiment_variants AS variants
        JOIN experiments
          ON experiments.experiment_id=variants.experiment_id
         AND experiments.owner_id=variants.owner_id
        JOIN jobs
          ON jobs.job_id=NEW.job_id AND jobs.owner_id=NEW.owner_id
        WHERE variants.experiment_id=NEW.experiment_id
          AND variants.variant_id=NEW.variant_id
          AND variants.owner_id=NEW.owner_id
          AND jobs.workflow_id=experiments.workflow_id
          AND EXISTS (
              SELECT 1 FROM execution_plans
              WHERE execution_plans.plan_id=jobs.plan_id
                AND execution_plans.workflow_id=experiments.workflow_id
                AND execution_plans.revision_id=experiments.pinned_revision_id
                AND execution_plans.deployment_id=experiments.pinned_deployment_id
                AND execution_plans.server_id=experiments.server_id
                AND execution_plans.raw_arguments_digest=variants.execution_input_digest
          )
          AND NEW.workflow_id=experiments.workflow_id
          AND NEW.server_id=experiments.server_id
          AND NEW.plan_id=jobs.plan_id
          AND NEW.revision_id=experiments.pinned_revision_id
          AND NEW.deployment_id=experiments.pinned_deployment_id
          AND (
              NEW.attempt_id IS NULL OR EXISTS (
                  SELECT 1 FROM execution_attempts
                  WHERE attempt_id=NEW.attempt_id AND job_id=NEW.job_id
                    AND server_id=experiments.server_id
              )
          )
    )
    BEGIN SELECT RAISE(ABORT,'experiment Variant Job execution binding conflict'); END
    """,
    "CREATE TRIGGER tr_experiment_variant_jobs_immutable_update BEFORE UPDATE ON experiment_variant_jobs BEGIN SELECT RAISE(ABORT,'experiment variant job binding is immutable'); END",
    "CREATE TRIGGER tr_experiment_variant_jobs_immutable_delete BEFORE DELETE ON experiment_variant_jobs BEGIN SELECT RAISE(ABORT,'experiment variant job binding is immutable'); END",
)

_PHASE_M_EXPERIMENT_RATINGS_UP = (
    f"""
    CREATE TABLE experiment_rubric_versions (
        owner_id TEXT NOT NULL,
        rubric_version TEXT NOT NULL CHECK({_safe_identifier_check("rubric_version")}),
        created_at TEXT NOT NULL,
        definition_json TEXT NOT NULL DEFAULT '{{}}' CHECK(typeof(definition_json)='text'),
        definition_digest TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000' CHECK({_sha256_check("definition_digest")}),
        PRIMARY KEY(owner_id,rubric_version),
        CHECK(typeof(owner_id)='text' AND length(owner_id) BETWEEN 1 AND 256),
        UNIQUE(owner_id,rubric_version,definition_digest)
    )
    """,
    "CREATE TRIGGER tr_experiment_rubric_versions_immutable_update BEFORE UPDATE ON experiment_rubric_versions BEGIN SELECT RAISE(ABORT,'experiment rubric version is immutable'); END",
    "CREATE TRIGGER tr_experiment_rubric_versions_immutable_delete BEFORE DELETE ON experiment_rubric_versions BEGIN SELECT RAISE(ABORT,'experiment rubric version is immutable'); END",
    f"""
    CREATE TABLE experiment_rubric_dimensions (
        owner_id TEXT NOT NULL,
        rubric_version TEXT NOT NULL,
        dimension TEXT NOT NULL CHECK({_safe_identifier_check("dimension")}),
        minimum REAL NOT NULL CHECK(typeof(minimum) IN ('integer','real')),
        maximum REAL NOT NULL CHECK(typeof(maximum) IN ('integer','real') AND maximum>=minimum),
        ordinal INTEGER NOT NULL CHECK(typeof(ordinal)='integer' AND ordinal>=0),
        PRIMARY KEY(owner_id,rubric_version,dimension),
        FOREIGN KEY(owner_id,rubric_version)
            REFERENCES experiment_rubric_versions(owner_id,rubric_version) ON DELETE RESTRICT,
        UNIQUE(owner_id,rubric_version,ordinal)
    )
    """,
    "CREATE TRIGGER tr_experiment_rubric_dimensions_immutable_update BEFORE UPDATE ON experiment_rubric_dimensions BEGIN SELECT RAISE(ABORT,'experiment rubric dimensions are immutable'); END",
    "CREATE TRIGGER tr_experiment_rubric_dimensions_immutable_delete BEFORE DELETE ON experiment_rubric_dimensions BEGIN SELECT RAISE(ABORT,'experiment rubric dimensions are immutable'); END",
    "CREATE TRIGGER tr_experiment_rubric_dimensions_sealed_insert BEFORE INSERT ON experiment_rubric_dimensions WHEN EXISTS (SELECT 1 FROM experiment_ratings WHERE owner_id=NEW.owner_id AND rubric_version=NEW.rubric_version) BEGIN SELECT RAISE(ABORT,'experiment rubric definition is sealed'); END",
    f"""
    CREATE TABLE experiment_ratings (
        rating_id TEXT NOT NULL PRIMARY KEY CHECK({_typed_id_check("rating_id", "rating_")}),
        owner_id TEXT NOT NULL,
        experiment_id TEXT NOT NULL,
        variant_id TEXT NOT NULL,
        rubric_version TEXT NOT NULL,
        scores_json TEXT NOT NULL CHECK(typeof(scores_json)='text'),
        rubric_definition_digest TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000' CHECK({_sha256_check("rubric_definition_digest")}),
        rating_digest TEXT NOT NULL CHECK({_sha256_check("rating_digest")}),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(experiment_id,owner_id,variant_id)
            REFERENCES experiment_variants(experiment_id,owner_id,variant_id) ON DELETE RESTRICT,
        FOREIGN KEY(owner_id,rubric_version)
            REFERENCES experiment_rubric_versions(owner_id,rubric_version) ON DELETE RESTRICT,
        UNIQUE(owner_id,experiment_id,variant_id,rubric_version),
        FOREIGN KEY(owner_id,rubric_version,rubric_definition_digest)
            REFERENCES experiment_rubric_versions(owner_id,rubric_version,definition_digest) ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX ix_experiment_ratings_variant ON experiment_ratings(owner_id,experiment_id,variant_id,created_at,rating_id)",
    """
    CREATE TRIGGER tr_experiment_ratings_identity_update BEFORE UPDATE OF
        rating_id,owner_id,experiment_id,variant_id,rubric_version,created_at
    ON experiment_ratings
    BEGIN SELECT RAISE(ABORT,'experiment rating identity is immutable'); END
    """,
    "CREATE TRIGGER tr_experiment_ratings_audit_update BEFORE UPDATE OF scores_json,rating_digest ON experiment_ratings WHEN NEW.updated_at=OLD.updated_at BEGIN SELECT RAISE(ABORT,'experiment rating mutation timestamp must advance'); END",
    "CREATE TRIGGER tr_experiment_ratings_no_delete BEFORE DELETE ON experiment_ratings BEGIN SELECT RAISE(ABORT,'experiment rating is immutable'); END",
    f"""
    CREATE TABLE experiment_presets (
        preset_id TEXT NOT NULL CHECK({_typed_id_check("preset_id", "preset_")}),
        owner_id TEXT NOT NULL,
        workflow_id TEXT NOT NULL,
        server_id TEXT NOT NULL CHECK({_safe_identifier_check("server_id")}),
        experiment_id TEXT NOT NULL,
        variant_id TEXT NOT NULL,
        arguments_json TEXT NOT NULL,
        content_digest TEXT NOT NULL CHECK({_sha256_check("content_digest")}),
        created_at TEXT NOT NULL,
        PRIMARY KEY(owner_id,preset_id),
        FOREIGN KEY(experiment_id,owner_id,variant_id)
            REFERENCES experiment_variants(experiment_id,owner_id,variant_id) ON DELETE RESTRICT,
        FOREIGN KEY(workflow_id) REFERENCES workflows(workflow_id) ON DELETE RESTRICT,
        UNIQUE(owner_id,workflow_id,server_id,content_digest)
    )
    """,
    "CREATE INDEX ix_experiment_presets_owner_workflow ON experiment_presets(owner_id,workflow_id,created_at DESC,preset_id DESC)",
    "CREATE TRIGGER tr_experiment_presets_immutable_update BEFORE UPDATE ON experiment_presets BEGIN SELECT RAISE(ABORT,'experiment preset is immutable'); END",
    "CREATE TRIGGER tr_experiment_presets_immutable_delete BEFORE DELETE ON experiment_presets BEGIN SELECT RAISE(ABORT,'experiment preset is immutable'); END",
    f"""
    CREATE TABLE experiment_promotions (
        promotion_id TEXT NOT NULL PRIMARY KEY CHECK({_typed_id_check("promotion_id", "promotion_")}),
        owner_id TEXT NOT NULL,
        experiment_id TEXT NOT NULL,
        variant_id TEXT NOT NULL,
        workflow_id TEXT NOT NULL,
        target TEXT NOT NULL CHECK(target IN ('preset','revision')),
        preset_id TEXT,
        revision_id TEXT,
        promotion_digest TEXT NOT NULL CHECK({_sha256_check("promotion_digest")}),
        created_at TEXT NOT NULL,
        FOREIGN KEY(experiment_id,owner_id,variant_id)
            REFERENCES experiment_variants(experiment_id,owner_id,variant_id) ON DELETE RESTRICT,
        FOREIGN KEY(owner_id,preset_id) REFERENCES experiment_presets(owner_id,preset_id) ON DELETE RESTRICT,
        FOREIGN KEY(workflow_id,revision_id) REFERENCES workflow_revisions(workflow_id,revision_id) ON DELETE RESTRICT,
        UNIQUE(owner_id,experiment_id,variant_id,target),
        CHECK((target='preset' AND preset_id IS NOT NULL AND revision_id IS NULL) OR
              (target='revision' AND preset_id IS NULL AND revision_id IS NOT NULL))
    )
    """,
    "CREATE INDEX ix_experiment_promotions_variant ON experiment_promotions(owner_id,experiment_id,variant_id,target)",
    "CREATE TRIGGER tr_experiment_promotions_immutable_update BEFORE UPDATE ON experiment_promotions BEGIN SELECT RAISE(ABORT,'experiment promotion is immutable'); END",
    "CREATE TRIGGER tr_experiment_promotions_immutable_delete BEFORE DELETE ON experiment_promotions BEGIN SELECT RAISE(ABORT,'experiment promotion is immutable'); END",
)


_PHASE_N_DIAGNOSTIC_RECOVERY_UP = (
    """
    CREATE TABLE diagnostic_rule_versions (
        registry_version TEXT NOT NULL PRIMARY KEY,
        rule_count INTEGER NOT NULL CHECK(typeof(rule_count)='integer' AND rule_count>0),
        created_at TEXT NOT NULL,
        CHECK(typeof(registry_version)='text' AND length(registry_version) BETWEEN 1 AND 128)
    )
    """,
    "INSERT INTO diagnostic_rule_versions(registry_version,rule_count,created_at) VALUES('diagnostic-rules-v1',18,'2026-08-03T00:00:00+00:00')",
    "CREATE TRIGGER tr_diagnostic_rule_versions_immutable_update BEFORE UPDATE ON diagnostic_rule_versions BEGIN SELECT RAISE(ABORT,'diagnostic rule version is immutable'); END",
    "CREATE TRIGGER tr_diagnostic_rule_versions_immutable_delete BEFORE DELETE ON diagnostic_rule_versions BEGIN SELECT RAISE(ABORT,'diagnostic rule version is immutable'); END",
    f"""
    CREATE TABLE diagnostic_reports (
        diagnostic_id TEXT NOT NULL PRIMARY KEY CHECK({_typed_id_check("diagnostic_id", "diagnostic_")}),
        owner_id TEXT NOT NULL CHECK(typeof(owner_id)='text' AND length(owner_id) BETWEEN 1 AND 256),
        registry_version TEXT NOT NULL REFERENCES diagnostic_rule_versions(registry_version) ON DELETE RESTRICT,
        subject_uri TEXT NOT NULL CHECK(typeof(subject_uri)='text' AND length(subject_uri) BETWEEN 1 AND 2048),
        subject_kind TEXT NOT NULL CHECK(subject_kind IN ('job','server')),
        job_id TEXT,
        server_id TEXT,
        classification TEXT NOT NULL CHECK({_safe_identifier_check("classification")}),
        rule_id TEXT NOT NULL CHECK(typeof(rule_id)='text' AND length(rule_id) BETWEEN 1 AND 128 AND rule_id NOT GLOB '*[^A-Za-z0-9_.-]*'),
        retryable INTEGER NOT NULL CHECK(typeof(retryable)='integer' AND retryable IN (0,1)),
        evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json) AND json_type(evidence_json)='object' AND length(CAST(evidence_json AS BLOB))<=65536),
        safe_actions_json TEXT NOT NULL CHECK(json_valid(safe_actions_json) AND json_type(safe_actions_json)='array' AND json_array_length(safe_actions_json)<=32 AND length(CAST(safe_actions_json AS BLOB))<=32768),
        approval_actions_json TEXT NOT NULL CHECK(json_valid(approval_actions_json) AND json_type(approval_actions_json)='array' AND json_array_length(approval_actions_json)<=32 AND length(CAST(approval_actions_json AS BLOB))<=32768),
        created_at TEXT NOT NULL CHECK(typeof(created_at)='text' AND length(created_at)>0),
        resource_uri TEXT NOT NULL UNIQUE CHECK(resource_uri='comfyui://diagnostics/'||diagnostic_id),
        FOREIGN KEY(job_id,owner_id) REFERENCES jobs(job_id,owner_id) ON DELETE RESTRICT,
        CHECK((subject_kind='job' AND job_id IS NOT NULL AND server_id IS NULL AND subject_uri='comfyui://jobs/'||job_id) OR
              (subject_kind='server' AND job_id IS NULL AND server_id IS NOT NULL AND subject_uri='comfyui://servers/'||server_id)),
        CHECK(server_id IS NULL OR {_safe_identifier_check("server_id")}),
        UNIQUE(diagnostic_id,owner_id),
        UNIQUE(owner_id,subject_uri,diagnostic_id)
    )
    """,
    "CREATE INDEX ix_diagnostic_reports_owner_created ON diagnostic_reports(owner_id,created_at DESC,diagnostic_id DESC)",
    "CREATE INDEX ix_diagnostic_reports_owner_subject ON diagnostic_reports(owner_id,subject_uri,created_at DESC,diagnostic_id DESC)",
    """
    CREATE TRIGGER tr_diagnostic_reports_shape_insert BEFORE INSERT ON diagnostic_reports
    WHEN json_type(NEW.evidence_json,'$.status')!='text'
      OR length(COALESCE(json_extract(NEW.evidence_json,'$.status'),''))>128
      OR json_type(NEW.evidence_json,'$.failed_node')!='object'
      OR json_type(NEW.evidence_json,'$.events')!='array'
      OR json_array_length(NEW.evidence_json,'$.events')>32
      OR json_type(NEW.evidence_json,'$.log_window')!='array'
      OR json_array_length(NEW.evidence_json,'$.log_window')>32
      OR EXISTS(SELECT 1 FROM json_each(NEW.evidence_json,'$.log_window') WHERE type!='text' OR length(CAST(value AS BLOB))>2048)
      OR EXISTS(SELECT 1 FROM json_each(NEW.evidence_json,'$.events') WHERE type!='object' OR json_type(value,'$.event_type')!='text' OR json_type(value,'$.occurred_at')!='text' OR json_type(value,'$.message')!='text' OR length(CAST(json_extract(value,'$.message') AS BLOB))>2048)
      OR EXISTS(SELECT 1 FROM json_each(NEW.safe_actions_json) WHERE type!='object' OR json_type(value,'$.tool')!='text' OR json_extract(value,'$.tool') NOT LIKE 'comfyui.%' OR json_type(value,'$.name')!='text' OR json_type(value,'$.required_arguments')!='object' OR json_extract(value,'$.risk')!='safe')
      OR EXISTS(SELECT 1 FROM json_each(NEW.approval_actions_json) WHERE type!='object' OR json_type(value,'$.tool')!='text' OR json_extract(value,'$.tool') NOT LIKE 'comfyui.%' OR json_type(value,'$.name')!='text' OR json_type(value,'$.required_arguments')!='object' OR json_extract(value,'$.risk')!='approval_required')
      OR EXISTS(SELECT 1 FROM json_tree(NEW.safe_actions_json) WHERE lower(COALESCE(key,'')) IN ('command','shell','cli','argv') OR (type='text' AND lower(CAST(value AS TEXT)) LIKE '%comfyui-skill%'))
      OR EXISTS(SELECT 1 FROM json_tree(NEW.approval_actions_json) WHERE lower(COALESCE(key,'')) IN ('command','shell','cli','argv') OR (type='text' AND lower(CAST(value AS TEXT)) LIKE '%comfyui-skill%'))
    BEGIN SELECT RAISE(ABORT,'diagnostic evidence or action shape is invalid'); END
    """,
    "CREATE TRIGGER tr_diagnostic_reports_owner_insert BEFORE INSERT ON diagnostic_reports WHEN NEW.job_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM jobs WHERE job_id=NEW.job_id AND owner_id=NEW.owner_id) BEGIN SELECT RAISE(ABORT,'diagnostic report owner conflict'); END",
    "CREATE TRIGGER tr_diagnostic_reports_immutable_update BEFORE UPDATE ON diagnostic_reports BEGIN SELECT RAISE(ABORT,'diagnostic report is immutable'); END",
    "CREATE TRIGGER tr_diagnostic_reports_immutable_delete BEFORE DELETE ON diagnostic_reports BEGIN SELECT RAISE(ABORT,'diagnostic report is immutable'); END",
    f"""
    CREATE TABLE repair_plans (
        repair_plan_id TEXT NOT NULL PRIMARY KEY CHECK({_typed_id_check("repair_plan_id", "repair_plan_")}),
        plan_digest TEXT NOT NULL CHECK({_sha256_check("plan_digest")}),
        owner_id TEXT NOT NULL CHECK(typeof(owner_id)='text' AND length(owner_id) BETWEEN 1 AND 256),
        resource_uri TEXT NOT NULL UNIQUE CHECK(resource_uri='comfyui://plans/'||repair_plan_id),
        original_job_id TEXT NOT NULL CHECK({_typed_id_check("original_job_id", "job_")}),
        workflow_id TEXT NOT NULL CHECK({_workflow_id_check("workflow_id")}),
        server_id TEXT NOT NULL CHECK({_safe_identifier_check("server_id")}),
        pinned_plan_id TEXT NOT NULL CHECK({_typed_id_check("pinned_plan_id", "plan_")}),
        pinned_revision_id TEXT NOT NULL CHECK({_typed_id_check("pinned_revision_id", "revision_")}),
        pinned_deployment_id TEXT NOT NULL CHECK({_typed_id_check("pinned_deployment_id", "deployment_")}),
        pinned_content_digest TEXT NOT NULL CHECK({_sha256_check("pinned_content_digest")}),
        original_arguments_json TEXT NOT NULL CHECK(json_valid(original_arguments_json) AND json_type(original_arguments_json)='object' AND length(CAST(original_arguments_json AS BLOB))<=1048576),
        original_arguments_digest TEXT NOT NULL CHECK({_sha256_check("original_arguments_digest")}),
        normalized_changes_json TEXT NOT NULL CHECK(json_valid(normalized_changes_json) AND json_type(normalized_changes_json)='object' AND length(CAST(normalized_changes_json AS BLOB))<=262144),
        resulting_arguments_json TEXT NOT NULL CHECK(json_valid(resulting_arguments_json) AND json_type(resulting_arguments_json)='object' AND length(CAST(resulting_arguments_json AS BLOB))<=1048576),
        resulting_arguments_digest TEXT NOT NULL CHECK({_sha256_check("resulting_arguments_digest")}),
        diff_json TEXT NOT NULL CHECK(json_valid(diff_json) AND json_type(diff_json)='array' AND json_array_length(diff_json)<=10000 AND length(CAST(diff_json AS BLOB))<=1048576),
        status TEXT NOT NULL CHECK(status='planned'),
        created_at TEXT NOT NULL CHECK(typeof(created_at)='text' AND length(created_at)>0),
        expires_at TEXT NOT NULL CHECK(
            typeof(expires_at)='text'
            AND substr(created_at,-6)='+00:00' AND substr(expires_at,-6)='+00:00'
            AND strftime('%s',created_at) IS NOT NULL AND strftime('%s',expires_at) IS NOT NULL
            AND CAST(strftime('%s',expires_at) AS INTEGER)=CAST(strftime('%s',created_at) AS INTEGER)+3600
            AND (CASE WHEN instr(created_at,'.')>0 THEN substr(created_at,instr(created_at,'.')+1,length(created_at)-instr(created_at,'.')-6) ELSE '' END)
                =(CASE WHEN instr(expires_at,'.')>0 THEN substr(expires_at,instr(expires_at,'.')+1,length(expires_at)-instr(expires_at,'.')-6) ELSE '' END)
        ),
        FOREIGN KEY(original_job_id,owner_id,workflow_id,server_id) REFERENCES jobs(job_id,owner_id,workflow_id,server_id) ON DELETE RESTRICT,
        FOREIGN KEY(pinned_plan_id,workflow_id,pinned_revision_id,pinned_deployment_id) REFERENCES execution_plans(plan_id,workflow_id,revision_id,deployment_id) ON DELETE RESTRICT,
        FOREIGN KEY(workflow_id,pinned_revision_id,pinned_content_digest) REFERENCES workflow_revisions(workflow_id,revision_id,content_digest) ON DELETE RESTRICT,
        FOREIGN KEY(pinned_deployment_id,workflow_id,pinned_revision_id,server_id) REFERENCES workflow_deployments(deployment_id,workflow_id,revision_id,server_id) ON DELETE RESTRICT,
        UNIQUE(repair_plan_id,owner_id,plan_digest),
        UNIQUE(owner_id,original_job_id,plan_digest)
    )
    """,
    "CREATE INDEX ix_repair_plans_owner_created ON repair_plans(owner_id,created_at DESC,repair_plan_id DESC)",
    "CREATE INDEX ix_repair_plans_owner_subject ON repair_plans(owner_id,original_job_id,created_at DESC,repair_plan_id DESC)",
    "CREATE INDEX ix_repair_plans_expiry ON repair_plans(expires_at,repair_plan_id)",
    """
    CREATE TRIGGER tr_repair_plans_source_insert BEFORE INSERT ON repair_plans
    WHEN NOT EXISTS(
        SELECT 1 FROM jobs JOIN execution_plans ON execution_plans.plan_id=jobs.plan_id
        WHERE jobs.job_id=NEW.original_job_id AND jobs.owner_id=NEW.owner_id
          AND jobs.workflow_id=NEW.workflow_id AND jobs.server_id=NEW.server_id
          AND jobs.plan_id=NEW.pinned_plan_id AND jobs.revision_id=NEW.pinned_revision_id
          AND jobs.deployment_id=NEW.pinned_deployment_id
          AND json(json_extract(execution_plans.resolved_inputs_json,'$.arguments'))=json(NEW.original_arguments_json)
    )
    BEGIN SELECT RAISE(ABORT,'repair plan original snapshot binding is invalid'); END
    """,
    """
    CREATE TRIGGER tr_repair_plans_diff_insert BEFORE INSERT ON repair_plans
    WHEN
      (SELECT count(*) FROM json_each(NEW.diff_json))!=(SELECT count(*) FROM json_each(NEW.normalized_changes_json))
      OR EXISTS(SELECT 1 FROM json_each(NEW.diff_json) GROUP BY json_extract(value,'$.path') HAVING count(*)!=1)
      OR EXISTS(
        SELECT 1 FROM json_each(NEW.normalized_changes_json) AS change
        LEFT JOIN json_each(NEW.resulting_arguments_json) AS result ON result.key=change.key
        WHERE result.key IS NULL OR result.type!=change.type OR result.value IS NOT change.value
      )
      OR EXISTS(
        SELECT 1 FROM json_each(NEW.original_arguments_json) AS original
        LEFT JOIN json_each(NEW.normalized_changes_json) AS change ON change.key=original.key
        LEFT JOIN json_each(NEW.resulting_arguments_json) AS result ON result.key=original.key
        WHERE change.key IS NULL AND (result.key IS NULL OR result.type!=original.type OR result.value IS NOT original.value)
      )
      OR EXISTS(
        SELECT 1 FROM json_each(NEW.resulting_arguments_json) AS result
        LEFT JOIN json_each(NEW.original_arguments_json) AS original ON original.key=result.key
        LEFT JOIN json_each(NEW.normalized_changes_json) AS change ON change.key=result.key
        WHERE original.key IS NULL AND change.key IS NULL
      )
      OR EXISTS(
        SELECT 1 FROM json_each(NEW.diff_json) AS item
        LEFT JOIN json_each(NEW.normalized_changes_json) AS change
          ON json_extract(item.value,'$.path')='/arguments/'||replace(replace(change.key,'~','~0'),'/','~1')
        LEFT JOIN json_each(NEW.original_arguments_json) AS original ON original.key=change.key
        WHERE change.key IS NULL OR json_type(item.value)!='object'
          OR json_type(item.value,'$.path')!='text' OR json_type(item.value,'$.operation')!='text'
          OR json_type(item.value,'$.after')!=change.type OR json_extract(item.value,'$.after') IS NOT change.value
          OR (original.key IS NULL AND (json_extract(item.value,'$.operation')!='add' OR json_type(item.value,'$.before')!='null'))
          OR (original.key IS NOT NULL AND (json_type(item.value,'$.before')!=original.type OR json_extract(item.value,'$.before') IS NOT original.value
              OR json_extract(item.value,'$.operation')!=CASE WHEN original.type=change.type AND original.value IS change.value THEN 'unchanged' ELSE 'replace' END))
      )
    BEGIN SELECT RAISE(ABORT,'repair plan diff does not exactly describe resulting snapshot'); END
    """,
    "CREATE TRIGGER tr_repair_plans_immutable_update BEFORE UPDATE ON repair_plans BEGIN SELECT RAISE(ABORT,'repair plan is immutable'); END",
    """
    CREATE TABLE repair_plan_commit_intents (
        repair_plan_id TEXT NOT NULL PRIMARY KEY,
        owner_id TEXT NOT NULL,
        plan_digest TEXT NOT NULL,
        reserved_at TEXT NOT NULL,
        FOREIGN KEY(repair_plan_id,owner_id,plan_digest) REFERENCES repair_plans(repair_plan_id,owner_id,plan_digest) ON DELETE RESTRICT,
        UNIQUE(repair_plan_id,owner_id,plan_digest)
    )
    """,
    """
    CREATE TRIGGER tr_repair_plan_commit_intents_expiry_insert BEFORE INSERT ON repair_plan_commit_intents
    WHEN NOT EXISTS(
        SELECT 1 FROM repair_plans WHERE repair_plan_id=NEW.repair_plan_id
          AND owner_id=NEW.owner_id AND plan_digest=NEW.plan_digest
          AND julianday(expires_at)>julianday(NEW.reserved_at)
    ) BEGIN SELECT RAISE(ABORT,'repair plan commit intent is invalid or expired'); END
    """,
    "CREATE TRIGGER tr_repair_plan_commit_intents_immutable_update BEFORE UPDATE ON repair_plan_commit_intents BEGIN SELECT RAISE(ABORT,'repair plan commit intent is immutable'); END",
    "CREATE TRIGGER tr_repair_plan_commit_intents_immutable_delete BEFORE DELETE ON repair_plan_commit_intents BEGIN SELECT RAISE(ABORT,'repair plan commit intent is immutable'); END",
    """
    CREATE TABLE repair_plan_commits (
        repair_plan_id TEXT NOT NULL PRIMARY KEY,
        owner_id TEXT NOT NULL,
        plan_digest TEXT NOT NULL,
        original_job_id TEXT NOT NULL,
        retry_job_id TEXT NOT NULL UNIQUE,
        result_job_uri TEXT NOT NULL UNIQUE,
        committed_at TEXT NOT NULL,
        FOREIGN KEY(repair_plan_id,owner_id,plan_digest) REFERENCES repair_plans(repair_plan_id,owner_id,plan_digest) ON DELETE RESTRICT,
        FOREIGN KEY(original_job_id,owner_id) REFERENCES jobs(job_id,owner_id) ON DELETE RESTRICT,
        FOREIGN KEY(retry_job_id,owner_id) REFERENCES jobs(job_id,owner_id) ON DELETE RESTRICT,
        CHECK(original_job_id!=retry_job_id),
        CHECK(result_job_uri='comfyui://jobs/'||retry_job_id),
        UNIQUE(repair_plan_id,owner_id,plan_digest,retry_job_id)
    )
    """,
    "CREATE INDEX ix_repair_plan_commits_owner_time ON repair_plan_commits(owner_id,committed_at DESC,repair_plan_id DESC)",
    """
    CREATE TRIGGER tr_repair_plan_commits_binding_insert BEFORE INSERT ON repair_plan_commits
    WHEN NOT EXISTS(
        SELECT 1 FROM repair_plans AS repair
        JOIN repair_plan_commit_intents AS intent
          ON intent.repair_plan_id=repair.repair_plan_id AND intent.owner_id=repair.owner_id
         AND intent.plan_digest=repair.plan_digest
        JOIN jobs AS retry ON retry.job_id=NEW.retry_job_id AND retry.owner_id=NEW.owner_id
        JOIN execution_plans AS result_plan ON result_plan.plan_id=retry.plan_id
        WHERE repair.repair_plan_id=NEW.repair_plan_id AND repair.owner_id=NEW.owner_id
          AND repair.plan_digest=NEW.plan_digest AND repair.original_job_id=NEW.original_job_id
          AND julianday(intent.reserved_at)<julianday(repair.expires_at)
          AND retry.job_id!=repair.original_job_id AND retry.workflow_id=repair.workflow_id
          AND retry.server_id=repair.server_id AND retry.revision_id=repair.pinned_revision_id
          AND retry.deployment_id=repair.pinned_deployment_id
          AND (retry.retry_of IS NULL OR retry.retry_of=repair.original_job_id)
          AND json(json_extract(result_plan.resolved_inputs_json,'$.arguments'))=json(repair.resulting_arguments_json)
    )
    BEGIN SELECT RAISE(ABORT,'repair plan commit aggregate binding is invalid or expired'); END
    """,
    "CREATE TRIGGER tr_repair_plan_commits_immutable_update BEFORE UPDATE ON repair_plan_commits BEGIN SELECT RAISE(ABORT,'repair plan commit evidence is immutable'); END",
    "CREATE TRIGGER tr_repair_plan_commits_immutable_delete BEFORE DELETE ON repair_plan_commits BEGIN SELECT RAISE(ABORT,'repair plan commit evidence is immutable'); END",
    "DROP TRIGGER tr_jobs_execution_identity_immutable",
    """
    CREATE TRIGGER tr_jobs_execution_identity_immutable BEFORE UPDATE OF
        job_id,workflow_id,plan_id,revision_id,deployment_id,owner_id,
        created_at,created_at_source,legacy_migrated,execution_origin
    ON jobs BEGIN SELECT RAISE(ABORT,'job execution identity is immutable'); END
    """,
    """
    CREATE TRIGGER tr_jobs_retry_cycle_insert BEFORE INSERT ON jobs
    WHEN NEW.retry_of IS NOT NULL AND (NEW.retry_of=NEW.job_id OR EXISTS(
        WITH RECURSIVE lineage(job_id,retry_of) AS (
            SELECT job_id,retry_of FROM jobs WHERE job_id=NEW.retry_of AND owner_id=NEW.owner_id
            UNION ALL SELECT jobs.job_id,jobs.retry_of FROM jobs JOIN lineage ON jobs.job_id=lineage.retry_of WHERE jobs.owner_id=NEW.owner_id
        ) SELECT 1 FROM lineage WHERE retry_of=NEW.job_id
    )) BEGIN SELECT RAISE(ABORT,'retry-of cycle is forbidden'); END
    """,
    """
    CREATE TRIGGER tr_jobs_retry_lineage_append BEFORE UPDATE OF retry_of ON jobs
    WHEN NOT(OLD.retry_of IS NULL AND NEW.retry_of IS NOT NULL AND NEW.retry_of!=NEW.job_id AND EXISTS(
        SELECT 1 FROM repair_plan_commits AS commits JOIN repair_plans AS repair ON repair.repair_plan_id=commits.repair_plan_id
        WHERE commits.retry_job_id=NEW.job_id AND commits.owner_id=NEW.owner_id
          AND commits.original_job_id=NEW.retry_of AND repair.workflow_id=NEW.workflow_id
          AND repair.server_id=NEW.server_id AND repair.pinned_revision_id=NEW.revision_id
          AND repair.pinned_deployment_id=NEW.deployment_id
    ))
    BEGIN SELECT RAISE(ABORT,'job retry lineage is append-once and commit-bound'); END
    """,
)

_PHASE_M_EXPERIMENTS_UP = (
    _PHASE_M_EXPERIMENT_AGGREGATES_UP
    + _PHASE_M_EXPERIMENT_VARIANTS_UP
    + _PHASE_M_EXPERIMENT_RATINGS_UP
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
    SchemaMigration(
        5,
        "phase-j-workflow-change-plans",
        _PHASE_J_WORKFLOW_CHANGE_UP,
        (),
        feasibility_note="forward-only immutable workflow change plan storage",
    ),
    SchemaMigration(
        6,
        "phase-l-asset-library",
        _PHASE_L_ASSET_LIBRARY_UP,
        (),
        feasibility_note="forward-only owner-bound asset and artifact lifecycle storage",
    ),
    SchemaMigration(
        7,
        "phase-m-experiments",
        _PHASE_M_EXPERIMENTS_UP,
        (),
        feasibility_note="forward-only owner-bound durable Experiment and Variant storage",
    ),
    SchemaMigration(
        8,
        "phase-n-diagnostic-recovery",
        _PHASE_N_DIAGNOSTIC_RECOVERY_UP,
        (),
        feasibility_note="forward-only owner-bound structured diagnostic and retry recovery storage",
    ),
    SchemaMigration(
        9,
        "phase-o-server-config-provisioning",
        PHASE_O_PROVISIONING_UP,
        (),
        feasibility_note="forward-only owner-bound server, config, approval, and provisioning storage",
    ),
    SchemaMigration(
        10,
        "phase-k-routing-plans",
        PHASE_K_ROUTING_UP,
        (),
        feasibility_note="forward-only owner-bound multi-server routing plan storage",
    ),
    SchemaMigration(
        11,
        "phase-j-workflow-change-hardening",
        PHASE_J_HARDENING_UP,
        (),
        feasibility_note="forward-only immutable actor-bound Workflow change plans",
    ),
    SchemaMigration(
        12,
        "phase-q-routing-commit-idempotency",
        PHASE_Q_ROUTING_COMMIT_UP,
        (),
        feasibility_note="forward-only owner-bound routing commit idempotency fencing",
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
            if (
                connection.execute("SELECT 1 FROM schema_migrations WHERE version=6").fetchone()
                is not None
            ):
                from comfyui_mcp_skills.infrastructure.persistence.execution_plan_input_backfill import (
                    backfill_execution_plan_inputs,
                )

                backfill_execution_plan_inputs(connection)
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
