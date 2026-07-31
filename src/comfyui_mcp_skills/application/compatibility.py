"""G6 versioned Compatibility Matrix and MCP Host fallback policy."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HostCapabilities:
    elicitation: bool = False
    subscriptions: bool = False
    tasks: bool = False
    apps: bool = False


def host_fallbacks(capabilities: HostCapabilities) -> dict[str, str]:
    """Return stable interaction paths without pretending optional Host support exists."""

    return {
        "elicitation": "native" if capabilities.elicitation else "approval_resource",
        "subscriptions": "native" if capabilities.subscriptions else "resource_refetch",
        "tasks": "native" if capabilities.tasks else "submitted_job_resource",
        "apps": "native" if capabilities.apps else "resource_link",
    }


@dataclass(frozen=True, slots=True)
class CompatibilityCell:
    cell_id: str
    component: str
    version: str
    capabilities: tuple[str, ...]
    scenarios: tuple[str, ...]
    evidence_level: str
    status: str
    last_verified_commit: str
    evidence_id: str


# v0.29.2 and Manager 4.2.2 were the current upstream tags when G6 was defined.
# Cells stay "implemented" until their gate reaches the real-integration evidence layer.
COMPATIBILITY_MATRIX: tuple[CompatibilityCell, ...] = (
    CompatibilityCell(
        "comfyui-minimum",
        "comfyui",
        "v0.3.0",
        ("prompt", "queue", "history", "object_info"),
        ("legacy prompt submission", "output refetch", "optional endpoint 404"),
        "contract",
        "planned",
        "150ab4b",
        "upstream-version-pin",
    ),
    CompatibilityCell(
        "comfyui-latest",
        "comfyui",
        "v0.29.2",
        ("prompt", "queue", "history", "object_info", "userdata"),
        ("submission", "upload", "progress", "resource refetch"),
        "contract",
        "planned",
        "150ab4b",
        "pending-latest-contract",
    ),
    CompatibilityCell(
        "manager-absent",
        "manager",
        "none",
        ("manager_unavailable",),
        ("core execution remains available", "provisioning stays unavailable"),
        "unit",
        "implemented",
        "working-tree",
        "tests/test_g6_catalog_eval.py::test_compatibility_matrix_covers_required_axes_and_evidence_levels",
    ),
    CompatibilityCell(
        "manager-supported",
        "manager",
        "4.2.2",
        ("manager",),
        ("version probe", "explicit provisioning availability"),
        "contract",
        "planned",
        "150ab4b",
        "upstream-manager-tag",
    ),
    CompatibilityCell(
        "prompt-legacy",
        "comfyui-api",
        "traditional-/prompt",
        ("prompt", "client_id_reconciliation"),
        ("unknown submission recovery", "running cancel rejected"),
        "unit",
        "implemented",
        "working-tree",
        "tests/test_g5_orchestrator.py",
    ),
    CompatibilityCell(
        "jobs-api",
        "comfyui-api",
        "optional-/api/jobs",
        ("jobs_api", "targeted_cancel"),
        ("capability-gated submit", "atomic targeted cancel"),
        "contract",
        "planned",
        "150ab4b",
        "pending-jobs-api-contract",
    ),
    CompatibilityCell(
        "mcp-optional-supported",
        "mcp-host",
        "2026-07-28+optional",
        ("elicitation", "subscriptions", "tasks", "apps"),
        ("native approval", "resource updates", "task handle", "app view"),
        "none",
        "planned",
        "150ab4b",
        "pending-mcp-optional-client",
    ),
    CompatibilityCell(
        "mcp-fallback-client",
        "mcp-host",
        "2026-07-28-core-only",
        (),
        (
            "approval Resource",
            "Resource refetch",
            "submitted Job Resource",
            "Resource Link",
        ),
        "unit",
        "implemented",
        "working-tree",
        "tests/test_g6_catalog_eval.py::test_host_fallbacks_are_explicit_for_every_optional_feature",
    ),
    CompatibilityCell(
        "sqlite-stdio",
        "deployment",
        "sqlite-stdio-single-process",
        ("sqlite", "stdio", "lease_recovery"),
        ("restart", "expired lease reclaim", "outbox redelivery"),
        "contract",
        "implemented",
        "working-tree",
        "tests/test_g5_orchestrator.py",
    ),
    CompatibilityCell(
        "postgres-two-worker",
        "deployment",
        "postgresql-two-worker",
        ("postgresql", "multi_worker"),
        ("lease fencing", "owner isolation", "outbox concurrency"),
        "real-integration",
        "planned",
        "150ab4b",
        "pending-postgresql-two-worker",
    ),
)
