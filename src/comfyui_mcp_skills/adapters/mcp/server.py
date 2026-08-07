"""Low-level MCP server with dynamic workflow schemas and durable jobs."""

from __future__ import annotations

import logging
import math
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
from mcp.server import Server, ServerRequestContext
from mcp.server.subscriptions import (
    InMemorySubscriptionBus,
    ListenHandler,
    SubscriptionBus,
)
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    CompleteRequestParams,
    CompleteResult,
    Completion,
    ListToolsResult,
    PaginatedRequestParams,
    Tool,
    ToolAnnotations,
)
from mcp_types import INVALID_PARAMS

from comfyui_mcp_skills import __version__
from comfyui_mcp_skills.adapters.mcp.orchestration import OrchestrationRuntime
from comfyui_mcp_skills.adapters.mcp.prompts import create_prompt_handlers
from comfyui_mcp_skills.adapters.mcp.resources import create_resource_handlers
from comfyui_mcp_skills.adapters.mcp.subscriptions import WorkflowChangeMonitor
from comfyui_mcp_skills.adapters.mcp.tooling import (
    EXECUTION_PROPERTY,
    JOB_SCHEMA,
    UI_EXTENSION_ID,
    bounded_integer,
    client_supports_apps,
    current_owner,
    current_scopes,
    decorate_tool,
    diagnostic_report_dict,
    experiment_dict,
    fixed_tools,
    job_dict,
    optional_boolean,
    optional_string,
    phase_h_tools,
    phase_k_tools,
    phase_l_tools,
    phase_m_tools,
    phase_n_tools,
    promotion_dict,
    rating_dict,
    repair_plan_dict,
    required_object,
    required_string,
    tool_result,
    validate_fixed_arguments,
    variant_page_dict,
    with_ui_metadata,
    workflow_tool_names,
)
from comfyui_mcp_skills.application.admin import JsonlAuditLog
from comfyui_mcp_skills.application.asset_library import AssetLibraryService
from comfyui_mcp_skills.application.assets import AssetService
from comfyui_mcp_skills.application.authorization import (
    AuthorizationContext,
    Scope,
    Toolset,
    is_authorized,
    scopes_for_resource,
    tool_visible,
)
from comfyui_mcp_skills.application.capabilities import (
    CAPABILITY_SPECS,
    CapabilityCatalog,
    ToolInventory,
)
from comfyui_mcp_skills.application.catalog import WorkflowCatalog
from comfyui_mcp_skills.application.discovery import DiscoveryService
from comfyui_mcp_skills.application.execution import ExecutionService
from comfyui_mcp_skills.application.experiment_orchestration import ExperimentAdvanceHandler
from comfyui_mcp_skills.application.experiments import ExperimentService, get_experiment_variant
from comfyui_mcp_skills.application.jobs import JobService
from comfyui_mcp_skills.application.model_guidance import guidance as model_guidance
from comfyui_mcp_skills.application.observability import ObservationService
from comfyui_mcp_skills.application.orchestration import (
    ComfyUIReconcileProbe,
    JobReconciler,
    OperationOrchestrator,
    WorkHandler,
)
from comfyui_mcp_skills.application.planning import ExecutionPlanningService
from comfyui_mcp_skills.application.ports import ComfyUIGateway
from comfyui_mcp_skills.application.provisioning import ProvisioningWorkHandler
from comfyui_mcp_skills.application.provisioning_ports import ManagerGateway
from comfyui_mcp_skills.application.routing import RoutingService
from comfyui_mcp_skills.application.runtime_control import (
    RuntimeController,
    RuntimeControlService,
)
from comfyui_mcp_skills.application.servers import OwnerAwareServerRegistry, ServerRegistry
from comfyui_mcp_skills.application.suggestions import SuggestionService
from comfyui_mcp_skills.application.telemetry import (
    Meter,
    Tracer,
    meter_from_env,
    tracer_from_env,
)
from comfyui_mcp_skills.application.workflow_change import (
    WorkflowChangeService,
    _graph_mermaid,
)
from comfyui_mcp_skills.application.workflow_graph import (
    WorkflowGraphService,
    WorkflowValidationService,
)
from comfyui_mcp_skills.application.workflow_inspection import WorkflowInspectionService
from comfyui_mcp_skills.domain.control_plane import (
    parse_legacy_resource_uri,
    validate_control_plane_id,
)
from comfyui_mcp_skills.domain.errors import (
    AuditIdempotencyConflict,
    ComfyUISkillsError,
    ServerNotFound,
    WorkflowNotFound,
)
from comfyui_mcp_skills.domain.identifiers import validate_identifier
from comfyui_mcp_skills.domain.models import Workflow
from comfyui_mcp_skills.domain.orchestration import PROVISIONING_WORK_TYPE
from comfyui_mcp_skills.domain.workflow_schema import build_input_schema
from comfyui_mcp_skills.domain.workflow_semantics import (
    DependencyExtractorRegistry,
    ParameterRoleRegistry,
)
from comfyui_mcp_skills.infrastructure.comfyui.gateway import create_gateway
from comfyui_mcp_skills.infrastructure.persistence.orchestration import (
    SQLiteOrchestrationRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.repository_factory import (
    RepositoryBundle,
    create_repository_bundle,
)
from comfyui_mcp_skills.infrastructure.persistence.resource_aliases import (
    SQLiteLegacyResourceAliasReader,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_asset_library import (
    SQLiteAssetLibraryRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_routing import SQLiteRoutingRepository
from comfyui_mcp_skills.infrastructure.persistence.sqlite_workflows import SQLiteWorkflowRepository
from comfyui_mcp_skills.infrastructure.persistence.workflow_changes import (
    SQLiteWorkflowChangeRepository,
)

G3_AUTHORING_TOOLS = frozenset(
    {
        "comfyui.revision.list",
        "comfyui.revision.diff",
        "comfyui.workflow.describe",
        "comfyui.workflow.dependencies.check",
        "comfyui.workflow.visualize",
    }
)
PHASE_L_TOOL_NAMES = frozenset(
    {
        "comfyui.asset.list",
        "comfyui.asset.describe",
        "comfyui.asset.collection.update",
        "comfyui.asset.metadata.extract",
        "comfyui.asset.import_output",
        "comfyui.asset.delete.plan",
        "comfyui.asset.delete.commit",
        "comfyui.asset.transfer.plan",
        "comfyui.asset.transfer.commit",
        "comfyui.asset.transfer.get",
    }
)
PHASE_M_TOOL_NAMES = frozenset(
    {
        "comfyui.experiment.plan",
        "comfyui.experiment.commit",
        "comfyui.experiment.get",
        "comfyui.experiment.cancel",
        "comfyui.experiment.variant.list",
        "comfyui.experiment.variant.rate",
        "comfyui.experiment.variant.promote",
    }
)
PHASE_N_DIAGNOSTIC_TOOL_NAMES = frozenset({"comfyui.job.diagnose", "comfyui.server.diagnose"})
PHASE_N_RETRY_TOOL_NAMES = frozenset({"comfyui.job.retry.plan", "comfyui.job.retry.commit"})
PHASE_N_TOOL_NAMES = PHASE_N_DIAGNOSTIC_TOOL_NAMES | PHASE_N_RETRY_TOOL_NAMES
PHASE_K_TOOL_NAMES = frozenset(
    {
        "comfyui.execution.plan",
        "comfyui.execution.commit",
        "comfyui.route.explain",
        "comfyui.policy.evaluate",
    }
)


GatewayFactory = Callable[[dict[str, Any]], ComfyUIGateway]

def _free_locked(
    audit_log: JsonlAuditLog,
    request_id: str,
    actor: str,
    target: dict[str, object],
    observation: Any,
    server_id: str,
    unload_models: bool,
    free_memory: bool,
) -> dict[str, Any]:
    """Run server.free under a request-scoped lock: check, intent, execute, terminal.

    The whole sequence holds one FileLock per request_id so concurrent retries
    with the same id serialize: exactly one wins, the rest are rejected before
    any destructive action. A failed intent write aborts without executing; a
    failed terminal write raises an explicit audit error instead of returning a
    non-recoverable ``pending`` state. The lock filename is derived from the
    caller-supplied request_id via SHA-256 so it can never affect the path.
    """
    import hashlib

    from filelock import FileLock

    lock_key = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
    lock_path = f"{audit_log.path}.{lock_key}.lock"
    # Lock file lifecycle is owned by filelock: Windows release unlinks the
    # file; Unix deliberately keeps it (flock is inode-scoped and unlink would
    # race, py-filelock#31), so a stale .lock file is inert, bounded per
    # request_id, and never affects mutual exclusion. No explicit unlink here:
    # a failed acquire (timeout while a concurrent same-request_id call holds
    # the lock) never touches the holder's file, and the events_for check is
    # the real serialization guarantee.
    lock = FileLock(lock_path, timeout=10)
    with lock:
        if audit_log.events_for(request_id):
            raise AuditIdempotencyConflict(
                f"request_id {request_id} was already used for server.free; "
                "refusing to re-execute a destructive operation"
            )
        audit_log.append(
            _audit_event(actor, "server.free", target, "intent", request_id=request_id)
        )
        try:
            result = observation.free(
                server_id, unload_models=unload_models, free_memory=free_memory
            )
        except Exception as exc:
            try:
                audit_log.append(
                    _audit_event(
                        actor,
                        "server.free",
                        target,
                        "failure",
                        request_id=request_id,
                        error_code=type(exc).__name__,
                    )
                )
            except Exception as audit_exc:
                raise RuntimeError(
                    f"server.free backend failed ({type(exc).__name__}) and its audit "
                    f"terminal could not be persisted (request_id={request_id}): "
                    f"{audit_exc}"
                ) from exc
            raise
        try:
            audit_log.append(
                _audit_event(
                    actor, "server.free", target, "success", request_id=request_id
                )
            )
        except Exception as exc:
            raise RuntimeError(
                f"server.free executed but its audit terminal could not be "
                f"persisted (request_id={request_id}); refusing to claim audited"
            ) from exc
        result = dict(result)
        result["audit_status"] = "audited"
        result["request_id"] = request_id
        return result


def _audit_event(
    actor: str,
    action: str,
    target: dict[str, object],
    outcome: str,
    *,
    request_id: str = "",
    error_code: str = "",
) -> dict[str, object]:
    """Build one event compatible with the admin audit trail (admin-audit.jsonl)."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id or uuid.uuid4().hex,
        "actor": actor,
        "action": action,
        "target": target,
        "operation_key": action,
        "outcome": outcome,
        "error_code": error_code or None,
    }


def _portable_tool_name(name: str) -> str:
    """Project one canonical MCP tool name into provider-safe ASCII."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def _required_revision_id(deployment: dict[str, Any], workflow_id: str, server_id: str) -> str:
    revision_id = deployment.get("revision_id")
    if not isinstance(revision_id, str) or not revision_id:
        raise WorkflowNotFound(
            f"Workflow not found: {server_id}/{workflow_id}"
        )
    return revision_id


# ComfyUI status_str values we project; anything else maps to "unknown".
_KNOWN_HISTORY_STATUSES = frozenset({"success", "error", "running"})

# Output media keys counted by engine history projection (JobService contract).
_HISTORY_OUTPUT_MEDIA_KEYS = ("images", "gifs", "audio", "video")


def _engine_history_projection(
    gateway: Any,
    *,
    prompt_id: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    """Flat projection of engine /history into bounded prompt records.

    Engine history entries look like {prompt_id: {prompt, outputs,
    status: {completed, status_str}, ...}}; only the stable fields are
    projected so callers never see full prompt graphs or raw output metadata.
    Outputs_count counts generated media across output nodes (images, gifs,
    audio, video — the JobService output contract). Entries with a
    numeric/string created_at sort newest-first by that time; standard engine
    payloads carry no created_at and fall back to reverse engine insertion
    order (newest first).
    """
    raw = gateway.get_history_bounded(prompt_id=prompt_id, max_items=limit)
    if not isinstance(raw, dict):
        raise ValueError("engine history response must be an object")
    items: list[dict[str, Any]] = []
    for entry_id, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        outputs = entry.get("outputs")
        outputs_count = 0
        if isinstance(outputs, dict):
            for node_output in outputs.values():
                if not isinstance(node_output, dict):
                    continue
                for media_key in _HISTORY_OUTPUT_MEDIA_KEYS:
                    media = node_output.get(media_key)
                    if isinstance(media, list):
                        outputs_count += len(media)
        status = entry.get("status")
        if isinstance(status, dict):
            status = status.get("status_str")
        normalized_status = "unknown"
        if isinstance(status, str):
            candidate = status.strip().lower()
            if candidate in _KNOWN_HISTORY_STATUSES:
                normalized_status = candidate
        item: dict[str, Any] = {
            "prompt_id": str(entry_id),
            "status": normalized_status,
            "outputs_count": outputs_count,
        }
        created = entry.get("created_at")
        if isinstance(created, (int, float)) and not isinstance(created, bool):
            item["_time"] = created
        elif isinstance(created, str) and created:
            item["_time"] = created
        items.append(item)
    timed = [item for item in items if "_time" in item]
    untimed = [item for item in items if "_time" not in item]

    def _time_key(item: dict[str, Any]) -> tuple[int, float | str]:
        # Sort on the raw typed value before any stringification: floats and
        # negative timestamps must compare numerically, not lexicographically.
        value = item["_time"]
        if isinstance(value, (int, float)):
            return (0, float(value))
        return (1, value)  # ISO strings sort lexicographically

    timed.sort(key=_time_key, reverse=True)
    untimed.reverse()  # engine insertion order is oldest-first
    ordered = timed + untimed
    for item in ordered:
        if "_time" in item:
            item["created_at"] = str(item.pop("_time"))
    return {"items": ordered[:limit], "total": len(raw)}


logger = logging.getLogger(__name__)


def create_server(
    base_dir: Path,
    *,
    gateway_factory: GatewayFactory = create_gateway,
    manager_gateway: ManagerGateway | None = None,
    upload_roots: list[Path] | None = None,
    max_upload_bytes: int = 100 * 1024 * 1024,
    repositories: RepositoryBundle | None = None,
    authorization: AuthorizationContext | None = None,
    diagnostic_service: Any | None = None,
    subscription_bus: SubscriptionBus | None = None,
    retry_service: Any | None = None,
    owner_provider: Callable[[], str] | None = None,
    portable_tool_names: bool = False,
    max_dynamic_tools: int = ToolInventory.DYNAMIC_LIMIT,
    runtime_controller_provider: Callable[[str], RuntimeController | None] | None = None,
    tracer: Tracer | None = None,
    meter: Meter | None = None,
) -> Server[dict[str, object]]:
    """Create an MCP server backed by one configured project directory."""
    base_dir = base_dir.resolve()
    tracer = tracer or tracer_from_env()
    meter = meter or meter_from_env()
    tool_calls = meter.counter(
        "mcp.tool.calls", unit="{call}", description="MCP tool invocations"
    )
    tool_errors = meter.counter(
        "mcp.tool.errors", unit="{error}", description="MCP tool invocation failures"
    )
    tool_duration = meter.histogram(
        "mcp.tool.duration", unit="s", description="MCP tool invocation duration"
    )
    repositories = repositories or create_repository_bundle(base_dir)
    enforce_authorization = authorization is not None
    authorization = authorization or AuthorizationContext(
        "local-stdio", frozenset({Scope.EXECUTE}), Toolset.EXECUTION
    )
    workflow_repository = repositories.workflows
    suggestion_service = None
    if repositories.store is not None and repositories.run_store == "sqlite":
        suggestion_service = SuggestionService(repositories.store.path)
    if isinstance(workflow_repository, SQLiteWorkflowRepository) and repositories.store is not None:
        workflow_repository = SQLiteWorkflowRepository(
            repositories.store,
            owner_id=authorization.principal_id if owner_provider is None else None,
            owner_provider=owner_provider,
        )
    catalog = WorkflowCatalog(workflow_repository)
    global_servers = ServerRegistry(base_dir)
    run_repository = repositories.runs
    asset_repository = repositories.assets
    routing_repository = (
        SQLiteRoutingRepository(repositories.store)
        if repositories.store is not None
        and repositories.workflow_store == "sqlite"
        and repositories.run_store == "sqlite"
        else None
    )

    def request_owner_id() -> str:
        return owner_provider() if owner_provider is not None else authorization.principal_id

    def owner_server_connection(owner_id: str, server_id: str) -> dict[str, Any]:
        connection = (
            routing_repository.current_server_connection(owner_id, server_id)
            if routing_repository is not None
            else None
        )
        return global_servers.connection(server_id) if connection is None else connection

    servers = (
        OwnerAwareServerRegistry(
            global_servers,
            request_owner_id,
            routing_repository.current_server_connection,
        )
        if routing_repository is not None
        else global_servers
    )

    asset_library_repository = (
        (
            asset_repository
            if isinstance(asset_repository, SQLiteAssetLibraryRepository)
            else SQLiteAssetLibraryRepository(repositories.store)
        )
        if (repositories.store is not None and repositories.run_store == "sqlite")
        else None
    )
    asset_library = (
        AssetLibraryService(
            asset_library_repository,
            servers,
            gateway_factory,
            max_bytes=max_upload_bytes,
            staging_root=base_dir / "data" / "asset-staging",
            connection_provider=(
                routing_repository.current_server_connection
                if routing_repository is not None
                else None
            ),
        )
        if asset_library_repository is not None and repositories.asset_store == "sqlite"
        else None
    )
    assets = AssetService(
        asset_repository,
        upload_roots=upload_roots if upload_roots is not None else [base_dir / "uploads"],
        max_bytes=max_upload_bytes,
    )
    planning = (
        ExecutionPlanningService(repositories.store, workflow_repository)
        if repositories.store is not None
        and repositories.workflow_store == "sqlite"
        and repositories.run_store == "sqlite"
        else None
    )
    execution = ExecutionService(
        catalog,
        servers,
        run_repository,
        asset_repository,
        gateway_factory,
        planning=planning,
        artifacts=asset_library_repository,
    )
    jobs = JobService(
        servers,
        run_repository,
        gateway_factory,
        artifacts=asset_library_repository,
        connection_provider=(
            routing_repository.current_server_connection if routing_repository is not None else None
        ),
    )

    def probe_routing_context(context: dict[str, Any]) -> dict[str, Any]:
        snapshot = dict(context)
        try:
            connection = (
                routing_repository.resolve_server_connection(
                    str(context["owner_id"]),
                    str(context["server_id"]),
                    int(context["server_revision"]),
                    str(context["server_config_digest"]),
                )
                if routing_repository is not None
                else None
            )
            gateway = gateway_factory(
                servers.connection(str(context["server_id"])) if connection is None else connection
            )
            queue = gateway.get_queue()
            running = queue.get("queue_running", [])
            pending = queue.get("queue_pending", [])
            if not isinstance(running, list) or not isinstance(pending, list):
                raise ValueError("ComfyUI queue response is invalid")
            snapshot["queue_depth"] = len(running) + len(pending)
            statistics = gateway.get_system_stats()
            devices = statistics.get("devices", [])
            if isinstance(devices, list):
                free_values = [
                    value
                    for device in devices
                    if isinstance(device, dict)
                    for value in (device.get("vram_free", device.get("free_memory")),)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                ]
                if free_values:
                    snapshot["available_vram_bytes"] = max(free_values)
            snapshot["health_available"] = True
        except (ComfyUISkillsError, OSError, TypeError, ValueError):
            snapshot["health_available"] = False
        return snapshot

    routing = (
        RoutingService(routing_repository, execution, probe=probe_routing_context)
        if routing_repository is not None
        else None
    )
    runtime_controls = RuntimeControlService(
        servers,
        run_repository,
        gateway_factory,
        controller_provider=runtime_controller_provider,
    )
    discovery = DiscoveryService(servers, gateway_factory)
    observation = ObservationService(servers, gateway_factory)
    workflow_graphs = WorkflowGraphService(
        ParameterRoleRegistry.default(), DependencyExtractorRegistry.default()
    )
    workflow_inspection = (
        WorkflowInspectionService(workflow_repository, workflow_graphs, WorkflowValidationService())
        if repositories.workflow_store == "sqlite"
        else None
    )
    workflow_changes = (
        WorkflowChangeService(
            SQLiteWorkflowChangeRepository(repositories.store),
            workflow_graphs,
            WorkflowValidationService(),
            actor=authorization.principal_id,
        )
        if repositories.store is not None and repositories.workflow_store == "sqlite"
        else None
    )
    experiment_repository = repositories.experiments
    experiments_available = (
        experiment_repository is not None
        and repositories.store is not None
        and repositories.workflow_store == "sqlite"
        and repositories.run_store == "sqlite"
    )
    experiment_service: ExperimentService | None = None
    if experiments_available and experiment_repository is not None:
        experiment_service = ExperimentService(experiment_repository)
    diagnostic_repository = getattr(repositories, "diagnostics", None)
    if diagnostic_service is None and diagnostic_repository is not None:
        from comfyui_mcp_skills.application.diagnostics import DiagnosticService

        diagnostic_service = DiagnosticService(diagnostic_repository)
    retry_repository = getattr(repositories, "retries", None)
    if retry_service is None and retry_repository is not None:
        from comfyui_mcp_skills.application.diagnostics import RetryService

        retry_service = RetryService(retry_repository, execution)
    fixed_surface = [
        *fixed_tools(),
        *(tool for tool in phase_h_tools(include_phase_p=True)),
        *(phase_l_tools() if asset_library is not None else []),
        *(phase_k_tools() if routing is not None else []),
        *(phase_m_tools() if experiment_service is not None else []),
        *[
            tool
            for tool in phase_n_tools()
            if (diagnostic_service is not None and tool.name in PHASE_N_DIAGNOSTIC_TOOL_NAMES)
            or (retry_service is not None and tool.name in PHASE_N_RETRY_TOOL_NAMES)
        ],
    ]
    available_phase_n = {
        *(PHASE_N_DIAGNOSTIC_TOOL_NAMES if diagnostic_service is not None else frozenset()),
        *(PHASE_N_RETRY_TOOL_NAMES if retry_service is not None else frozenset()),
    }
    capability_catalog = CapabilityCatalog(
        (
            *(
                spec
                for spec in CAPABILITY_SPECS
                if (repositories.workflow_store == "sqlite" or spec.name not in G3_AUTHORING_TOOLS)
                and (asset_library is not None or spec.name not in PHASE_L_TOOL_NAMES)
                and (experiment_service is not None or spec.name not in PHASE_M_TOOL_NAMES)
                and (routing is not None or spec.name not in PHASE_K_TOOL_NAMES)
                and (
                    repositories.run_store == "sqlite" or spec.name != "comfyui.job.list"
                )
                and (
                    repositories.run_store == "sqlite"
                    or spec.name != "comfyui.job.history.suggest"
                )
                and (
                    spec.name not in PHASE_N_TOOL_NAMES
                    or spec.name in available_phase_n
                )
            ),
        )
    )
    tool_inventory = ToolInventory(
        (
            tool
            for tool in fixed_surface
            if tool_visible(tool.name, authorization.toolset, authorization.scopes)
        ),
        max_fixed_limit=ToolInventory.HARD_FIXED_LIMIT,
        max_dynamic_limit=max_dynamic_tools,
    )

    subscription_bus = subscription_bus or InMemorySubscriptionBus()
    listen_handler = ListenHandler(subscription_bus, max_subscriptions=64, max_buffered_events=256)
    change_monitor = WorkflowChangeMonitor(base_dir, subscription_bus)
    orchestration_runtime: OrchestrationRuntime | None = None
    orchestration_repository: SQLiteOrchestrationRepository | None = None
    if repositories.store is not None:
        orchestration_repository = SQLiteOrchestrationRepository(repositories.store)
        handlers: dict[str, WorkHandler] = {}
        provisioning_repository = getattr(repositories, "provisioning", None)
        if repositories.run_store == "sqlite":
            handlers["job.reconcile"] = JobReconciler(
                orchestration_repository,
                ComfyUIReconcileProbe(servers, gateway_factory),
            )
            if experiment_repository is not None:
                handlers["experiment.advance"] = ExperimentAdvanceHandler(
                    experiment_repository,
                    run_repository,
                    execution,
                    jobs,
                )
        if provisioning_repository is not None and manager_gateway is not None:
            handlers[PROVISIONING_WORK_TYPE] = ProvisioningWorkHandler(
                provisioning_repository,
                manager_gateway,
            )

        def resource_owner_for_uri(uri: str) -> str | None:
            if urlsplit(uri).netloc == "experiments" and experiment_repository is not None:
                return experiment_repository.resource_owner_for_uri(uri)
            if urlsplit(uri).netloc == "provisioning" and provisioning_repository is not None:
                return provisioning_repository.owner_for_uri(uri)
            return orchestration_repository.job_owner_for_uri(uri)

        if handlers:
            orchestration_runtime = OrchestrationRuntime(
                OperationOrchestrator(orchestration_repository, handlers),
                orchestration_repository,
                subscription_bus,
                worker_id=f"mcp-{uuid.uuid4().hex}",
                owner_for_uri=resource_owner_for_uri,
            )

    async def authorized_listen(ctx: Any, params: Any) -> Any:
        active_scopes = current_scopes() or authorization.scopes
        if orchestration_repository is not None:
            owner_id = current_owner()
            uris = params.notifications.resource_subscriptions or ()
            for value in uris:
                uri = str(value)
                legacy = parse_legacy_resource_uri(uri)
                kind: str
                if legacy is not None:
                    kind = legacy.kind
                else:
                    collection = urlsplit(uri).netloc
                    kind = {
                        "workflows": "workflow",
                        "revisions": "revision",
                        "deployments": "deployment",
                        "assets": "asset",
                        "jobs": "job",
                        "artifacts": "artifact",
                        "experiments": (
                            "variant" if "/variants/" in urlsplit(uri).path else "experiment"
                        ),
                    }.get(collection, "")
                required = scopes_for_resource(kind)
                if not required or not is_authorized(active_scopes, required):
                    raise MCPError(code=INVALID_PARAMS, message="Resource unavailable")
                if kind in {"asset", "artifact"}:
                    raise MCPError(code=INVALID_PARAMS, message="Resource unavailable")
                if kind not in {"job", "experiment", "variant"}:
                    continue
                owner_lookup = (
                    experiment_repository.resource_owner_for_uri
                    if kind in {"experiment", "variant"} and experiment_repository is not None
                    else orchestration_repository.job_owner_for_uri
                )
                resource_owner = await anyio.to_thread.run_sync(owner_lookup, uri)
                if resource_owner is None or resource_owner != owner_id:
                    raise MCPError(code=INVALID_PARAMS, message="Resource unavailable")
        return await listen_handler(ctx, params)

    @asynccontextmanager
    async def lifespan(_server: Server[dict[str, object]]) -> AsyncIterator[dict[str, object]]:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(change_monitor.run)
            if orchestration_runtime is not None:
                task_group.start_soon(orchestration_runtime.run_worker)
                task_group.start_soon(orchestration_runtime.run_outbox)
            try:
                yield {}
            finally:
                listen_handler.close()
                task_group.cancel_scope.cancel()

    def enabled_workflows() -> list[Workflow]:
        result: list[Workflow] = []
        for workflow in catalog.list_enabled():
            try:
                owner_server_connection(request_owner_id(), workflow.server_id)
            except ServerNotFound:
                continue
            result.append(workflow)
        return result

    resource_aliases = (
        SQLiteLegacyResourceAliasReader(repositories.store)
        if repositories.store is not None
        and (
            repositories.workflow_store == "sqlite"
            or repositories.run_store == "sqlite"
            or repositories.asset_store == "sqlite"
        )
        else None
    )

    experiment_variant_reader = None
    if experiment_service is not None:

        def experiment_variant_reader(
            experiment_id: str, variant_id: str, owner_id: str
        ) -> dict[str, Any]:
            return get_experiment_variant(experiment_service, experiment_id, variant_id, owner_id)

    experiment_preset_reader = (
        getattr(experiment_repository, "get_preset", None)
        if experiment_repository is not None
        else None
    )
    resource_handlers = create_resource_handlers(
        catalog,
        servers,
        assets,
        jobs,
        gateway_factory,
        enabled_workflows,
        resource_aliases=resource_aliases,
        require_authorization=enforce_authorization,
        authorization=authorization,
        workflow_inspection=workflow_inspection,
        asset_library=asset_library,
        asset_library_repository=asset_library_repository,
        experiment_service=experiment_service,
        experiment_variant_reader=experiment_variant_reader,
        experiment_preset_reader=experiment_preset_reader,
        diagnostic_service=diagnostic_service,
        retry_service=retry_service,
    )
    prompt_handlers = create_prompt_handlers(
        authorization,
        require_authorization=True,
        experiments_available=experiment_service is not None,
        diagnostics_available=diagnostic_service is not None,
    )

    def current_tools(
        apps_supported: bool = False,
    ) -> tuple[list[Tool], dict[str, Workflow], dict[str, str]]:
        granted_scopes = current_scopes() if enforce_authorization else None
        active_scopes = granted_scopes or authorization.scopes
        g3_tools_enabled = repositories.workflow_store == "sqlite"
        include_dynamic = (
            authorization.toolset is Toolset.EXECUTION and Scope.EXECUTE in active_scopes
        )
        all_workflows = workflow_tool_names(enabled_workflows()) if include_dynamic else {}
        selected_dynamic = tool_inventory.select_dynamic(all_workflows)
        canonical_workflow_map = {name: all_workflows[name] for name in selected_dynamic}
        canonical_tools: list[Tool] = []
        for name in sorted(canonical_workflow_map):
            workflow = canonical_workflow_map[name]
            schema = build_input_schema(workflow.parameters)
            schema["properties"]["_execution"] = EXECUTION_PROPERTY
            canonical_tools.append(
                decorate_tool(
                    Tool(
                        name=name,
                        title=f"Run {workflow.server_id}/{workflow.workflow_id}",
                        description=(
                            workflow.description
                            or f"Run ComfyUI workflow {workflow.server_id}/{workflow.workflow_id}"
                        ),
                        input_schema=schema,
                        output_schema=JOB_SCHEMA,
                        annotations=ToolAnnotations(
                            read_only_hint=False,
                            destructive_hint=False,
                            idempotent_hint=False,
                            open_world_hint=True,
                        ),
                    ),
                    risk="medium",
                    toolset="execution",
                )
            )
        if enforce_authorization or granted_scopes is not None:
            canonical_tools.extend(
                tool
                for tool in fixed_surface
                if (g3_tools_enabled or tool.name not in G3_AUTHORING_TOOLS)
                and (repositories.run_store == "sqlite" or tool.name != "comfyui.job.list")
                and (
                    repositories.run_store == "sqlite"
                    or tool.name != "comfyui.job.history.suggest"
                )
                and tool_visible(tool.name, authorization.toolset, active_scopes)
            )
            if authorization.toolset is not Toolset.EXECUTION:
                canonical_workflow_map = {}
        else:
            canonical_tools.extend(
                tool
                for tool in fixed_surface
                if (g3_tools_enabled or tool.name not in G3_AUTHORING_TOOLS)
                and (repositories.run_store == "sqlite" or tool.name != "comfyui.job.list")
                and (
                    repositories.run_store == "sqlite"
                    or tool.name != "comfyui.job.history.suggest"
                )
                and tool_visible(tool.name, authorization.toolset, authorization.scopes)
            )
        if apps_supported:
            canonical_tools = [
                with_ui_metadata(tool) if tool.name == "comfyui.job.get" else tool
                for tool in canonical_tools
            ]
        if not portable_tool_names:
            return (
                canonical_tools,
                canonical_workflow_map,
                {tool.name: tool.name for tool in canonical_tools},
            )
        tools: list[Tool] = []
        canonical_by_external: dict[str, str] = {}
        for tool in canonical_tools:
            external_name = _portable_tool_name(tool.name)
            prior = canonical_by_external.get(external_name)
            if prior is not None and prior != tool.name:
                raise RuntimeError(f"Portable tool name collision: {external_name}")
            canonical_by_external[external_name] = tool.name
            tools.append(tool.model_copy(update={"name": external_name}))
        workflow_map = {
            _portable_tool_name(name): workflow
            for name, workflow in canonical_workflow_map.items()
        }
        return tools, workflow_map, canonical_by_external

    async def list_tools(
        ctx: ServerRequestContext[dict[str, object]],
        _params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        tools, _mapping, _canonical_names = current_tools(
            apps_supported=client_supports_apps(ctx)
        )
        return ListToolsResult(tools=tools, ttl_ms=5_000, cache_scope="private")

    async def complete_reference(
        _ctx: ServerRequestContext[dict[str, object]],
        params: CompleteRequestParams,
    ) -> CompleteResult:
        name = params.argument.name
        prefix = params.argument.value
        if len(prefix) > 256:
            raise MCPError(code=INVALID_PARAMS, message="Completion prefix is too long")
        values: list[str] = []
        if name == "server_id":
            values = [server.server_id for server in servers.list()]
        elif name == "workflow_id":
            values = sorted({workflow.workflow_id for workflow in catalog.list_enabled()})
        elif name == "revision_id":
            context = params.context.arguments if params.context is not None else None
            workflow_id = context.get("workflow_id", "") if context else ""
            if workflow_id:
                values = [
                    str(item.get("revision_id", ""))
                    for item in workflow_repository.list_revisions(workflow_id)
                ]
        matches = sorted(value for value in values if value and value.startswith(prefix))
        return CompleteResult(
            completion=Completion(
                values=matches[:100],
                total=len(matches),
                has_more=len(matches) > 100,
            )
        )

    async def call_tool(
        ctx: ServerRequestContext[dict[str, object]],
        params: CallToolRequestParams,
    ) -> CallToolResult:
        owner_id = (
            authorization.principal_id
            if enforce_authorization and current_scopes() is None
            else current_owner()
        )
        started = time.perf_counter()
        failed = False
        with tracer.span(
            "tool.call", {"tool": params.name, "owner": owner_id}
        ) as span:
            try:
                result = await _dispatch_tool_call(ctx, params)
                failed = bool(result.is_error)
            except BaseException:
                failed = True
                raise
            finally:
                elapsed = time.perf_counter() - started
                attributes: dict[str, Any] = {"duration_ms": elapsed * 1000.0}
                if failed:
                    attributes["is_error"] = True
                    tool_errors.add(1, {"tool": params.name, "owner": owner_id})
                span.set_attributes(attributes)
                tool_calls.add(1, {"tool": params.name, "owner": owner_id})
                tool_duration.record(elapsed, {"tool": params.name, "owner": owner_id})
            return result

    async def _dispatch_tool_call(
        ctx: ServerRequestContext[dict[str, object]],
        params: CallToolRequestParams,
    ) -> CallToolResult:
        arguments = dict(params.arguments or {})
        owner_id = (
            authorization.principal_id
            if enforce_authorization and current_scopes() is None
            else current_owner()
        )
        tools, workflow_map, canonical_by_external = current_tools(
            apps_supported=client_supports_apps(ctx)
        )
        requested_name = params.name
        visible_names = {tool.name for tool in tools}
        if requested_name not in visible_names:
            raise MCPError(code=INVALID_PARAMS, message=f"Unknown tool: {requested_name}")
        try:
            if requested_name in workflow_map:
                workflow = workflow_map[requested_name]
                execution_options = arguments.pop("_execution", {})
                if not isinstance(execution_options, dict):
                    raise TypeError("_execution must be an object")
                idempotency_key = execution_options.get("idempotency_key", "")
                unexpected_options = set(execution_options) - {
                    "idempotency_key",
                    "wait",
                    "wait_timeout_seconds",
                    "priority",
                    "partial_execution_targets",
                }
                if unexpected_options:
                    raise ValueError(
                        "Unexpected _execution fields: " + ", ".join(sorted(unexpected_options))
                    )
                if not isinstance(idempotency_key, str):
                    raise TypeError("idempotency_key must be a string")
                wait = execution_options.get("wait", False)
                if len(idempotency_key) > 256:
                    raise ValueError("idempotency_key exceeds 256 characters")
                if not isinstance(wait, bool):
                    raise TypeError("wait must be a boolean")
                timeout_raw = execution_options.get("wait_timeout_seconds", 120)
                if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, (int, float)):
                    raise TypeError("wait_timeout_seconds must be a number")
                timeout_seconds = float(timeout_raw)
                if not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= 300:
                    raise ValueError("wait_timeout_seconds must be between 0 and 300")
                priority_raw = execution_options.get("priority")
                priority: float | None = None
                if priority_raw is not None:
                    if isinstance(priority_raw, bool) or not isinstance(
                        priority_raw, (int, float)
                    ):
                        raise TypeError("priority must be a number")
                    priority = float(priority_raw)
                    if not math.isfinite(priority) or not -1000 <= priority <= 1000:
                        raise ValueError("priority must be between -1000 and 1000")
                targets_raw = execution_options.get("partial_execution_targets")
                targets: tuple[str, ...] = ()
                if targets_raw is not None:
                    if not isinstance(targets_raw, list) or not targets_raw:
                        raise TypeError(
                            "partial_execution_targets must be a non-empty array"
                        )
                    if len(targets_raw) > 100:
                        raise ValueError(
                            "partial_execution_targets must not exceed 100 entries"
                        )
                    for item in targets_raw:
                        if (
                            not isinstance(item, str)
                            or not item
                            or len(item) > 128
                            or "\r" in item
                            or "\n" in item
                        ):
                            raise TypeError(
                                "partial_execution_targets entries must be node IDs "
                                "without line breaks"
                            )
                    targets = tuple(dict.fromkeys(targets_raw))
                await ctx.session.report_progress(1, None, "Submitting ComfyUI workflow")
                job = await anyio.to_thread.run_sync(
                    lambda: execution.submit(
                        workflow.server_id,
                        workflow.workflow_id,
                        arguments,
                        idempotency_key=idempotency_key,
                        owner_id=owner_id,
                        server_connection=owner_server_connection(owner_id, workflow.server_id),
                        priority=priority,
                        targets=targets,
                    )
                )
                await ctx.session.report_progress(2, None, "Workflow submitted")
                if wait:
                    progress_value = 2

                    def report(event: dict[str, Any]) -> None:
                        nonlocal progress_value
                        progress_value += 1
                        data = event.get("data", {})
                        message = str(event.get("type", "progress"))
                        if data.get("node") is not None:
                            message = f"{message}: node {data['node']}"
                        anyio.from_thread.run(
                            ctx.session.report_progress,
                            progress_value,
                            None,
                            message,
                        )

                    job = await anyio.to_thread.run_sync(
                        lambda: jobs.wait(
                            workflow.server_id,
                            job.prompt_id,
                            timeout_seconds=timeout_seconds,
                            progress=report,
                            cancel_check=anyio.from_thread.check_cancelled,
                            owner_id=owner_id,
                        ),
                        abandon_on_cancel=True,
                    )
                return tool_result(job_dict(job))
            params = params.model_copy(update={"name": canonical_by_external[requested_name]})
            request_authorization = AuthorizationContext(
                owner_id,
                current_scopes() or authorization.scopes,
                authorization.toolset,
            )
            external_by_canonical = {
                canonical: external
                for external, canonical in canonical_by_external.items()
            }
            if params.name == "comfyui.capability.search":
                validate_fixed_arguments(arguments, {"query", "limit"})
                query = optional_string(arguments, "query", "")
                limit = arguments.get("limit", 10)
                result = capability_catalog.search(query, request_authorization, limit=limit)
                if portable_tool_names:
                    result["items"] = [
                        {**item, "name": external_by_canonical[str(item["name"])]}
                        for item in result["items"]
                        if str(item.get("name", "")) in external_by_canonical
                    ]
                    result["total"] = len(result["items"])
                return tool_result(result)
            if params.name == "comfyui.capability.describe":
                validate_fixed_arguments(arguments, {"name"})
                requested_capability_name = required_string(arguments, "name")
                capability_name = canonical_by_external.get(
                    requested_capability_name,
                    requested_capability_name,
                )
                result = capability_catalog.describe(
                    capability_name,
                    request_authorization,
                )
                if portable_tool_names:
                    result["name"] = external_by_canonical[capability_name]

                described_tool = next(
                    (
                        tool
                        for tool in tools
                        if canonical_by_external[tool.name] == capability_name
                    ),
                    None,
                )
                if described_tool is None:
                    raise PermissionError("capability is unavailable")
                result["input_schema"] = described_tool.input_schema
                result["output_schema"] = described_tool.output_schema
                return tool_result(result)
            if params.name in PHASE_K_TOOL_NAMES:
                if routing is None:
                    raise ValueError("Routing service is unavailable")
                if params.name == "comfyui.execution.plan":
                    validate_fixed_arguments(
                        arguments,
                        {
                            "workflow_id",
                            "arguments",
                            "server_id",
                            "policy",
                            "submission_window",
                            "request_id",
                        },
                    )
                    result = await anyio.to_thread.run_sync(
                        lambda: routing.plan(
                            owner_id,
                            validate_identifier(
                                required_string(arguments, "workflow_id", max_length=128),
                                field="workflow_id",
                            ),
                            required_object(
                                arguments, "arguments", max_properties=256, max_bytes=1024 * 1024
                            ),
                            server_id=optional_string(arguments, "server_id", "", max_length=128),
                            policy=required_object(
                                {"policy": arguments.get("policy", {})},
                                "policy",
                                max_properties=4,
                                max_bytes=4096,
                            ),
                            submission_window=bounded_integer(
                                arguments,
                                "submission_window",
                                0,
                                minimum=0,
                                maximum=10_000,
                            ),
                            request_id=optional_string(arguments, "request_id", "", max_length=256),
                        )
                    )
                elif params.name == "comfyui.execution.commit":
                    validate_fixed_arguments(
                        arguments, {"plan_id", "plan_digest", "idempotency_key"}
                    )
                    result = await anyio.to_thread.run_sync(
                        lambda: routing.commit(
                            required_string(arguments, "plan_id", max_length=128),
                            required_string(arguments, "plan_digest", max_length=64),
                            owner_id,
                            idempotency_key=required_string(
                                arguments, "idempotency_key", max_length=256
                            ),
                        )
                    )
                elif params.name == "comfyui.route.explain":
                    validate_fixed_arguments(arguments, {"plan_id"})
                    result = await anyio.to_thread.run_sync(
                        routing.explain,
                        required_string(arguments, "plan_id", max_length=128),
                        owner_id,
                    )
                else:
                    validate_fixed_arguments(arguments, {"arguments", "policy"})
                    result = routing.evaluate_policy(
                        required_object(
                            arguments, "arguments", max_properties=256, max_bytes=1024 * 1024
                        ),
                        required_object(arguments, "policy", max_properties=4, max_bytes=4096),
                    )
                return tool_result(result)
            if params.name in PHASE_N_DIAGNOSTIC_TOOL_NAMES:
                if diagnostic_service is None:
                    raise ValueError("Diagnostic service is unavailable")
                if params.name == "comfyui.job.diagnose":
                    validate_fixed_arguments(arguments, {"job_id"})
                    job_id = validate_control_plane_id(
                        "job", required_string(arguments, "job_id", max_length=128)
                    )
                    result = await anyio.to_thread.run_sync(
                        diagnostic_service.diagnose_job, job_id, owner_id
                    )
                else:
                    validate_fixed_arguments(arguments, {"server_id"})
                    server_id = validate_identifier(
                        required_string(arguments, "server_id", max_length=128),
                        field="server_id",
                    )
                    result = await anyio.to_thread.run_sync(
                        diagnostic_service.diagnose_server, server_id, owner_id
                    )
                return tool_result(diagnostic_report_dict(result))
            if params.name in PHASE_N_RETRY_TOOL_NAMES:
                if retry_service is None:
                    raise ValueError("Retry service is unavailable")
                if params.name == "comfyui.job.retry.plan":
                    validate_fixed_arguments(arguments, {"job_id", "changes"})
                    job_id = validate_control_plane_id(
                        "job", required_string(arguments, "job_id", max_length=128)
                    )
                    changes = required_object(
                        arguments, "changes", max_properties=64, max_bytes=256 * 1024
                    )
                    result = await anyio.to_thread.run_sync(
                        retry_service.plan, job_id, owner_id, changes
                    )
                else:
                    validate_fixed_arguments(arguments, {"repair_plan_id", "plan_digest"})
                    repair_plan_id = required_string(arguments, "repair_plan_id", max_length=128)
                    plan_digest = required_string(arguments, "plan_digest", max_length=64)
                    if re.fullmatch(r"repair_plan_[0-9a-f]{64}", repair_plan_id) is None:
                        raise ValueError("repair_plan_id must be canonical")
                    if re.fullmatch(r"[0-9a-f]{64}", plan_digest) is None:
                        raise ValueError("plan_digest must be a SHA-256 digest")
                    result = await anyio.to_thread.run_sync(
                        retry_service.commit, repair_plan_id, plan_digest, owner_id
                    )
                return tool_result(repair_plan_dict(result))
            if params.name in PHASE_M_TOOL_NAMES:
                if experiment_service is None:
                    raise ValueError("Experiment service is unavailable")
                if params.name == "comfyui.experiment.plan":
                    validate_fixed_arguments(
                        arguments,
                        {
                            "workflow_id",
                            "server_id",
                            "preset_id",
                            "expansion",
                            "base_arguments",
                            "budgets",
                            "failure_policy",
                            "concurrency",
                            "submission_window",
                        },
                    )
                    workflow_id = required_string(arguments, "workflow_id", max_length=128)
                    server_id = required_string(arguments, "server_id", max_length=128)
                    base_arguments = required_object(
                        arguments, "base_arguments", max_properties=64, max_bytes=256 * 1024
                    )
                    preset_id = optional_string(arguments, "preset_id", "", max_length=128)
                    if preset_id:
                        if experiment_repository is None:
                            raise ValueError("Experiment Preset seeding is unavailable")
                        preset_arguments = experiment_repository.consume_preset(
                            preset_id, owner_id, workflow_id, server_id
                        )
                        base_arguments = {**preset_arguments, **base_arguments}
                    result = await anyio.to_thread.run_sync(
                        lambda: experiment_service.plan(
                            owner_id,
                            workflow_id,
                            server_id,
                            required_object(
                                arguments, "expansion", max_properties=4, max_bytes=1024 * 1024
                            ),
                            base_arguments,
                            required_object(arguments, "budgets", max_properties=5, max_bytes=4096),
                            required_string(arguments, "failure_policy", max_length=32),
                            bounded_integer(arguments, "concurrency", 1, minimum=1, maximum=64),
                            bounded_integer(
                                arguments, "submission_window", 0, minimum=0, maximum=10_000
                            ),
                        )
                    )
                    return tool_result(experiment_dict(result))
                if params.name == "comfyui.experiment.commit":
                    validate_fixed_arguments(arguments, {"plan_id", "plan_digest"})
                    result = await anyio.to_thread.run_sync(
                        lambda: experiment_service.commit(
                            required_string(arguments, "plan_id", max_length=128),
                            required_string(arguments, "plan_digest", max_length=128),
                            owner_id,
                        )
                    )
                    return tool_result(experiment_dict(result))
                if params.name == "comfyui.experiment.get":
                    validate_fixed_arguments(arguments, {"experiment_id"})
                    result = await anyio.to_thread.run_sync(
                        lambda: experiment_service.get(
                            required_string(arguments, "experiment_id", max_length=128),
                            owner_id,
                        )
                    )
                    return tool_result(experiment_dict(result))
                if params.name == "comfyui.experiment.cancel":
                    validate_fixed_arguments(arguments, {"experiment_id", "mode"})
                    result = await anyio.to_thread.run_sync(
                        lambda: experiment_service.cancel(
                            required_string(arguments, "experiment_id", max_length=128),
                            required_string(arguments, "mode", max_length=32),
                            owner_id,
                        )
                    )
                    return tool_result(experiment_dict(result))
                if params.name == "comfyui.experiment.variant.list":
                    validate_fixed_arguments(arguments, {"experiment_id", "limit", "cursor"})
                    result = await anyio.to_thread.run_sync(
                        lambda: experiment_service.list_variants(
                            required_string(arguments, "experiment_id", max_length=128),
                            owner_id,
                            bounded_integer(arguments, "limit", 50, minimum=1, maximum=100),
                            optional_string(arguments, "cursor", "", max_length=2048),
                        )
                    )
                    return tool_result(variant_page_dict(result))
                if params.name == "comfyui.experiment.variant.rate":
                    validate_fixed_arguments(
                        arguments,
                        {"experiment_id", "variant_id", "rubric_version", "scores"},
                    )
                    result = await anyio.to_thread.run_sync(
                        lambda: experiment_service.rate(
                            required_string(arguments, "experiment_id", max_length=128),
                            required_string(arguments, "variant_id", max_length=128),
                            required_string(arguments, "rubric_version", max_length=128),
                            required_object(
                                arguments,
                                "scores",
                                max_properties=32,
                                max_bytes=16 * 1024,
                            ),
                            owner_id,
                        )
                    )
                    return tool_result(rating_dict(result))
                validate_fixed_arguments(arguments, {"experiment_id", "variant_id", "target"})
                result = await anyio.to_thread.run_sync(
                    lambda: experiment_service.promote(
                        required_string(arguments, "experiment_id", max_length=128),
                        required_string(arguments, "variant_id", max_length=128),
                        required_string(arguments, "target", max_length=16),
                        owner_id,
                    )
                )
                return tool_result(promotion_dict(result))
            if params.name in PHASE_L_TOOL_NAMES:
                if asset_library is None:
                    raise ValueError("Asset library is unavailable")
                if params.name == "comfyui.asset.list":
                    validate_fixed_arguments(
                        arguments, {"limit", "cursor", "media_type", "collection"}
                    )
                    result = await anyio.to_thread.run_sync(
                        lambda: asset_library.list_assets(
                            owner_id=owner_id,
                            limit=bounded_integer(arguments, "limit", 20, minimum=1, maximum=100),
                            cursor=optional_string(arguments, "cursor", "", max_length=2048),
                            media_type=optional_string(arguments, "media_type", "", max_length=16),
                            collection=optional_string(arguments, "collection", "", max_length=128),
                        )
                    )
                elif params.name == "comfyui.asset.describe":
                    validate_fixed_arguments(arguments, {"asset_id"})
                    result = await anyio.to_thread.run_sync(
                        lambda: asset_library.describe(
                            required_string(arguments, "asset_id", max_length=128),
                            owner_id=owner_id,
                        )
                    )
                elif params.name == "comfyui.asset.collection.update":
                    validate_fixed_arguments(arguments, {"collection", "asset_ids", "action"})
                    asset_ids = arguments.get("asset_ids")
                    if (
                        not isinstance(asset_ids, list)
                        or not asset_ids
                        or len(asset_ids) > 100
                        or any(
                            not isinstance(item, str) or not item or len(item) > 128
                            for item in asset_ids
                        )
                    ):
                        raise TypeError("asset_ids must be a non-empty list of strings")
                    action = required_string(arguments, "action", max_length=16)
                    if action not in {"add", "remove"}:
                        raise ValueError("action must be add or remove")
                    result = await anyio.to_thread.run_sync(
                        lambda: asset_library.collection_update(
                            required_string(arguments, "collection", max_length=128),
                            asset_ids,
                            action,
                            owner_id=owner_id,
                        )
                    )
                elif params.name == "comfyui.asset.metadata.extract":
                    validate_fixed_arguments(arguments, {"asset_id"})
                    result = await anyio.to_thread.run_sync(
                        lambda: asset_library.metadata_extract(
                            required_string(arguments, "asset_id", max_length=128),
                            owner_id=owner_id,
                        )
                    )
                elif params.name == "comfyui.asset.import_output":
                    validate_fixed_arguments(
                        arguments,
                        {"artifact_id", "target_server_id", "workflow_id", "parameter_name"},
                    )
                    result = await anyio.to_thread.run_sync(
                        lambda: asset_library.import_output(
                            required_string(arguments, "artifact_id", max_length=128),
                            required_string(arguments, "target_server_id", max_length=128),
                            required_string(arguments, "workflow_id", max_length=128),
                            required_string(arguments, "parameter_name", max_length=128),
                            owner_id=owner_id,
                        )
                    )
                elif params.name == "comfyui.asset.delete.plan":
                    validate_fixed_arguments(arguments, {"asset_id"})
                    result = await anyio.to_thread.run_sync(
                        lambda: asset_library.delete_plan(
                            required_string(arguments, "asset_id", max_length=128),
                            owner_id=owner_id,
                        )
                    )
                elif params.name == "comfyui.asset.delete.commit":
                    validate_fixed_arguments(arguments, {"plan_id", "plan_digest"})
                    result = await anyio.to_thread.run_sync(
                        lambda: asset_library.delete_commit(
                            required_string(arguments, "plan_id", max_length=128),
                            required_string(arguments, "plan_digest", max_length=128),
                            owner_id=owner_id,
                        )
                    )
                elif params.name == "comfyui.asset.transfer.plan":
                    validate_fixed_arguments(arguments, {"artifact_id", "target_server_id"})
                    result = await anyio.to_thread.run_sync(
                        lambda: asset_library.transfer_plan(
                            required_string(arguments, "artifact_id", max_length=128),
                            required_string(arguments, "target_server_id", max_length=128),
                            owner_id=owner_id,
                        )
                    )
                elif params.name == "comfyui.asset.transfer.commit":
                    validate_fixed_arguments(arguments, {"transfer_id", "plan_digest"})
                    result = await anyio.to_thread.run_sync(
                        lambda: asset_library.transfer_commit(
                            required_string(arguments, "transfer_id", max_length=128),
                            required_string(arguments, "plan_digest", max_length=128),
                            owner_id=owner_id,
                        )
                    )
                else:
                    validate_fixed_arguments(arguments, {"transfer_id"})
                    result = await anyio.to_thread.run_sync(
                        lambda: asset_library.transfer_get(
                            required_string(arguments, "transfer_id", max_length=128),
                            owner_id=owner_id,
                        )
                    )
                return tool_result(result)
            if params.name == "comfyui.revision.list":
                validate_fixed_arguments(arguments, {"workflow_id"})
                workflow_id = required_string(arguments, "workflow_id")
                revisions = await anyio.to_thread.run_sync(
                    lambda: workflow_repository.list_revisions(workflow_id)
                )
                summaries = [
                    {
                        "revision_id": revision["revision_id"],
                        "workflow_id": revision["workflow_id"],
                        "content_digest": revision["content_digest"],
                        "created_at": revision["created_at"],
                    }
                    for revision in revisions
                ]
                return tool_result({"workflow_id": workflow_id, "revisions": summaries})
            if params.name == "comfyui.revision.diff":
                validate_fixed_arguments(arguments, {"from_revision_id", "to_revision_id"})
                if workflow_changes is None:
                    raise ValueError("Workflow revision diff requires the SQLite Workflow store")
                from_revision_id = required_string(arguments, "from_revision_id")
                to_revision_id = required_string(arguments, "to_revision_id")
                result = await anyio.to_thread.run_sync(
                    lambda: workflow_changes.diff(from_revision_id, to_revision_id)
                )
                return tool_result(result)
            if params.name == "comfyui.workflow.visualize":
                validate_fixed_arguments(arguments, {"workflow_id", "server_id"})
                workflow_id = required_string(arguments, "workflow_id")
                server_id = required_string(arguments, "server_id")
                if not isinstance(workflow_repository, SQLiteWorkflowRepository):
                    raise ValueError(
                        "Workflow visualization requires the SQLite Workflow store"
                    )
                try:
                    deployment = workflow_repository.describe(workflow_id, server_id)
                    revision = workflow_repository.get_revision(
                        _required_revision_id(deployment, workflow_id, server_id)
                    )
                except LookupError as exc:
                    raise WorkflowNotFound(
                        f"Workflow not found: {server_id}/{workflow_id}"
                    ) from exc
                graph = revision.get("graph")
                if not isinstance(graph, dict):
                    raise ValueError("Workflow revision has no graph")
                return tool_result(
                    {
                        "workflow_id": workflow_id,
                        "server_id": server_id,
                        "mermaid": _graph_mermaid(graph),
                        "node_count": len(graph),
                    }
                )
            if params.name in {
                "comfyui.workflow.describe",
                "comfyui.workflow.dependencies.check",
            }:
                validate_fixed_arguments(arguments, {"workflow_id", "server_id"})
                workflow_id = required_string(arguments, "workflow_id")
                server_id = required_string(arguments, "server_id")
                gateway = gateway_factory(servers.connection(server_id))
                if workflow_inspection is None:
                    raise ValueError("Workflow inspection requires the SQLite Workflow store")
                operation = (
                    workflow_inspection.describe
                    if params.name == "comfyui.workflow.describe"
                    else workflow_inspection.dependencies_check
                )
                result = await anyio.to_thread.run_sync(
                    lambda: operation(workflow_id, server_id, gateway)
                )
                return tool_result(result)
            if params.name == "comfyui.job.get":
                validate_fixed_arguments(arguments, {"server_id", "prompt_id"})
                server_id = required_string(arguments, "server_id")
                prompt_id = required_string(arguments, "prompt_id")
                job = await anyio.to_thread.run_sync(
                    lambda: jobs.get(server_id, prompt_id, owner_id=owner_id)
                )
                return tool_result(job_dict(job))
            if params.name == "comfyui.job.list":
                validate_fixed_arguments(
                    arguments,
                    {"status", "workflow_id", "server_id", "created_after", "limit", "cursor"},
                )
                status = optional_string(arguments, "status", "", max_length=32)
                workflow_id = optional_string(arguments, "workflow_id", "", max_length=128)
                server_id = optional_string(arguments, "server_id", "", max_length=128)
                created_after = optional_string(arguments, "created_after", "", max_length=64)
                cursor = optional_string(arguments, "cursor", "", max_length=4096)
                limit = bounded_integer(arguments, "limit", 50, minimum=1, maximum=100)
                result = await anyio.to_thread.run_sync(
                    lambda: jobs.list(
                        owner_id=owner_id,
                        status=status,
                        workflow_id=workflow_id,
                        server_id=server_id,
                        created_after=created_after,
                        limit=limit,
                        cursor=cursor,
                    )
                )
                return tool_result(result)
            if params.name == "comfyui.workflow.list":
                validate_fixed_arguments(
                    arguments,
                    {"server_id", "query", "include_disabled", "limit", "cursor"},
                )
                server_id = optional_string(arguments, "server_id", "", max_length=128)
                query = optional_string(arguments, "query", "", max_length=256)
                include_disabled = optional_boolean(arguments, "include_disabled", False)
                cursor = optional_string(arguments, "cursor", "", max_length=512)
                limit = bounded_integer(arguments, "limit", 50, minimum=1, maximum=200)
                result = await anyio.to_thread.run_sync(
                    lambda: catalog.list_workflows(
                        server_id=server_id,
                        query=query,
                        include_disabled=include_disabled,
                        limit=limit,
                        cursor=cursor,
                    )
                )
                return tool_result(result)
            if params.name == "comfyui.job.cancel":
                validate_fixed_arguments(arguments, {"server_id", "prompt_id"})
                server_id = required_string(arguments, "server_id")
                prompt_id = required_string(arguments, "prompt_id")
                job = await anyio.to_thread.run_sync(
                    lambda: jobs.cancel(server_id, prompt_id, owner_id=owner_id)
                )
                return tool_result(job_dict(job))
            if params.name == "comfyui.server.list":
                validate_fixed_arguments(arguments, set())
                return tool_result(await anyio.to_thread.run_sync(discovery.servers))
            if params.name == "comfyui.server.health":
                validate_fixed_arguments(arguments, {"server_id"})
                server_id = required_string(arguments, "server_id")
                result = await anyio.to_thread.run_sync(lambda: discovery.health(server_id))
                return tool_result(result)
            if params.name == "comfyui.queue.list":
                validate_fixed_arguments(arguments, {"server_id", "limit", "cursor"})
                server_id = required_string(arguments, "server_id", max_length=128)
                cursor = optional_string(arguments, "cursor", "", max_length=4096)
                limit = bounded_integer(arguments, "limit", 50, minimum=1, maximum=200)
                result = await anyio.to_thread.run_sync(
                    lambda: observation.queue(server_id, limit=limit, cursor=cursor)
                )
                return tool_result(result)
            if params.name == "comfyui.model.guidance":
                validate_fixed_arguments(arguments, {"query"})
                query = optional_string(arguments, "query", "")
                return tool_result(model_guidance(query=query))
            if params.name == "comfyui.job.history.suggest":
                validate_fixed_arguments(arguments, {"workflow_id"})
                workflow_id = optional_string(arguments, "workflow_id", "")
                if suggestion_service is None:
                    raise ValueError(
                        "Run-history suggestions require the SQLite run store"
                    )
                result = await anyio.to_thread.run_sync(
                    lambda: suggestion_service.suggest(owner_id, workflow_id=workflow_id)
                )
                return tool_result(result)
            if params.name == "comfyui.local.plugins":
                validate_fixed_arguments(arguments, {"server_id"})
                server_id = required_string(arguments, "server_id", max_length=128)
                result = await anyio.to_thread.run_sync(
                    lambda: discovery.plugins(server_id)
                )
                return tool_result(result)
            if params.name == "comfyui.node.blueprint":
                validate_fixed_arguments(arguments, {"server_id", "query", "limit"})
                server_id = required_string(arguments, "server_id", max_length=128)
                query = required_string(arguments, "query", max_length=256)
                limit = bounded_integer(arguments, "limit", 5, minimum=1, maximum=10)
                result = await anyio.to_thread.run_sync(
                    lambda: discovery.blueprint(server_id, query=query, limit=limit)
                )
                return tool_result(result)
            if params.name == "comfyui.engine.history":
                validate_fixed_arguments(arguments, {"server_id", "prompt_id", "limit"})
                server_id = required_string(arguments, "server_id", max_length=128)
                prompt_id = optional_string(arguments, "prompt_id", "", max_length=128)
                limit = bounded_integer(arguments, "limit", 10, minimum=1, maximum=50)
                gateway = gateway_factory(servers.connection(server_id))
                result = await anyio.to_thread.run_sync(
                    lambda: _engine_history_projection(
                        gateway, prompt_id=prompt_id, limit=limit
                    )
                )
                return tool_result(result)
            if params.name == "comfyui.queue.remove":
                validate_fixed_arguments(arguments, {"server_id", "prompt_ids", "execute"})
                prompt_ids = arguments.get("prompt_ids")
                if not isinstance(prompt_ids, list):
                    raise TypeError("prompt_ids must be an array")
                result = await anyio.to_thread.run_sync(
                    lambda: runtime_controls.queue_remove(
                        required_string(arguments, "server_id", max_length=128),
                        prompt_ids,
                        owner_id,
                        execute=optional_boolean(arguments, "execute", False),
                    )
                )
                return tool_result(result)
            if params.name == "comfyui.queue.clear":
                validate_fixed_arguments(arguments, {"server_id", "execute"})
                result = await anyio.to_thread.run_sync(
                    lambda: runtime_controls.queue_clear(
                        required_string(arguments, "server_id", max_length=128),
                        owner_id,
                        execute=optional_boolean(arguments, "execute", False),
                        allow_cross_owner=True,
                    )
                )
                return tool_result(result)
            if params.name == "comfyui.server.interrupt":
                validate_fixed_arguments(arguments, {"server_id", "execute"})
                result = await anyio.to_thread.run_sync(
                    lambda: runtime_controls.interrupt(
                        required_string(arguments, "server_id", max_length=128),
                        owner_id,
                        execute=optional_boolean(arguments, "execute", False),
                        allow_cross_owner=True,
                    )
                )
                return tool_result(result)
            if params.name == "comfyui.runtime.restart.plan":
                validate_fixed_arguments(arguments, {"server_id"})
                result = await anyio.to_thread.run_sync(
                    runtime_controls.restart_plan,
                    required_string(arguments, "server_id", max_length=128),
                    owner_id,
                )
                return tool_result(result)
            if params.name == "comfyui.log.read":
                validate_fixed_arguments(arguments, {"server_id", "limit", "cursor"})
                server_id = required_string(arguments, "server_id", max_length=128)
                cursor = optional_string(arguments, "cursor", "", max_length=4096)
                limit = bounded_integer(arguments, "limit", 100, minimum=1, maximum=1000)
                result = await anyio.to_thread.run_sync(
                    lambda: observation.logs(server_id, limit=limit, cursor=cursor)
                )
                return tool_result(result)
            if params.name == "comfyui.server.capabilities":
                validate_fixed_arguments(arguments, {"server_id"})
                server_id = required_string(arguments, "server_id", max_length=128)
                result = await anyio.to_thread.run_sync(lambda: observation.capabilities(server_id))
                return tool_result(result)
            if params.name == "comfyui.template.list":
                validate_fixed_arguments(arguments, {"server_id", "limit", "cursor"})
                server_id = required_string(arguments, "server_id", max_length=128)
                cursor = optional_string(arguments, "cursor", "", max_length=4096)
                limit = bounded_integer(arguments, "limit", 50, minimum=1, maximum=200)
                result = await anyio.to_thread.run_sync(
                    lambda: observation.templates(server_id, limit=limit, cursor=cursor)
                )
                return tool_result(result)
            if params.name == "comfyui.subgraph.list":
                validate_fixed_arguments(arguments, {"server_id", "limit", "cursor"})
                server_id = required_string(arguments, "server_id", max_length=128)
                cursor = optional_string(arguments, "cursor", "", max_length=4096)
                limit = bounded_integer(arguments, "limit", 50, minimum=1, maximum=200)
                result = await anyio.to_thread.run_sync(
                    lambda: observation.subgraphs(server_id, limit=limit, cursor=cursor)
                )
                return tool_result(result)
            if params.name == "comfyui.subgraph.get":
                validate_fixed_arguments(arguments, {"server_id", "subgraph_id"})
                server_id = required_string(arguments, "server_id", max_length=128)
                subgraph_id = required_string(arguments, "subgraph_id", max_length=128)
                result = await anyio.to_thread.run_sync(
                    lambda: observation.subgraph(server_id, subgraph_id)
                )
                return tool_result(result)
            if params.name == "comfyui.server.free":
                validate_fixed_arguments(
                    arguments, {"server_id", "unload_models", "free_memory", "request_id"}
                )
                server_id = required_string(arguments, "server_id", max_length=128)
                unload_models = optional_boolean(arguments, "unload_models", False)
                free_memory = optional_boolean(arguments, "free_memory", False)
                if not (unload_models or free_memory):
                    raise ValueError("at least one memory action must be selected")
                audit_log = JsonlAuditLog(base_dir / "data" / "admin-audit.jsonl")
                request_id = (
                    optional_string(arguments, "request_id", "", max_length=128)
                    or uuid.uuid4().hex
                )
                result = await anyio.to_thread.run_sync(
                    lambda: _free_locked(
                        audit_log,
                        request_id,
                        owner_id,
                        {
                            "server_id": server_id,
                            "unload_models": unload_models,
                            "free_memory": free_memory,
                        },
                        observation,
                        server_id,
                        unload_models,
                        free_memory,
                    )
                )
                return tool_result(result)
            if params.name == "comfyui.node.list":
                validate_fixed_arguments(arguments, {"server_id", "query", "limit", "cursor"})
                server_id = required_string(arguments, "server_id")
                query = optional_string(arguments, "query", "")
                cursor = optional_string(arguments, "cursor", "")
                limit = arguments.get("limit", 50)
                result = await anyio.to_thread.run_sync(
                    lambda: discovery.nodes(server_id, query=query, limit=limit, cursor=cursor)
                )
                return tool_result(result)
            if params.name == "comfyui.node.describe":
                validate_fixed_arguments(arguments, {"server_id", "node_class"})
                server_id = required_string(arguments, "server_id")
                node_class = required_string(arguments, "node_class")
                result = await anyio.to_thread.run_sync(
                    lambda: discovery.node(server_id, node_class)
                )
                return tool_result(result)
            if params.name == "comfyui.model.list":
                validate_fixed_arguments(
                    arguments, {"server_id", "kind", "query", "limit", "cursor"}
                )
                server_id = required_string(arguments, "server_id")
                kind = optional_string(arguments, "kind", "")
                query = optional_string(arguments, "query", "")
                cursor = optional_string(arguments, "cursor", "")
                limit = arguments.get("limit", 50)
                result = await anyio.to_thread.run_sync(
                    lambda: discovery.models(
                        server_id,
                        kind=kind,
                        query=query,
                        limit=limit,
                        cursor=cursor,
                    )
                )
                return tool_result(result)
            if params.name == "comfyui.asset.upload":
                validate_fixed_arguments(
                    arguments,
                    {"server_id", "local_path", "purpose", "original_asset_id"},
                )
                server_id = required_string(arguments, "server_id")
                local_path = required_string(arguments, "local_path")
                purpose = optional_string(arguments, "purpose", "image")
                original_asset_id = optional_string(arguments, "original_asset_id", "")
                gateway = gateway_factory(owner_server_connection(owner_id, server_id))
                asset = await anyio.to_thread.run_sync(
                    lambda: assets.upload_local(
                        gateway,
                        server_id,
                        local_path,
                        purpose=purpose,
                        original_asset_id=original_asset_id,
                        owner_id=owner_id,
                    )
                )
                return tool_result(asset.to_public_dict())
            raise MCPError(
                code=INVALID_PARAMS,
                message=f"Unknown tool: {params.name}",
            )
        except MCPError:
            raise
        except (ComfyUISkillsError, KeyError, PermissionError, TypeError, ValueError) as exc:
            if isinstance(exc, ComfyUISkillsError):
                error = exc.as_dict()
            else:
                error = {
                    "code": "INVALID_ARGUMENTS",
                    "message": "Invalid tool arguments",
                    "retryable": False,
                    "details": {},
                }
            return tool_result(error, error=True)
        except RuntimeError as exc:
            return tool_result(
                {
                    "code": "OPERATION_UNAVAILABLE",
                    "message": str(exc),
                    "retryable": False,
                    "details": {},
                },
                error=True,
            )
        except Exception:
            logger.exception("Unexpected MCP tool failure", extra={"tool": params.name})
            return tool_result(
                {
                    "code": "INTERNAL_ERROR",
                    "message": "Unexpected server error",
                    "retryable": False,
                    "details": {},
                },
                error=True,
            )

    server = Server(
        "ComfyUI MCP Skills",
        version=__version__,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_resources=resource_handlers.list_resources,
        on_list_resource_templates=resource_handlers.list_templates,
        on_read_resource=resource_handlers.read_resource,
        on_list_prompts=prompt_handlers.list_prompts,
        on_get_prompt=prompt_handlers.get_prompt,
        on_completion=complete_reference,
        lifespan=lifespan,
        on_subscriptions_listen=authorized_listen,
    )
    server.extensions[UI_EXTENSION_ID] = {}
    return server
