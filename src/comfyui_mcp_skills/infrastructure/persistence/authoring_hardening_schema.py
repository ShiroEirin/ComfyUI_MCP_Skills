"""Forward-only hardening for immutable owner-bound Workflow change plans."""

# ruff: noqa: E501

PHASE_J_HARDENING_UP = (
    """CREATE TRIGGER tr_workflow_change_plans_identity_immutable BEFORE UPDATE OF
        plan_id,plan_digest,workflow_id,server_id,base_revision_id,operations_json,
        graph_json,parameter_schema_json,dependency_contract_json,content_digest,
        diff_json,actor,created_at,expires_at ON workflow_change_plans
        BEGIN SELECT RAISE(ABORT,'workflow change plan identity is immutable'); END""",
    """CREATE TRIGGER tr_workflow_change_plans_commit_guard BEFORE UPDATE OF committed_revision_id ON workflow_change_plans
        WHEN OLD.committed_revision_id IS NOT NULL OR NEW.committed_revision_id IS NULL
        BEGIN SELECT RAISE(ABORT,'workflow change plan commit transition is invalid'); END""",
    """CREATE TRIGGER tr_workflow_change_plans_no_delete BEFORE DELETE ON workflow_change_plans
        BEGIN SELECT RAISE(ABORT,'workflow change plan is retained'); END""",
    "CREATE INDEX ix_workflow_change_plans_actor ON workflow_change_plans(actor,created_at DESC,plan_id)",
)

__all__ = ["PHASE_J_HARDENING_UP"]
