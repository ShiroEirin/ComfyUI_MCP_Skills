"""Forward-only Phase K schema for owner-bound routing plans."""

# ruff: noqa: E501

PHASE_K_ROUTING_UP = (
    """CREATE TABLE routing_plans(
        plan_id TEXT PRIMARY KEY NOT NULL,
        owner_id TEXT NOT NULL,
        workflow_id TEXT NOT NULL,
        selected_server_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        deployment_id TEXT NOT NULL,
        plan_digest TEXT NOT NULL CHECK(length(plan_digest)=64 AND plan_digest NOT GLOB '*[^0-9a-f]*'),
        content_json TEXT NOT NULL CHECK(json_valid(content_json) AND json_type(content_json)='object' AND length(CAST(content_json AS BLOB))<=1048576),
        status TEXT NOT NULL CHECK(status IN('planned','committed')),
        job_id TEXT,
        created_at TEXT NOT NULL,
        committed_at TEXT,
        resource_uri TEXT NOT NULL UNIQUE,
        UNIQUE(plan_id,owner_id),
        UNIQUE(plan_id,plan_digest,owner_id),
        FOREIGN KEY(deployment_id,workflow_id,revision_id,selected_server_id)
          REFERENCES workflow_deployments(deployment_id,workflow_id,revision_id,server_id) ON DELETE RESTRICT,
        FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT,
        CHECK((status='planned' AND job_id IS NULL AND committed_at IS NULL) OR
              (status='committed' AND job_id IS NOT NULL AND committed_at IS NOT NULL))
    )""",
    "CREATE INDEX ix_routing_plans_owner ON routing_plans(owner_id,created_at DESC,plan_id DESC)",
    """CREATE TRIGGER tr_routing_plans_identity_immutable BEFORE UPDATE OF
        plan_id,owner_id,workflow_id,selected_server_id,revision_id,deployment_id,
        plan_digest,content_json,created_at,resource_uri ON routing_plans
        BEGIN SELECT RAISE(ABORT,'routing plan identity is immutable'); END""",
    """CREATE TRIGGER tr_routing_plans_transition_guard BEFORE UPDATE OF status,job_id,committed_at ON routing_plans
        WHEN OLD.status!='planned' OR NEW.status!='committed' OR NEW.job_id IS NULL OR NEW.committed_at IS NULL
        BEGIN SELECT RAISE(ABORT,'routing plan transition is invalid'); END""",
    "CREATE TRIGGER tr_routing_plans_no_delete BEFORE DELETE ON routing_plans BEGIN SELECT RAISE(ABORT,'routing plan is retained'); END",
)

__all__ = ["PHASE_K_ROUTING_UP"]
