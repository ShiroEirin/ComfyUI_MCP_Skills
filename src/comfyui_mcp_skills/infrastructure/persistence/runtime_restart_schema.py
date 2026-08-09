"""Phase G2 runtime restart execution schema (v13)."""

from __future__ import annotations

G2_RESTART_UP = (
    # Single-state restart session: plan -> approve -> drain -> restart -> terminal.
    # The approved impact snapshot is immutable (approval basis); the execution
    # impact intent is persisted before the external controller call so crash
    # recovery can reconstruct an honest receipt.
    """
    CREATE TABLE runtime_restart_plans (
        plan_id TEXT NOT NULL PRIMARY KEY CHECK(
            typeof(plan_id) = 'text' AND substr(plan_id, 1, 13) = 'runtime_plan_'
            AND length(plan_id) = 45 AND substr(plan_id, 14) NOT GLOB '*[^0-9a-f]*'
        ),
        approval_id TEXT NOT NULL UNIQUE CHECK(
            typeof(approval_id) = 'text' AND substr(approval_id, 1, 17) = 'runtime_approval_'
            AND length(approval_id) = 49 AND substr(approval_id, 18) NOT GLOB '*[^0-9a-f]*'
        ),
        owner_id TEXT NOT NULL CHECK(
            typeof(owner_id) = 'text' AND length(owner_id) BETWEEN 1 AND 256
        ),
        server_id TEXT NOT NULL CHECK(
            typeof(server_id) = 'text' AND length(server_id) BETWEEN 1 AND 128
            AND substr(server_id, 1, 1) GLOB '[A-Za-z0-9]'
            AND server_id NOT GLOB '*[^A-Za-z0-9_-]*'
        ),
        plan_digest TEXT NOT NULL CHECK(
            typeof(plan_digest) = 'text' AND length(plan_digest) = 64
            AND plan_digest NOT GLOB '*[^0-9a-f]*'
        ),
        approved_impact_summary_json TEXT NOT NULL CHECK(
            json_valid(approved_impact_summary_json)
            AND json_type(approved_impact_summary_json) = 'object'
            AND length(CAST(approved_impact_summary_json AS BLOB)) <= 4096
        ),
        status TEXT NOT NULL CHECK(status IN (
            'planned', 'approved', 'rejected', 'draining', 'restarting',
            'completed', 'failed', 'expired'
        )),
        approval_actor TEXT CHECK(
            approval_actor IS NULL OR length(approval_actor) BETWEEN 1 AND 256
        ),
        approval_reason TEXT NOT NULL DEFAULT '' CHECK(length(approval_reason) <= 512),
        approval_decided_at TEXT,
        approval_expires_at TEXT NOT NULL CHECK(
            julianday(approval_expires_at) > julianday(created_at)
        ),
        controller_binding_json TEXT NOT NULL CHECK(
            json_valid(controller_binding_json)
            AND json_type(controller_binding_json) = 'object'
            AND length(CAST(controller_binding_json AS BLOB)) <= 4096
        ),
        controller_binding_digest TEXT NOT NULL CHECK(
            typeof(controller_binding_digest) = 'text' AND length(controller_binding_digest) = 64
            AND controller_binding_digest NOT GLOB '*[^0-9a-f]*'
        ),
        controller_available INTEGER NOT NULL CHECK(controller_available IN (0, 1)),
        execution_impact_summary_json TEXT CHECK(
            execution_impact_summary_json IS NULL OR (
                json_valid(execution_impact_summary_json)
                AND json_type(execution_impact_summary_json) = 'object'
                AND length(CAST(execution_impact_summary_json AS BLOB)) <= 4096
            )
        ),
        execution_impact_digest TEXT CHECK(
            execution_impact_digest IS NULL OR (
                typeof(execution_impact_digest) = 'text'
                AND length(execution_impact_digest) = 64
                AND execution_impact_digest NOT GLOB '*[^0-9a-f]*'
            )
        ),
        execution_intent_committed_at TEXT,
        commit_request_id TEXT CHECK(
            commit_request_id IS NULL OR length(commit_request_id) BETWEEN 1 AND 128
        ),
        commit_result_json TEXT CHECK(
            commit_result_json IS NULL OR (
                json_valid(commit_result_json)
                AND length(CAST(commit_result_json AS BLOB)) <= 8192
            )
        ),
        committed_at TEXT,
        error TEXT CHECK(error IS NULL OR length(error) <= 512),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT NOT NULL CHECK(julianday(expires_at) > julianday(created_at)),
        completed_at TEXT,
        resource_uri TEXT NOT NULL CHECK(resource_uri = 'comfyui://plans/' || plan_id),
        CHECK(
            (status IN ('approved', 'draining', 'restarting', 'completed', 'failed')
             AND approval_actor IS NOT NULL AND approval_decided_at IS NOT NULL)
            OR status IN ('planned', 'rejected', 'expired')
        ),
        CHECK(
            committed_at IS NULL OR status IN ('draining', 'restarting', 'completed', 'failed')
        ),
        CHECK(completed_at IS NULL OR status IN ('completed', 'failed', 'expired', 'rejected')),
        CHECK(
            commit_result_json IS NOT NULL OR status NOT IN ('completed', 'failed')
        ),
        CHECK(
            execution_impact_summary_json IS NULL
            OR status NOT IN ('planned', 'approved', 'rejected', 'expired')
        ),
        UNIQUE(plan_id, owner_id),
        UNIQUE(plan_id, commit_request_id)
    )
    """,
    """
    CREATE UNIQUE INDEX ux_runtime_restart_active
    ON runtime_restart_plans(server_id)
    WHERE status IN ('draining', 'restarting')
    """,
    """
    CREATE INDEX ix_runtime_restart_owner_created
    ON runtime_restart_plans(owner_id, created_at DESC, plan_id)
    """,
    # Normalized impact rows: the sole full snapshot; the plan digest is the
    # canonical encoding of these rows (no byte ceiling conflict).
    """
    CREATE TABLE runtime_restart_impact_jobs (
        plan_id TEXT NOT NULL REFERENCES runtime_restart_plans(plan_id) ON DELETE RESTRICT,
        job_id TEXT NOT NULL CHECK(
            typeof(job_id) = 'text' AND substr(job_id, 1, 4) = 'job_'
            AND length(job_id) IN (36, 68)
            AND substr(job_id, 5) NOT GLOB '*[^0-9a-f]*'
        ),
        owner_id TEXT NOT NULL CHECK(
            typeof(owner_id) = 'text' AND length(owner_id) BETWEEN 1 AND 256
        ),
        status TEXT NOT NULL CHECK(length(status) > 0),
        ordinal INTEGER NOT NULL CHECK(typeof(ordinal) = 'integer' AND ordinal >= 0),
        PRIMARY KEY(plan_id, job_id),
        UNIQUE(plan_id, ordinal)
    )
    """,
    """
    CREATE INDEX ix_runtime_restart_impact_plan
    ON runtime_restart_impact_jobs(plan_id)
    """,
    # Per-submission admission records: the atomic gate rows drained before an
    # external restart command runs. Rows are cleaned by the submit finally
    # contract and by startup recovery (single-instance premise).
    """
    CREATE TABLE runtime_submission_admissions (
        admission_id TEXT NOT NULL PRIMARY KEY CHECK(
            typeof(admission_id) = 'text' AND substr(admission_id, 1, 10) = 'admission_'
            AND length(admission_id) = 42 AND substr(admission_id, 11) NOT GLOB '*[^0-9a-f]*'
        ),
        server_id TEXT NOT NULL CHECK(
            typeof(server_id) = 'text' AND length(server_id) BETWEEN 1 AND 128
            AND substr(server_id, 1, 1) GLOB '[A-Za-z0-9]'
            AND server_id NOT GLOB '*[^A-Za-z0-9_-]*'
        ),
        created_at TEXT NOT NULL,
        UNIQUE(server_id, admission_id)
    )
    """,
    """
    CREATE INDEX ix_runtime_admissions_server
    ON runtime_submission_admissions(server_id, created_at)
    """,
)
