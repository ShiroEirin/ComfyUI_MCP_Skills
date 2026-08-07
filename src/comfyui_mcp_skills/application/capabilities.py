"""G6 capability catalog and bounded Tool Inventory contracts."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from comfyui_mcp_skills.application.authorization import AuthorizationContext, Scope, Toolset
from comfyui_mcp_skills.application.compatibility import HostCapabilities, host_fallbacks

PROJECT_ICON_SRC = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
    "%3Crect width='64' height='64' rx='14' fill='%231f2937'/%3E"
    "%3Cpath d='M17 21h30v22H17z' fill='none' stroke='%2360a5fa' stroke-width='5'/%3E"
    "%3Cpath d='M25 14v10M39 14v10M25 40v10M39 40v10' stroke='%23f8fafc' stroke-width='4'/%3E"
    "%3C/svg%3E"
)


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    name: str
    title: str
    summary: str
    toolsets: frozenset[Toolset]
    required_scopes: frozenset[Scope]
    risk: RiskLevel
    keywords: tuple[str, ...] = ()

    def summary_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "summary": self.summary,
            "toolsets": sorted(toolset.value for toolset in self.toolsets),
            "required_scopes": sorted(scope.value for scope in self.required_scopes),
            "risk": self.risk.value,
        }


_ALL_TOOLSETS = frozenset(Toolset)
_ALL_SCOPES = frozenset(Scope)

CAPABILITY_SPECS: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(
        "comfyui.capability.search",
        "Search ComfyUI capabilities",
        "Find authorized capabilities without changing the active Tool list.",
        _ALL_TOOLSETS,
        _ALL_SCOPES,
        RiskLevel.LOW,
        ("discover", "tool", "inventory"),
    ),
    CapabilitySpec(
        "comfyui.capability.describe",
        "Describe a ComfyUI capability",
        "Read one authorized capability schema, risk, Toolset, and Host fallback contract.",
        _ALL_TOOLSETS,
        _ALL_SCOPES,
        RiskLevel.LOW,
        ("discover", "schema", "fallback"),
    ),
    CapabilitySpec(
        "comfyui.asset.upload",
        "Upload an input asset",
        "Upload authorized local media for workflow execution.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.MEDIUM,
        ("image", "audio", "video", "mask"),
    ),
    CapabilitySpec(
        "comfyui.asset.list",
        "List assets",
        "List owner-visible assets with filters and cursor pagination.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.LOW,
        ("library", "media", "collection", "cursor"),
    ),
    CapabilitySpec(
        "comfyui.asset.describe",
        "Describe an asset",
        "Read owner-bound media, provenance, reference, and retention facts.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.LOW,
        ("metadata", "retention", "reference", "provenance"),
    ),
    CapabilitySpec(
        "comfyui.asset.collection.update",
        "Update an asset collection",
        "Add or remove owned assets from a named collection.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.MEDIUM,
        ("collection", "organize", "add", "remove"),
    ),
    CapabilitySpec(
        "comfyui.asset.metadata.extract",
        "Extract asset metadata",
        "Extract safe generation and workflow references from owned media.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.LOW,
        ("png", "generation", "workflow", "metadata"),
    ),
    CapabilitySpec(
        "comfyui.asset.import_output",
        "Import an output artifact",
        "Reuse or materialize an owned Artifact as a workflow input.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.MEDIUM,
        ("artifact", "reuse", "direct", "upload"),
    ),
    CapabilitySpec(
        "comfyui.asset.delete.plan",
        "Plan asset deletion",
        "Create a digest-bound deletion impact plan for an owned asset.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.MEDIUM,
        ("delete", "plan", "reference", "lineage"),
    ),
    CapabilitySpec(
        "comfyui.asset.delete.commit",
        "Commit asset deletion",
        "Commit a digest-bound owned asset deletion plan after rechecking references.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.HIGH,
        ("delete", "commit", "destructive"),
    ),
    CapabilitySpec(
        "comfyui.asset.transfer.plan",
        "Plan artifact transfer",
        "Create a digest-bound cross-server transfer plan for an owned Artifact.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.MEDIUM,
        ("artifact", "transfer", "server", "plan"),
    ),
    CapabilitySpec(
        "comfyui.asset.transfer.commit",
        "Commit artifact transfer",
        "Commit a digest-bound owned Artifact transfer plan.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.MEDIUM,
        ("artifact", "transfer", "commit"),
    ),
    CapabilitySpec(
        "comfyui.asset.transfer.get",
        "Get artifact transfer",
        "Read one owner-bound Artifact transfer state.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.LOW,
        ("artifact", "transfer", "status"),
    ),
    CapabilitySpec(
        "comfyui.job.get",
        "Get job status",
        "Read durable job status and output Resource Links.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.LOW,
        ("status", "output", "result"),
    ),
    CapabilitySpec(
        "comfyui.job.cancel",
        "Cancel a queued job",
        "Cancel an owned queued job without globally interrupting ComfyUI.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.HIGH,
        ("stop", "queue"),
    ),
    CapabilitySpec(
        "comfyui.job.list",
        "List durable jobs",
        "List owner-bound durable jobs with filters and keyset pagination.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.LOW,
        ("history", "status", "workflow", "cursor"),
    ),
    CapabilitySpec(
        "comfyui.job.diagnose",
        "Diagnose a failed Job",
        "Generate one owner-bound structured Diagnostic Report.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.LOW,
        ("diagnose", "failure", "evidence"),
    ),
    CapabilitySpec(
        "comfyui.job.retry.plan",
        "Plan a Job retry",
        "Create an owner-bound digest-bound retry repair plan.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.MEDIUM,
        ("retry", "repair", "plan", "diff"),
    ),
    CapabilitySpec(
        "comfyui.job.retry.commit",
        "Commit a Job retry",
        "Create one new Job from an unexpired digest-bound repair plan.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.MEDIUM,
        ("retry", "repair", "commit"),
    ),
    CapabilitySpec(
        "comfyui.server.diagnose",
        "Diagnose a server",
        "Generate one bounded structured server Diagnostic Report.",
        frozenset({Toolset.OPERATIONS, Toolset.AUTHORING}),
        frozenset({Scope.OBSERVE}),
        RiskLevel.LOW,
        ("diagnose", "server", "health"),
    ),
    CapabilitySpec(
        "comfyui.server.list",
        "List ComfyUI servers",
        "List configured servers without credentials or private URLs.",
        frozenset({Toolset.OPERATIONS}),
        frozenset({Scope.OBSERVE}),
        RiskLevel.LOW,
        ("discover", "instances"),
    ),
    CapabilitySpec(
        "comfyui.server.health",
        "Check server health",
        "Probe ComfyUI runtime and device health.",
        frozenset({Toolset.OPERATIONS}),
        frozenset({Scope.OBSERVE}),
        RiskLevel.LOW,
        ("online", "gpu", "status"),
    ),
    CapabilitySpec(
        "comfyui.queue.list",
        "List the server queue",
        "Read bounded running and pending queue entries without prompt payloads.",
        frozenset({Toolset.OPERATIONS}),
        frozenset({Scope.OBSERVE}),
        RiskLevel.LOW,
        ("running", "pending", "observe"),
    ),
    CapabilitySpec(
        "comfyui.log.read",
        "Read server logs",
        "Read a bounded cursor window of redacted ComfyUI log lines.",
        frozenset({Toolset.OPERATIONS}),
        frozenset({Scope.OBSERVE}),
        RiskLevel.LOW,
        ("diagnose", "failure", "redacted"),
    ),
    CapabilitySpec(
        "comfyui.server.capabilities",
        "Inspect server capabilities",
        "Inspect optional API states without treating optional failures as server outages.",
        frozenset({Toolset.OPERATIONS}),
        frozenset({Scope.OBSERVE}),
        RiskLevel.LOW,
        ("optional", "api", "manager", "version"),
    ),
    CapabilitySpec(
        "comfyui.template.list",
        "List workflow templates",
        "List redacted workflow template summaries with cursor pagination.",
        frozenset({Toolset.OPERATIONS}),
        frozenset({Scope.OBSERVE}),
        RiskLevel.LOW,
        ("userdata", "workflow", "template"),
    ),
    CapabilitySpec(
        "comfyui.subgraph.list",
        "List global subgraphs",
        "List redacted global subgraph summaries with cursor pagination.",
        frozenset({Toolset.OPERATIONS}),
        frozenset({Scope.OBSERVE}),
        RiskLevel.LOW,
        ("userdata", "component", "template"),
    ),
    CapabilitySpec(
        "comfyui.subgraph.get",
        "Get a global subgraph",
        "Read one redacted global subgraph summary without its workflow graph payload.",
        frozenset({Toolset.OPERATIONS}),
        frozenset({Scope.OBSERVE}),
        RiskLevel.LOW,
        ("userdata", "component", "summary"),
    ),
    CapabilitySpec(
        "comfyui.server.free",
        "Free server memory",
        "Unload models or free runtime memory and report the audited impact.",
        frozenset({Toolset.OPERATIONS}),
        frozenset({Scope.OPERATE}),
        RiskLevel.HIGH,
        ("gpu", "vram", "unload", "memory"),
    ),
    CapabilitySpec(
        "comfyui.node.list",
        "List ComfyUI nodes",
        "Search installed node classes with cursor pagination.",
        frozenset({Toolset.OPERATIONS, Toolset.AUTHORING, Toolset.ADMIN}),
        frozenset({Scope.OBSERVE}),
        RiskLevel.LOW,
        ("search", "class"),
    ),
    CapabilitySpec(
        "comfyui.node.blueprint",
        "Project a goal-driven node blueprint",
        "Keyword-matched node classes with bounded input signatures and advertised options.",
        frozenset({Toolset.OPERATIONS, Toolset.AUTHORING, Toolset.ADMIN}),
        frozenset({Scope.OBSERVE}),
        RiskLevel.LOW,
        ("blueprint", "node", "target", "inputs"),
    ),
    CapabilitySpec(
        "comfyui.engine.history",
        "Read engine run history",
        "Flat projection of engine-side history records with a bounded response.",
        frozenset({Toolset.OPERATIONS, Toolset.AUTHORING, Toolset.ADMIN}),
        frozenset({Scope.OBSERVE}),
        RiskLevel.LOW,
        ("history", "run", "prompt"),
    ),
    CapabilitySpec(
        "comfyui.node.describe",
        "Describe a ComfyUI node",
        "Read the complete definition of one installed node class.",
        frozenset({Toolset.OPERATIONS, Toolset.AUTHORING, Toolset.ADMIN}),
        frozenset({Scope.OBSERVE}),
        RiskLevel.LOW,
        ("schema", "input", "output"),
    ),
    CapabilitySpec(
        "comfyui.model.list",
        "List ComfyUI models",
        "Search model folders and server-side model names.",
        frozenset({Toolset.OPERATIONS, Toolset.AUTHORING, Toolset.ADMIN}),
        frozenset({Scope.OBSERVE}),
        RiskLevel.LOW,
        ("checkpoint", "lora", "vae"),
    ),
    CapabilitySpec(
        "comfyui.revision.list",
        "List workflow revisions",
        "List immutable revisions for a workflow.",
        frozenset({Toolset.AUTHORING}),
        frozenset({Scope.OBSERVE, Scope.AUTHOR}),
        RiskLevel.LOW,
        ("history", "version"),
    ),
    CapabilitySpec(
        "comfyui.revision.diff",
        "Compare workflow revisions",
        "Return bounded graph, parameter, dependency, and output contract differences.",
        frozenset({Toolset.AUTHORING}),
        frozenset({Scope.OBSERVE, Scope.AUTHOR}),
        RiskLevel.LOW,
        ("revision", "diff", "change"),
    ),
    CapabilitySpec(
        "comfyui.workflow.describe",
        "Describe a workflow",
        "Read a published workflow Revision and Deployment summary.",
        frozenset({Toolset.AUTHORING}),
        frozenset({Scope.OBSERVE, Scope.AUTHOR}),
        RiskLevel.LOW,
        ("schema", "deployment", "revision"),
    ),
    CapabilitySpec(
        "comfyui.workflow.list",
        "List workflows",
        "List configured workflows with filtering and cursor pagination.",
        frozenset({Toolset.AUTHORING}),
        frozenset({Scope.OBSERVE}),
        RiskLevel.LOW,
        ("discover", "workflow", "catalog"),
    ),
    CapabilitySpec(
        "comfyui.workflow.dependencies.check",
        "Check workflow dependencies",
        "Compare one published Revision's bounded node and model contract to a server.",
        frozenset({Toolset.AUTHORING}),
        frozenset({Scope.OBSERVE, Scope.AUTHOR}),
        RiskLevel.LOW,
        ("dependency", "model", "node", "readiness"),
    ),
    CapabilitySpec(
        "comfyui.experiment.plan",
        "Plan an experiment",
        "Expand bounded workflow variants and calculate conservative budgets before persistence.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.MEDIUM,
        ("experiment", "matrix", "zip", "sample", "variant", "budget"),
    ),
    CapabilitySpec(
        "comfyui.experiment.commit",
        "Commit an experiment",
        "Commit an owned digest-bound experiment plan and return its Resource link.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.MEDIUM,
        ("experiment", "commit", "execute", "batch"),
    ),
    CapabilitySpec(
        "comfyui.experiment.get",
        "Get an experiment",
        "Read one owner-bound experiment summary without inlining its variants.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.LOW,
        ("experiment", "status", "summary", "resource"),
    ),
    CapabilitySpec(
        "comfyui.experiment.cancel",
        "Cancel an experiment",
        "Stop new work for an owned experiment using an explicit cancellation mode.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.HIGH,
        ("experiment", "cancel", "queued", "stop"),
    ),
    CapabilitySpec(
        "comfyui.experiment.variant.list",
        "List experiment variants",
        "List bounded owner-visible experiment variants with keyset pagination.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.LOW,
        ("experiment", "variant", "result", "cursor"),
    ),
    CapabilitySpec(
        "comfyui.experiment.variant.rate",
        "Rate an experiment variant",
        "Record bounded rubric-versioned numeric scores for one owned variant.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.MEDIUM,
        ("experiment", "variant", "rating", "rubric", "score"),
    ),
    CapabilitySpec(
        "comfyui.experiment.variant.promote",
        "Promote an experiment variant",
        "Promote one owned variant to a preset or immutable unpublished Revision.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.MEDIUM,
        ("experiment", "variant", "preset", "revision", "promote"),
    ),
    CapabilitySpec(
        "comfyui.admin.workflow.import",
        "Preview or commit workflow import",
        "Validate API or Editor JSON and optionally create an unpublished Revision.",
        frozenset({Toolset.ADMIN}),
        frozenset({Scope.CONFIGURE}),
        RiskLevel.MEDIUM,
        ("workflow", "import", "preview", "revision"),
    ),
    CapabilitySpec(
        "comfyui.admin.workflow.change.plan",
        "Plan workflow graph changes",
        "Validate bounded domain operations and return a structured change diff.",
        frozenset({Toolset.ADMIN}),
        frozenset({Scope.CONFIGURE}),
        RiskLevel.MEDIUM,
        ("workflow", "change", "plan", "diff"),
    ),
    CapabilitySpec(
        "comfyui.admin.workflow.change.commit",
        "Commit workflow graph changes",
        "Create one immutable unpublished Revision from a bound change plan.",
        frozenset({Toolset.ADMIN}),
        frozenset({Scope.CONFIGURE}),
        RiskLevel.MEDIUM,
        ("workflow", "change", "commit", "revision"),
    ),
    CapabilitySpec(
        "comfyui.admin.workflow.publish",
        "Publish workflow deployment",
        "Atomically switch the published Deployment for one server.",
        frozenset({Toolset.ADMIN}),
        frozenset({Scope.CONFIGURE}),
        RiskLevel.HIGH,
        ("workflow", "publish", "deployment"),
    ),
    CapabilitySpec(
        "comfyui.admin.workflow.rollback",
        "Rollback workflow deployment",
        "Create and publish a new immutable Revision from a historical target.",
        frozenset({Toolset.ADMIN}),
        frozenset({Scope.CONFIGURE}),
        RiskLevel.HIGH,
        ("workflow", "rollback", "revision"),
    ),
    CapabilitySpec(
        "comfyui.admin.workflow.set_enabled",
        "Set workflow availability",
        "Enable or disable one configured workflow with an idempotent request ID.",
        frozenset({Toolset.ADMIN}),
        frozenset({Scope.CONFIGURE}),
        RiskLevel.MEDIUM,
        ("enable", "disable", "configuration"),
    ),
    CapabilitySpec(
        "comfyui.admin.workflow.validate",
        "Validate a workflow",
        "Validate one workflow graph, parameters, nodes, and dependencies without executing.",
        frozenset({Toolset.ADMIN}),
        frozenset({Scope.CONFIGURE}),
        RiskLevel.LOW,
        ("workflow", "validate", "schema"),
    ),
    CapabilitySpec(
        "comfyui.admin.workflow.delete",
        "Delete a workflow permanently",
        "Permanently delete one workflow after exact confirmation.",
        frozenset({Toolset.ADMIN}),
        frozenset({Scope.CONFIGURE}),
        RiskLevel.HIGH,
        ("delete", "destructive"),
    ),
    CapabilitySpec(
        "comfyui.admin.audit.get",
        "Get an admin audit outcome",
        "Read durable commit and audit status for one admin request.",
        frozenset({Toolset.ADMIN}),
        frozenset({Scope.AUDIT}),
        RiskLevel.LOW,
        ("audit", "status"),
    ),
    CapabilitySpec(
        "comfyui.admin.audit.retry",
        "Retry pending audit delivery",
        "Retry a pending audit outcome without repeating its operation.",
        frozenset({Toolset.ADMIN}),
        frozenset({Scope.AUDIT}),
        RiskLevel.MEDIUM,
        ("audit", "recovery"),
    ),
    CapabilitySpec(
        "comfyui.admin.audit.export",
        "Export the admin audit trail",
        "Export a bounded, filterable slice of durable admin audit events in append order.",
        frozenset({Toolset.ADMIN}),
        frozenset({Scope.AUDIT}),
        RiskLevel.LOW,
        ("audit", "export", "trail"),
    ),
    *tuple(
        CapabilitySpec(
            name,
            name.rsplit(".", 1)[-1].replace("_", " ").title(),
            "Owner-bound Phase O admin control-plane operation with bounded projections.",
            frozenset({Toolset.ADMIN}),
            frozenset(
                {Scope.PROVISION, Scope.AUDIT}
                if name == "comfyui.admin.approval.get"
                else {Scope.PROVISION}
                if "dependency" in name or "approval" in name or "provisioning" in name
                else {Scope.CONFIGURE}
            ),
            RiskLevel.HIGH
            if name
            in {
                "comfyui.admin.server.upsert",
                "comfyui.admin.server.set_enabled",
                "comfyui.admin.server.set_default",
                "comfyui.admin.server.delete",
                "comfyui.admin.config.import",
                "comfyui.admin.dependency.install",
                "comfyui.admin.provisioning.cancel",
            }
            or name.endswith("commit")
            else RiskLevel.LOW,
            ("server", "config", "dependency", "approval", "provisioning"),
        )
        for name in (
            "comfyui.admin.server.list",
            "comfyui.admin.server.inspect",
            "comfyui.admin.server.upsert",
            "comfyui.admin.server.set_enabled",
            "comfyui.admin.server.set_default",
            "comfyui.admin.server.delete",
            "comfyui.admin.config.export",
            "comfyui.admin.config.import",
            "comfyui.admin.dependency.inspect",
            "comfyui.admin.dependency.plan",
            "comfyui.admin.dependency.install",
            "comfyui.admin.approval.get",
            "comfyui.admin.approval.decision.plan",
            "comfyui.admin.approval.decision.commit",
            "comfyui.admin.provisioning.get",
            "comfyui.admin.provisioning.cancel",
        )
    ),
    CapabilitySpec(
        "comfyui.execution.plan",
        "Plan multi-server execution",
        "Select one compatible Deployment and persist an immutable routing plan.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.LOW,
        ("route", "plan", "server", "policy"),
    ),
    CapabilitySpec(
        "comfyui.execution.commit",
        "Commit planned execution",
        "Submit exactly the Deployment pinned by a digest-bound routing plan.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.MEDIUM,
        ("route", "commit", "job", "digest"),
    ),
    CapabilitySpec(
        "comfyui.route.explain",
        "Explain a routing plan",
        "Read the owner-bound candidate set, exclusions, score, and selection reason.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.LOW,
        ("route", "explain", "candidate", "score"),
    ),
    CapabilitySpec(
        "comfyui.policy.evaluate",
        "Evaluate execution policy",
        "Evaluate arguments against bounded execution policy without submitting work.",
        frozenset({Toolset.EXECUTION}),
        frozenset({Scope.EXECUTE}),
        RiskLevel.LOW,
        ("policy", "evaluate", "allow", "deny"),
    ),
    CapabilitySpec(
        "comfyui.queue.remove",
        "Remove queued Jobs",
        "Preview or remove an explicit owner-safe set of queued Jobs.",
        frozenset({Toolset.OPERATIONS}),
        frozenset({Scope.OPERATE}),
        RiskLevel.MEDIUM,
        ("queue", "remove", "cancel"),
    ),
    CapabilitySpec(
        "comfyui.queue.clear",
        "Clear a server queue",
        "Preview affected Jobs before an explicit global queue clear.",
        frozenset({Toolset.OPERATIONS}),
        frozenset({Scope.OPERATE}),
        RiskLevel.HIGH,
        ("queue", "clear", "global"),
    ),
    CapabilitySpec(
        "comfyui.server.interrupt",
        "Interrupt a server",
        "Preview running Jobs before an explicit global ComfyUI interrupt.",
        frozenset({Toolset.OPERATIONS}),
        frozenset({Scope.OPERATE}),
        RiskLevel.HIGH,
        ("server", "interrupt", "global"),
    ),
    CapabilitySpec(
        "comfyui.runtime.restart.plan",
        "Plan a runtime restart",
        "Return restart impact, approval requirements, and controller availability.",
        frozenset({Toolset.OPERATIONS}),
        frozenset({Scope.OPERATE}),
        RiskLevel.HIGH,
        ("runtime", "restart", "approval", "impact"),
    ),
)

CAPABILITY_BY_NAME = {spec.name: spec for spec in CAPABILITY_SPECS}


class _NamedTool(Protocol):
    name: str


class ToolInventory:
    """Validate and bound one endpoint's stable active Tool surface."""

    DEFAULT_FIXED_LIMIT = 16
    HARD_FIXED_LIMIT = 32
    DYNAMIC_LIMIT = 8
    HARD_DYNAMIC_LIMIT = 128

    def __init__(
        self,
        fixed: Iterable[_NamedTool],
        *,
        max_fixed_limit: int = DEFAULT_FIXED_LIMIT,
        max_dynamic_limit: int = DYNAMIC_LIMIT,
    ) -> None:
        if type(max_fixed_limit) is not int or not 1 <= max_fixed_limit <= self.HARD_FIXED_LIMIT:
            raise ValueError("max_fixed_limit must be between 1 and 32")
        if (
            type(max_dynamic_limit) is not int
            or not 1 <= max_dynamic_limit <= self.HARD_DYNAMIC_LIMIT
        ):
            raise ValueError("max_dynamic_limit must be between 1 and 128")
        names = tuple(tool.name for tool in fixed)
        if len(names) != len(set(names)):
            raise ValueError("fixed Tool names must be unique")
        if len(names) > max_fixed_limit:
            raise ValueError(f"fixed Toolset exceeds its limit of {max_fixed_limit}")
        self._fixed_names = names
        self._max_dynamic_limit = max_dynamic_limit

    @property
    def fixed_names(self) -> tuple[str, ...]:
        return self._fixed_names

    @property
    def fixed_count(self) -> int:
        return len(self._fixed_names)

    def select_dynamic(self, names: Iterable[str]) -> tuple[str, ...]:
        return tuple(sorted(set(names))[: self._max_dynamic_limit])


