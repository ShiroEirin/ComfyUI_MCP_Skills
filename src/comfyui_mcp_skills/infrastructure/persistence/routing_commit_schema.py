"""Forward-only Phase Q routing commit idempotency schema."""

# ruff: noqa: E501

PHASE_Q_ROUTING_COMMIT_UP = (
    """CREATE TABLE routing_commit_idempotency(
        owner_id TEXT NOT NULL,
        idempotency_digest TEXT NOT NULL CHECK(length(idempotency_digest)=64 AND idempotency_digest NOT GLOB '*[^0-9a-f]*'),
        plan_id TEXT NOT NULL,
        plan_digest TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(owner_id,idempotency_digest),
        UNIQUE(plan_id,owner_id),
        FOREIGN KEY(plan_id,plan_digest,owner_id) REFERENCES routing_plans(plan_id,plan_digest,owner_id) ON DELETE RESTRICT
    ) WITHOUT ROWID""",
    "CREATE TRIGGER tr_routing_commit_idempotency_immutable_update BEFORE UPDATE ON routing_commit_idempotency BEGIN SELECT RAISE(ABORT,'routing commit idempotency is immutable'); END",
    "CREATE TRIGGER tr_routing_commit_idempotency_immutable_delete BEFORE DELETE ON routing_commit_idempotency BEGIN SELECT RAISE(ABORT,'routing commit idempotency is retained'); END",
)

__all__ = ["PHASE_Q_ROUTING_COMMIT_UP"]