class CapabilityCatalog:
    """Search immutable capability metadata visible to one authorization context."""

    def __init__(self, specifications: Iterable[CapabilitySpec]) -> None:
        ordered = tuple(sorted(specifications, key=lambda item: item.name))
        if len({item.name for item in ordered}) != len(ordered):
            raise ValueError("capability names must be unique")
        self._specifications = ordered
        self._by_name = {item.name: item for item in ordered}

    @classmethod
    def default(cls) -> CapabilityCatalog:
        return cls(CAPABILITY_SPECS)

    @staticmethod
    def _visible(spec: CapabilitySpec, authorization: AuthorizationContext) -> bool:
        return authorization.toolset in spec.toolsets and bool(
            authorization.scopes & spec.required_scopes
        )

    def visible_names(self, authorization: AuthorizationContext) -> tuple[str, ...]:
        return tuple(
            spec.name for spec in self._specifications if self._visible(spec, authorization)
        )

    def search(
        self,
        query: str,
        authorization: AuthorizationContext,
        *,
        limit: int = 10,
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("limit must be an integer between 1 and 50")
        terms = tuple(part.casefold() for part in query.split() if part)
        ranked: list[tuple[int, CapabilitySpec]] = []
        for spec in self._specifications:
            if not self._visible(spec, authorization):
                continue
            text = " ".join((spec.name, spec.title, spec.summary, *spec.keywords)).casefold()
            score = sum(term in text for term in terms)
            if terms and score == 0:
                continue
            ranked.append((score, spec))
        ranked.sort(key=lambda item: (-item[0], item[1].name))
        return {
            "items": [spec.summary_dict() for _score, spec in ranked[:limit]],
            "total": len(ranked),
        }

    def describe(
        self,
        name: str,
        authorization: AuthorizationContext,
        *,
        host: HostCapabilities | None = None,
    ) -> dict[str, Any]:
        spec = self._by_name.get(name)
        if spec is None or not self._visible(spec, authorization):
            raise PermissionError("capability is unavailable")
        result = spec.summary_dict()
        result["icon"] = {"src": PROJECT_ICON_SRC, "mime_type": "image/svg+xml"}
        result["fallbacks"] = host_fallbacks(host or HostCapabilities())
        return result
