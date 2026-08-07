"""Separate MCP server for dangerous workflow administration."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from mcp.server import Server, ServerRequestContext
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)
from mcp_types import INVALID_PARAMS

from comfyui_mcp_skills import __version__
from comfyui_mcp_skills.adapters.mcp.admin_control import (
    PHASE_O_TOOL_NAMES,
    AdminOutboxRuntime,
    _resource_ref,
    approval_dict,
    config_bundle_dict,
    create_admin_resource_handlers,
    dependency_plan_dict,
    dependency_report_dict,
    phase_o_tools,
    plan_dict,
    provisioning_job_dict,
    server_dict,
    server_page_dict,
)
from comfyui_mcp_skills.adapters.mcp.tooling import decorate_tool
from comfyui_mcp_skills.application.admin import MAX_ADMIN_REQUEST_ID_LENGTH, WorkflowAdmin
from comfyui_mcp_skills.application.authorization import (
    AuthorizationContext,
    Scope,
    Toolset,
    is_authorized,
    scopes_for_resource,
    scopes_for_tool,
)
from comfyui_mcp_skills.application.ports import ComfyUIGateway
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.application.telemetry import (
    Meter,
    Tracer,
    meter_from_env,
    tracer_from_env,
)
from comfyui_mcp_skills.application.workflow_change import WorkflowChangeService
from comfyui_mcp_skills.application.workflow_graph import (
    WorkflowGraphService,
    WorkflowValidationService,
)
from comfyui_mcp_skills.application.workflow_import import WorkflowImportService
from comfyui_mcp_skills.domain.errors import ComfyUISkillsError
from comfyui_mcp_skills.domain.workflow_schema import (
    build_input_schema,
    normalize_parameters,
    validate_parameter_targets,
)
from comfyui_mcp_skills.domain.workflow_semantics import (
    DependencyExtractorRegistry,
    ParameterRoleRegistry,
)
from comfyui_mcp_skills.infrastructure.comfyui.client import ComfyUIClient
from comfyui_mcp_skills.infrastructure.comfyui.gateway import create_gateway
from comfyui_mcp_skills.infrastructure.persistence.repository_factory import (
    RepositoryBundle,
    create_repository_bundle,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_workflows import (
    SQLiteWorkflowRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.store_fencing import LegacyStoreSwitched
from comfyui_mcp_skills.infrastructure.persistence.workflow_changes import (
    SQLiteWorkflowChangeRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.workflows import FileWorkflowRepository

GatewayFactory = Callable[[dict[str, Any]], ComfyUIGateway]
logger = logging.getLogger(__name__)


def create_admin_server(
    base_dir: Path,
    *,
    enabled: bool = False,
    actor: str = "stdio-admin",
    repositories: RepositoryBundle | None = None,
    gateway_factory: GatewayFactory = create_gateway,
    authorization: AuthorizationContext | None = None,
    server_control: Any = None,
    config_bundles: Any = None,
    dependency_provisioning: Any = None,
    provisioning_repository: Any = None,
    tracer: Tracer | None = None,
    meter: Meter | None = None,
    upload_roots: list[Path] | None = None,
) -> Server[dict[str, object]]:
    if not enabled:
        raise PermissionError("Admin MCP requires an explicit enabled=True configuration")
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
    authorization = authorization or AuthorizationContext(
        actor, frozenset({Scope.CONFIGURE, Scope.PROVISION, Scope.AUDIT}), Toolset.ADMIN
    )
    owner_id = authorization.principal_id
    file_workflow_available = True
    try:
        admin = WorkflowAdmin(
            base_dir,
            FileWorkflowRepository(base_dir),
            actor=actor,
        )
    except LegacyStoreSwitched:
        # After the workflow aggregate cutover the file store is fenced. The
        # audit trail (JSONL) stays available; only the file-backed
        # workflow.set_enabled/delete tools are hidden.
        file_workflow_available = False
        admin = WorkflowAdmin(
            base_dir,
            None,
            actor=actor,
        )
    repositories = repositories or create_repository_bundle(base_dir)
    provisioning_repository = provisioning_repository or getattr(repositories, "provisioning", None)
    phase_o_surface = phase_o_tools(
        servers_available=server_control is not None,
        config_available=config_bundles is not None,
        dependencies_available=dependency_provisioning is not None,
    )
    phase_o_surface = [
        tool
        for tool in phase_o_surface
        if is_authorized(authorization.scopes, scopes_for_tool(tool.name))
    ]
    phase_o_resources = (
        create_admin_resource_handlers(
            provisioning_repository,
            owner_id,
            authorization.scopes,
        )
        if provisioning_repository is not None
        else None
    )
    subscription_bus = InMemorySubscriptionBus()
    listen_handler = ListenHandler(subscription_bus, max_subscriptions=64, max_buffered_events=256)
    phase_o_runtime = (
        AdminOutboxRuntime(
            provisioning_repository,
            subscription_bus,
            owner_id=owner_id,
        )
        if provisioning_repository is not None
        and all(
            hasattr(provisioning_repository, method)
            for method in ("pending_outbox", "mark_outbox_delivered")
        )
        and {Scope.CONFIGURE, Scope.PROVISION, Scope.AUDIT} <= authorization.scopes
        else None
    )
    servers = ServerRegistry(base_dir)
    store = repositories.store
    workflow_owner_id = owner_id if provisioning_repository is not None else None
    workflow_actor = owner_id if provisioning_repository is not None else actor
    validation_service = WorkflowValidationService()
    workflow_graphs = WorkflowGraphService(
        ParameterRoleRegistry.default(), DependencyExtractorRegistry.default()
    )
    workflow_import = None
    if repositories.workflow_store == "sqlite" and store is not None:
        workflow_import = WorkflowImportService(
            workflow_graphs,
            validation_service,
            SQLiteWorkflowRepository(store, owner_id=workflow_owner_id),
            runtime_estimator=lambda server_id, _graph: float(
                servers.connection(server_id).get("experiment_trusted_seconds_per_run", 300.0)
            ),
        )
    workflow_changes = None
    if store is not None and repositories.workflow_store == "sqlite":
        workflow_changes = WorkflowChangeService(
            SQLiteWorkflowChangeRepository(store, owner_id=workflow_owner_id),
            workflow_graphs,
            validation_service,
            actor=workflow_actor,
        )

    async def list_tools(
        _ctx: ServerRequestContext[dict[str, object]],
        _params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        identity = {
            "server_id": {"type": "string", "minLength": 1},
            "workflow_id": {"type": "string", "minLength": 1},
        }
        request_id = {
            "request_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_ADMIN_REQUEST_ID_LENGTH,
                "description": "Stable caller-supplied idempotency and audit request ID.",
            }
        }
        return ListToolsResult(
            tools=[
                decorate_tool(tool)
                for tool in [
                    *(
                        [
                            Tool(
                                name="comfyui.admin.workflow.import",
                                description=(
                                    "Preview an API or Editor workflow import and optionally "
                                    "commit one validated, unpublished Revision."
                                ),
                                input_schema={
                                    "type": "object",
                                    "properties": {
                                        **identity,
                                        "source": {
                                            "anyOf": [
                                                {
                                                    "type": "object",
                                                    "description": "Inline workflow JSON",
                                                    "properties": {
                                                        "kind": {"enum": ["inline_json"]},
                                                        "workflow": {"type": "object"},
                                                    },
                                                    "required": ["kind", "workflow"],
                                                    "additionalProperties": False,
                                                },
                                                {
                                                    "type": "object",
                                                    "description": (
                                                        "Read one workflow from ComfyUI userdata"
                                                    ),
                                                    "properties": {
                                                        "kind": {"enum": ["server_userdata"]},
                                                        "path": {"type": "string"},
                                                    },
                                                    "required": ["kind", "path"],
                                                    "additionalProperties": False,
                                                },
                                                {
                                                    "type": "object",
                                                    "description": (
                                                        "Read one workflow JSON from an "
                                                        "authorized local upload root"
                                                    ),
                                                    "properties": {
                                                        "kind": {
                                                            "enum": ["authorized_local_file"]
                                                        },
                                                        "path": {"type": "string"},
                                                    },
                                                    "required": ["kind", "path"],
                                                    "additionalProperties": False,
                                                },
                                                {
                                                    "type": "object",
                                                    "not": {"required": ["kind"]},
                                                    "description": (
                                                        "Legacy inline form: the workflow "
                                                        "object itself (no kind key)"
                                                    ),
                                                },
                                            ]
                                        },
                                        "media_type": {
                                            "type": "string",
                                            "enum": ["image", "audio", "video"],
                                            "default": "image",
                                        },
                                        "commit": {"type": "boolean", "default": False},
                                    },
                                    "required": ["server_id", "workflow_id", "source"],
                                    "additionalProperties": False,
                                },
                                output_schema={"type": "object"},
                                annotations=ToolAnnotations(
                                    read_only_hint=False,
                                    destructive_hint=False,
                                    idempotent_hint=True,
                                    open_world_hint=True,
                                ),
                            )
                        ]
                        if workflow_import is not None
                        else []
                    ),
                    *(
                        [
                            Tool(
                                name="comfyui.admin.workflow.change.plan",
                                description=(
                                    "Plan validated graph operations against a published Revision."
                                ),
                                input_schema={
                                    "type": "object",
                                    "properties": {
                                        **identity,
                                        "operations": {
                                            "type": "array",
                                            "minItems": 1,
                                            "maxItems": 100,
                                            "items": {"type": "object"},
                                        },
                                    },
                                    "required": ["server_id", "workflow_id", "operations"],
                                    "additionalProperties": False,
                                },
                                output_schema={"type": "object"},
                                annotations=ToolAnnotations(
                                    read_only_hint=False,
                                    destructive_hint=False,
                                    idempotent_hint=False,
                                    open_world_hint=True,
                                ),
                            ),
                            Tool(
                                name="comfyui.admin.workflow.change.commit",
                                description=(
                                    "Commit a bound, unexpired change plan as an unpublished "
                                    "Revision."
                                ),
                                input_schema={
                                    "type": "object",
                                    "properties": {
                                        "plan_id": {"type": "string", "minLength": 1},
                                        "plan_digest": {
                                            "type": "string",
                                            "minLength": 64,
                                            "maxLength": 64,
                                        },
                                    },
                                    "required": ["plan_id", "plan_digest"],
                                    "additionalProperties": False,
                                },
                                output_schema={"type": "object"},
                                annotations=ToolAnnotations(
                                    read_only_hint=False,
                                    destructive_hint=False,
                                    idempotent_hint=True,
                                    open_world_hint=False,
                                ),
                            ),
                            Tool(
                                name="comfyui.admin.workflow.publish",
                                description="Atomically publish one validated Workflow Deployment.",
                                input_schema={
                                    "type": "object",
                                    "properties": {
                                        "deployment_id": {"type": "string", "minLength": 1}
                                    },
                                    "required": ["deployment_id"],
                                    "additionalProperties": False,
                                },
                                output_schema={"type": "object"},
                                annotations=ToolAnnotations(
                                    read_only_hint=False,
                                    destructive_hint=True,
                                    idempotent_hint=True,
                                    open_world_hint=False,
                                ),
                            ),
                            Tool(
                                name="comfyui.admin.workflow.rollback",
                                description=(
                                    "Create and publish a new Revision from a historical target."
                                ),
                                input_schema={
                                    "type": "object",
                                    "properties": {
                                        **identity,
                                        **request_id,
                                        "target_revision_id": {"type": "string", "minLength": 1},
                                    },
                                    "required": [
                                        "server_id",
                                        "workflow_id",
                                        "target_revision_id",
                                        "request_id",
                                    ],
                                    "additionalProperties": False,
                                },
                                output_schema={"type": "object"},
                                annotations=ToolAnnotations(
                                    read_only_hint=False,
                                    destructive_hint=True,
                                    idempotent_hint=True,
                                    open_world_hint=False,
                                ),
                            ),
                        ]
                        if workflow_changes is not None
                        else []
                    ),
                    Tool(
                        name="comfyui.admin.workflow.validate",
                        description=(
                            "Validate one workflow graph, parameters, nodes, and model "
                            "dependencies without executing it. Requires a published "
                            "deployment; enabled-only enforcement applies in SQLite "
                            "store mode, while legacy file mode validates the stored "
                            "schema as-is."
                        ),
                        input_schema={
                            "type": "object",
                            "properties": identity,
                            "required": ["server_id", "workflow_id"],
                            "additionalProperties": False,
                        },
                        output_schema={"type": "object"},
                        annotations=ToolAnnotations(
                            read_only_hint=True,
                            destructive_hint=False,
                            idempotent_hint=True,
                            open_world_hint=True,
                        ),
                    ),
                    *(
                        [
                            Tool(
                                name="comfyui.admin.workflow.set_enabled",
                                description="Enable or disable one configured workflow.",
                                input_schema={
                                    "type": "object",
                                    "properties": {
                                        **identity,
                                        **request_id,
                                        "enabled": {"type": "boolean"},
                                    },
                                    "required": ["server_id", "workflow_id", "enabled"],
                                    "additionalProperties": False,
                                },
                                output_schema={"type": "object"},
                                annotations=ToolAnnotations(
                                    read_only_hint=False,
                                    destructive_hint=False,
                                    idempotent_hint=True,
                                    open_world_hint=False,
                                ),
                            ),
                            Tool(
                                name="comfyui.admin.workflow.delete",
                                description=(
                                    "Permanently delete one workflow after an exact confirmation "
                                    "phrase. Supply request_id to make retries idempotent."
                                ),
                                input_schema={
                                    "type": "object",
                                    "properties": {
                                        **identity,
                                        **request_id,
                                        "confirmation": {"type": "string"},
                                    },
                                    "required": [
                                        "server_id",
                                        "workflow_id",
                                        "confirmation",
                                        "request_id",
                                    ],
                                    "additionalProperties": False,
                                },
                                output_schema={"type": "object"},
                                annotations=ToolAnnotations(
                                    read_only_hint=False,
                                    destructive_hint=True,
                                    idempotent_hint=True,
                                    open_world_hint=False,
                                ),
                            ),
                        ]
                        if file_workflow_available
                        else []
                    ),
                    Tool(
                        name="comfyui.admin.audit.get",
                        description="Read the durable commit and audit status of an admin request.",
                        input_schema={
                            "type": "object",
                            "properties": request_id,
                            "required": ["request_id"],
                            "additionalProperties": False,
                        },
                        output_schema={"type": "object"},
                        annotations=ToolAnnotations(
                            read_only_hint=True,
                            destructive_hint=False,
                            idempotent_hint=True,
                            open_world_hint=False,
                        ),
                    ),
                    Tool(
                        name="comfyui.admin.audit.retry",
                        description=(
                            "Retry only a pending audit outcome without repeating its operation."
                        ),
                        input_schema={
                            "type": "object",
                            "properties": request_id,
                            "required": ["request_id"],
                            "additionalProperties": False,
                        },
                        output_schema={"type": "object"},
                        annotations=ToolAnnotations(
                            read_only_hint=False,
                            destructive_hint=False,
                            idempotent_hint=True,
                            open_world_hint=False,
                        ),
                    ),
                    Tool(
                        name="comfyui.admin.audit.export",
                        description=(
                            "Export a bounded, filterable slice of the durable admin audit "
                            "trail in append order."
                        ),
                        input_schema={
                            "type": "object",
                            "properties": {
                                "actor": {"type": "string", "maxLength": 128, "default": ""},
                                "action": {"type": "string", "maxLength": 256, "default": ""},
                                "outcomes": {
                                    "type": "array",
                                    "items": {
                                        "type": "string",
                                        "enum": ["intent", "success", "failure"],
                                    },
                                    "maxItems": 3,
                                    "default": [],
                                },
                                "after": {
                                    "type": "string",
                                    "description": "UTC ISO-8601 lower bound (inclusive)",
                                    "maxLength": 64,
                                    "default": "",
                                },
                                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                                "cursor": {
                                    "type": "string",
                                    "description": "Opaque next-page cursor from a prior call",
                                    "maxLength": 64,
                                    "default": "",
                                },
                            },
                            "additionalProperties": False,
                        },
                        output_schema={"type": "object"},
                        annotations=ToolAnnotations(
                            read_only_hint=True,
                            destructive_hint=False,
                            idempotent_hint=True,
                            open_world_hint=False,
                        ),
                    ),
                    *phase_o_surface,
                ]
                if is_authorized(authorization.scopes, scopes_for_tool(tool.name))
            ],
            cache_scope="private",
        )

    async def call_tool(
        ctx: ServerRequestContext[dict[str, object]], params: CallToolRequestParams
    ) -> CallToolResult:
        started = time.perf_counter()
        failed = False
        with tracer.span(
            "tool.call", {"tool": params.name, "owner": authorization.principal_id}
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
                    tool_errors.add(1, {"tool": params.name, "owner": authorization.principal_id})
                span.set_attributes(attributes)
                tool_calls.add(1, {"tool": params.name, "owner": authorization.principal_id})
                tool_duration.record(
                    elapsed, {"tool": params.name, "owner": authorization.principal_id}
                )
            return result

    async def _dispatch_tool_call(
        ctx: ServerRequestContext[dict[str, object]], params: CallToolRequestParams
    ) -> CallToolResult:
        arguments = dict(params.arguments or {})
        context_request_id = "" if ctx.request_id is None else str(ctx.request_id)
        if not is_authorized(authorization.scopes, scopes_for_tool(params.name)):
            raise MCPError(code=INVALID_PARAMS, message="Tool unavailable")
        try:
            if params.name == "comfyui.admin.workflow.import":
                if workflow_import is None:
                    raise MCPError(code=INVALID_PARAMS, message="Workflow import unavailable")
                _validate_keys(
                    arguments,
                    {"server_id", "workflow_id", "source", "media_type", "commit"},
                )
                server_id = _required_string(arguments, "server_id")
                workflow_id = _required_string(arguments, "workflow_id")
                source = arguments.get("source")
                if not isinstance(source, dict):
                    raise TypeError("source must be an object")
                kind = source.get("kind", "inline_json")
                if kind not in {"inline_json", "server_userdata", "authorized_local_file"}:
                    raise TypeError(
                        "source.kind must be inline_json, server_userdata, or "
                        "authorized_local_file"
                    )
                workflow_source: dict[str, Any]
                if kind == "inline_json":
                    if "kind" not in source:
                        workflow_source = source  # legacy inline workflow object
                    else:
                        candidate = source.get("workflow")
                        if not isinstance(candidate, dict):
                            raise TypeError(
                                "source.workflow must be an object for inline_json"
                            )
                        workflow_source = candidate
                elif kind == "server_userdata":
                    workflow_path = source.get("path")
                    if not isinstance(workflow_path, str) or not _safe_userdata_path(
                        workflow_path
                    ):
                        raise TypeError(
                            "source.path must be a relative workflows/*.json path "
                            "for server_userdata"
                        )
                    config = servers.connection(server_id)
                    userdata_client = ComfyUIClient(
                        server_url=str(config.get("url", "http://127.0.0.1:8188")),
                        auth=str(config.get("auth", "")),
                        comfy_api_key=str(config.get("comfy_api_key", "")),
                        timeout=float(config.get("timeout", 30.0)),
                    )
                    fetched = await anyio.to_thread.run_sync(
                        lambda: userdata_client.read_userdata_workflow(workflow_path)
                    )
                    if fetched is None:
                        raise LookupError(f"userdata workflow not found: {workflow_path}")
                    workflow_source = fetched
                else:
                    workflow_source = _read_authorized_local_workflow(
                        base_dir, source, upload_roots
                    )
                media_type = arguments.get("media_type", "image")
                if media_type not in {"image", "audio", "video"}:
                    raise TypeError("media_type must be image, audio, or video")
                commit = arguments.get("commit", False)
                if not isinstance(commit, bool):
                    raise TypeError("commit must be a boolean")
                gateway = gateway_factory(servers.connection(server_id))
                object_info = await anyio.to_thread.run_sync(gateway.get_object_info)
                replacement_reader = getattr(gateway, "get_node_replacements", None)
                if callable(replacement_reader):
                    try:
                        replacements = await anyio.to_thread.run_sync(replacement_reader)
                    except ComfyUISkillsError:
                        replacements = {}
                else:
                    replacements = {}
                if not isinstance(replacements, dict):
                    replacements = {}
                preview = await anyio.to_thread.run_sync(
                    lambda: workflow_import.preview(
                        workflow_source,
                        workflow_id=workflow_id,
                        server_id=server_id,
                        object_info=object_info,
                        media_type=str(media_type),
                        node_replacements={
                            str(old): str(new)
                            for old, new in replacements.items()
                            if isinstance(old, str) and isinstance(new, str)
                        },
                    )
                )
                result = preview.to_public_dict()
                if commit:
                    result["commit"] = await anyio.to_thread.run_sync(
                        lambda: workflow_import.commit(preview)
                    )
            elif params.name == "comfyui.admin.workflow.change.plan":
                if workflow_changes is None:
                    raise MCPError(code=INVALID_PARAMS, message="Workflow change unavailable")
                _validate_keys(arguments, {"server_id", "workflow_id", "operations"})
                server_id = _required_string(arguments, "server_id")
                workflow_id = _required_string(arguments, "workflow_id")
                operations = arguments.get("operations")
                if not isinstance(operations, list) or not all(
                    isinstance(operation, dict) for operation in operations
                ):
                    raise TypeError("operations must be an array of objects")
                gateway = gateway_factory(servers.connection(server_id))
                object_info = await anyio.to_thread.run_sync(gateway.get_object_info)
                result = await anyio.to_thread.run_sync(
                    lambda: workflow_changes.plan(
                        workflow_id,
                        server_id,
                        operations,
                        object_info=object_info,
                    )
                )
            elif params.name == "comfyui.admin.workflow.change.commit":
                if workflow_changes is None:
                    raise MCPError(code=INVALID_PARAMS, message="Workflow change unavailable")
                _validate_keys(arguments, {"plan_id", "plan_digest"})
                plan_id = _required_string(arguments, "plan_id")
                plan_digest = _required_string(arguments, "plan_digest")
                result = await anyio.to_thread.run_sync(
                    lambda: workflow_changes.commit(plan_id, plan_digest)
                )
            elif params.name == "comfyui.admin.workflow.publish":
                if workflow_changes is None:
                    raise MCPError(code=INVALID_PARAMS, message="Workflow publish unavailable")
                _validate_keys(arguments, {"deployment_id"})
                deployment_id = _required_string(arguments, "deployment_id")
                result = await anyio.to_thread.run_sync(
                    lambda: workflow_changes.publish(deployment_id)
                )
            elif params.name == "comfyui.admin.workflow.rollback":
                if workflow_changes is None:
                    raise MCPError(code=INVALID_PARAMS, message="Workflow rollback unavailable")
                _validate_keys(
                    arguments,
                    {"server_id", "workflow_id", "target_revision_id", "request_id"},
                )
                server_id = _required_string(arguments, "server_id")
                workflow_id = _required_string(arguments, "workflow_id")
                target_revision_id = _required_string(arguments, "target_revision_id")
                request_id = _required_string(arguments, "request_id")
                result = await anyio.to_thread.run_sync(
                    lambda: workflow_changes.rollback(
                        workflow_id,
                        server_id,
                        target_revision_id,
                        request_id,
                    )
                )
            elif params.name == "comfyui.admin.workflow.validate":
                _validate_keys(arguments, {"server_id", "workflow_id"})
                server_id = _required_string(arguments, "server_id")
                workflow_id = _required_string(arguments, "workflow_id")
                result = await anyio.to_thread.run_sync(
                    lambda: _validate_published_workflow(
                        repositories.workflows,
                        workflow_graphs,
                        validation_service,
                        gateway_factory,
                        servers,
                        server_id,
                        workflow_id,
                    )
                )
            elif params.name == "comfyui.admin.workflow.set_enabled":
                if not file_workflow_available:
                    raise MCPError(
                        code=INVALID_PARAMS, message="Workflow admin unavailable after cutover"
                    )
                _validate_keys(arguments, {"server_id", "workflow_id", "enabled", "request_id"})
                server_id = _required_string(arguments, "server_id")
                workflow_id = _required_string(arguments, "workflow_id")
                enabled_value = arguments.get("enabled")
                if not isinstance(enabled_value, bool):
                    raise TypeError("enabled must be a boolean")
                request_id = _optional_request_id(arguments, context_request_id)
                result = await anyio.to_thread.run_sync(
                    lambda: admin.set_enabled(
                        server_id,
                        workflow_id,
                        enabled_value,
                        request_id=request_id,
                    )
                )
            elif params.name == "comfyui.admin.workflow.delete":
                if not file_workflow_available:
                    raise MCPError(
                        code=INVALID_PARAMS, message="Workflow admin unavailable after cutover"
                    )
                _validate_keys(
                    arguments,
                    {"server_id", "workflow_id", "confirmation", "request_id"},
                )
                server_id = _required_string(arguments, "server_id")
                workflow_id = _required_string(arguments, "workflow_id")
                confirmation = _required_string(arguments, "confirmation")
                request_id = _required_string(arguments, "request_id")
                result = await anyio.to_thread.run_sync(
                    lambda: admin.delete(
                        server_id,
                        workflow_id,
                        confirmation,
                        request_id=request_id,
                    )
                )
            elif params.name == "comfyui.admin.audit.get":
                _validate_keys(arguments, {"request_id"})
                request_id = _required_string(arguments, "request_id")
                result = await anyio.to_thread.run_sync(lambda: admin.get_audit_status(request_id))
            elif params.name == "comfyui.admin.audit.retry":
                _validate_keys(arguments, {"request_id"})
                request_id = _required_string(arguments, "request_id")
                result = await anyio.to_thread.run_sync(lambda: admin.retry_audit(request_id))
            elif params.name == "comfyui.admin.audit.export":
                _validate_keys(
                    arguments, {"actor", "action", "outcomes", "after", "limit", "cursor"}
                )
                result = await anyio.to_thread.run_sync(
                    lambda: admin.export_audit(
                        actor=_optional_string(arguments, "actor", ""),
                        action=_optional_string(arguments, "action", ""),
                        outcomes=arguments.get("outcomes"),
                        after=_optional_string(arguments, "after", ""),
                        limit=arguments.get("limit", 100),
                        cursor=_optional_string(arguments, "cursor", ""),
                    )
                )
            elif params.name in PHASE_O_TOOL_NAMES:
                if params.name.startswith("comfyui.admin.server."):
                    if server_control is None:
                        raise MCPError(code=INVALID_PARAMS, message="Server control unavailable")
                elif params.name.startswith("comfyui.admin.config."):
                    if config_bundles is None:
                        raise MCPError(code=INVALID_PARAMS, message="Config bundles unavailable")
                elif dependency_provisioning is None:
                    raise MCPError(
                        code=INVALID_PARAMS, message="Dependency provisioning unavailable"
                    )
                if params.name == "comfyui.admin.server.list":
                    _validate_keys(arguments, {"limit", "cursor"})
                    result = await anyio.to_thread.run_sync(
                        lambda: server_page_dict(
                            server_control.list(
                                owner_id,
                                limit=arguments.get("limit", 50),
                                cursor=arguments.get("cursor", ""),
                            )
                        )
                    )
                elif params.name == "comfyui.admin.server.inspect":
                    _validate_keys(arguments, {"server_id"})
                    result = await anyio.to_thread.run_sync(
                        lambda: server_dict(
                            server_control.inspect(
                                _required_string(arguments, "server_id"), owner_id
                            )
                        )
                    )
                elif params.name in {
                    "comfyui.admin.server.upsert",
                    "comfyui.admin.server.set_enabled",
                    "comfyui.admin.server.set_default",
                    "comfyui.admin.server.delete",
                }:
                    operation = params.name.rsplit(".", 1)[1]
                    phase = _required_string(arguments, "phase")
                    if phase == "plan":
                        _validate_keys(
                            arguments,
                            {"phase", "server_id", "changes", "expected_revision"},
                        )
                        changes = dict(arguments.get("changes", {}))
                        if "expected_revision" in arguments:
                            changes["expected_revision"] = arguments["expected_revision"]
                        result = await anyio.to_thread.run_sync(
                            lambda: plan_dict(
                                server_control.plan(
                                    operation,
                                    _required_string(arguments, "server_id"),
                                    owner_id,
                                    changes,
                                )
                            )
                        )
                    elif phase == "commit":
                        _validate_keys(arguments, {"phase", "plan_id", "plan_digest"})
                        result = await anyio.to_thread.run_sync(
                            lambda: server_dict(
                                server_control.commit(
                                    _required_string(arguments, "plan_id"),
                                    _required_string(arguments, "plan_digest"),
                                    owner_id,
                                )
                            )
                        )
                    else:
                        raise ValueError("phase must be plan or commit")
                elif params.name == "comfyui.admin.config.export":
                    _validate_keys(arguments, {"revision"})
                    result = await anyio.to_thread.run_sync(
                        lambda: config_bundle_dict(
                            config_bundles.export(owner_id, arguments.get("revision", ""))
                        )
                    )
                elif params.name == "comfyui.admin.config.import":
                    phase = _required_string(arguments, "phase")
                    if phase == "plan":
                        _validate_keys(arguments, {"phase", "bundle", "expected_revision"})
                        result = await anyio.to_thread.run_sync(
                            lambda: plan_dict(
                                config_bundles.plan_import(
                                    arguments["bundle"], arguments["expected_revision"], owner_id
                                )
                            )
                        )
                    elif phase == "commit":
                        _validate_keys(arguments, {"phase", "plan_id", "plan_digest"})
                        result = await anyio.to_thread.run_sync(
                            lambda: config_bundle_dict(
                                config_bundles.commit_import(
                                    _required_string(arguments, "plan_id"),
                                    _required_string(arguments, "plan_digest"),
                                    owner_id,
                                )
                            )
                        )
                    else:
                        raise ValueError("phase must be plan or commit")
                elif params.name == "comfyui.admin.dependency.inspect":
                    _validate_keys(
                        arguments,
                        {"server_id", "requirements", "workflow_id", "revision_id"},
                    )
                    result = await anyio.to_thread.run_sync(
                        lambda: dependency_report_dict(
                            dependency_provisioning.inspect(
                                _required_string(arguments, "server_id"),
                                owner_id,
                                arguments.get("requirements"),
                                workflow_id=arguments.get("workflow_id", ""),
                                revision_id=arguments.get("revision_id", ""),
                            )
                        )
                    )
                elif params.name == "comfyui.admin.dependency.plan":
                    _validate_keys(arguments, {"server_id", "requirements"})
                    result = await anyio.to_thread.run_sync(
                        lambda: dependency_plan_dict(
                            dependency_provisioning.plan(
                                _required_string(arguments, "server_id"),
                                owner_id,
                                arguments["requirements"],
                            )
                        )
                    )
                elif params.name == "comfyui.admin.dependency.install":
                    _validate_keys(
                        arguments,
                        {"plan_id", "plan_digest", "approval_id", "request_id", "confirmation"},
                    )
                    if arguments.get("confirmation") != "INSTALL APPROVED DEPENDENCIES":
                        raise ValueError("Invalid installation confirmation")
                    result = await anyio.to_thread.run_sync(
                        lambda: provisioning_job_dict(
                            dependency_provisioning.commit(
                                _required_string(arguments, "plan_id"),
                                _required_string(arguments, "plan_digest"),
                                _required_string(arguments, "approval_id"),
                                owner_id,
                                _required_string(arguments, "request_id"),
                                arguments["confirmation"],
                            )
                        )
                    )
                elif params.name == "comfyui.admin.provisioning.cancel":
                    phase = _required_string(arguments, "phase")
                    if phase == "plan":
                        _validate_keys(arguments, {"phase", "job_id"})
                        result = await anyio.to_thread.run_sync(
                            lambda: plan_dict(
                                dependency_provisioning.plan_cancel(
                                    _required_string(arguments, "job_id"), owner_id
                                )
                            )
                        )
                    elif phase == "commit":
                        _validate_keys(arguments, {"phase", "plan_id", "plan_digest"})
                        result = await anyio.to_thread.run_sync(
                            lambda: provisioning_job_dict(
                                dependency_provisioning.commit_cancel(
                                    _required_string(arguments, "plan_id"),
                                    _required_string(arguments, "plan_digest"),
                                    owner_id,
                                )
                            )
                        )
                    else:
                        raise ValueError("phase must be plan or commit")
                elif params.name == "comfyui.admin.approval.get":
                    _validate_keys(arguments, {"approval_id"})
                    result = await anyio.to_thread.run_sync(
                        lambda: approval_dict(
                            dependency_provisioning.get_approval(
                                _required_string(arguments, "approval_id"), owner_id
                            )
                        )
                    )
                elif params.name == "comfyui.admin.approval.decision.plan":
                    _validate_keys(arguments, {"approval_id", "decision", "reason"})
                    result = await anyio.to_thread.run_sync(
                        lambda: plan_dict(
                            dependency_provisioning.plan_approval(
                                _required_string(arguments, "approval_id"),
                                arguments["decision"],
                                owner_id,
                                arguments.get("reason", ""),
                            )
                        )
                    )
                elif params.name == "comfyui.admin.approval.decision.commit":
                    _validate_keys(arguments, {"plan_id", "plan_digest"})
                    result = await anyio.to_thread.run_sync(
                        lambda: approval_dict(
                            dependency_provisioning.commit_approval(
                                _required_string(arguments, "plan_id"),
                                _required_string(arguments, "plan_digest"),
                                owner_id,
                            )
                        )
                    )
                else:
                    _validate_keys(arguments, {"job_id"})
                    result = await anyio.to_thread.run_sync(
                        lambda: provisioning_job_dict(
                            dependency_provisioning.get_job(
                                _required_string(arguments, "job_id"), owner_id
                            )
                        )
                    )
            else:
                raise MCPError(
                    code=INVALID_PARAMS,
                    message=f"Unknown tool: {params.name}",
                )
            return _result(result)
        except MCPError:
            raise
        except (ComfyUISkillsError, KeyError, TypeError, ValueError, LookupError) as exc:
            error = (
                exc.as_dict()
                if isinstance(exc, ComfyUISkillsError)
                else {
                    "code": "NOT_FOUND" if isinstance(exc, LookupError) else "INVALID_ARGUMENTS",
                    "message": "Resource not found" if isinstance(exc, LookupError) else str(exc),
                }
            )
            return _result(error, error=True)
        except Exception:
            logger.exception("Unexpected admin MCP tool failure", extra={"tool": params.name})
            return _result(
                {
                    "code": "INTERNAL_ERROR",
                    "message": "Unexpected server error",
                    "retryable": False,
                    "details": {},
                },
                error=True,
            )

    async def authorized_listen(ctx: Any, params: Any) -> Any:
        if provisioning_repository is None:
            raise MCPError(code=INVALID_PARAMS, message="Resource unavailable")
        for value in params.notifications.resource_subscriptions or ():
            try:
                kind, identity = _resource_ref(str(value))
                resource_kind = {
                    "server": "admin_server",
                    "bundle": "admin_bundle",
                    "plan": "admin_plan",
                    "approval": "admin_approval",
                    "job": "admin_provisioning",
                }[kind]
                if not is_authorized(authorization.scopes, scopes_for_resource(resource_kind)):
                    raise LookupError("resource unavailable")
                getter = {
                    "server": provisioning_repository.get_server,
                    "bundle": provisioning_repository.get_bundle,
                    "plan": provisioning_repository.get_plan,
                    "approval": provisioning_repository.get_approval,
                    "job": provisioning_repository.get_job,
                }[kind]
                resource = await anyio.to_thread.run_sync(getter, identity, owner_id)
                if resource is None:
                    raise LookupError("resource unavailable")
            except Exception as exc:
                raise MCPError(code=INVALID_PARAMS, message="Resource unavailable") from exc
        return await listen_handler(ctx, params)

    @asynccontextmanager
    async def lifespan(_server: Server[dict[str, object]]) -> Any:
        async with anyio.create_task_group() as task_group:
            if phase_o_runtime is not None:
                task_group.start_soon(phase_o_runtime.run)
            try:
                yield {}
            finally:
                listen_handler.close()
                task_group.cancel_scope.cancel()

    resource_kwargs: dict[str, Any] = {}
    if phase_o_resources is not None:
        resource_kwargs = {
            "on_list_resources": phase_o_resources.list_resources,
            "on_list_resource_templates": phase_o_resources.list_templates,
            "on_read_resource": phase_o_resources.read_resource,
        }
    else:
        resource_kwargs = {}

    return Server(
        "ComfyUI MCP Skills Admin",
        version=__version__,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        lifespan=lifespan,
        on_subscriptions_listen=authorized_listen,
        **resource_kwargs,
    )


_USERDATA_PATH = re.compile(r"^workflows/[A-Za-z0-9][A-Za-z0-9_.-]*\.json$")

# A folder inventory larger than this is treated as unreadable rather than
# compared exhaustively; documented so the cap cannot be mistaken for a bug.
_MODEL_INVENTORY_LIMIT = 10_000


def _safe_userdata_path(value: object) -> bool:
    """Userdata paths must be relative workflows/*.json with safe characters."""
    return isinstance(value, str) and _USERDATA_PATH.fullmatch(value) is not None


def _read_authorized_local_workflow(
    base_dir: Path, source: dict[str, Any], upload_roots: list[Path] | None = None
) -> dict[str, Any]:
    """Read an inline workflow JSON only from authorized upload roots.

    Mirrors the AssetService read discipline: stat, open, verify the same
    unmodified file via fstat, read with a hard size cap, and verify again
    after reading so a swapped file can never be parsed as the workflow.
    The roots are injected by the entry point; nothing here reads the
    environment, so the configured-roots-only semantics cannot be widened.
    """
    import os

    from comfyui_mcp_skills.application.assets import same_file_stat

    max_bytes = 2 * 1024 * 1024
    raw_path = source.get("path")
    if not isinstance(raw_path, str) or not raw_path or len(raw_path) > 1024:
        raise TypeError("source.path must be a non-empty string for authorized_local_file")
    if upload_roots is None:
        roots = [(base_dir / "uploads").resolve()]
    else:
        roots = [root.resolve() for root in upload_roots]
    if not roots:
        raise ValueError("no authorized upload roots are configured")
    try:
        resolved = Path(raw_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LookupError("source.path could not be resolved") from exc
    if not resolved.is_file() or not any(
        _path_within(resolved, root.resolve()) for root in roots
    ):
        raise ValueError("source.path is outside authorized upload roots")
    try:
        expected = resolved.stat()
    except OSError as exc:
        raise LookupError("source.path could not be inspected") from exc
    if expected.st_size > max_bytes:
        raise ValueError("source.path exceeds the 2 MiB workflow file limit")
    try:
        with resolved.open("r", encoding="utf-8") as handle:
            opened = os.fstat(handle.fileno())
            if not same_file_stat(expected, opened):
                raise ValueError("source.path was replaced before it could be read")
            content = handle.read(max_bytes + 1)
            after_read = os.fstat(handle.fileno())
            try:
                current = resolved.stat()
            except FileNotFoundError as exc:
                raise ValueError("source.path was replaced while being read") from exc
            if (
                len(content) > max_bytes
                or not same_file_stat(opened, after_read)
                or not same_file_stat(opened, current)
            ):
                raise ValueError("source.path was replaced while being read")
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("source.path could not be read") from exc
    try:
        parsed = json.loads(content)
    except ValueError as exc:
        raise ValueError("source.path is not a valid workflow JSON file") from exc
    if not isinstance(parsed, dict):
        raise TypeError("source.path must contain a workflow object")
    return parsed


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_published_workflow(
    repository: Any,
    graphs: WorkflowGraphService,
    validation: WorkflowValidationService,
    gateway_factory: GatewayFactory,
    servers: ServerRegistry,
    server_id: str,
    workflow_id: str,
) -> dict[str, Any]:
    """Validate one workflow graph and its dependency contract without executing."""
    workflow = repository.get(server_id, workflow_id)
    if workflow is None:
        raise LookupError(f"Workflow not found: {server_id}/{workflow_id}")
    gateway = gateway_factory(servers.connection(server_id))
    object_info = gateway.get_object_info()
    result = validation.validate_api(workflow.graph, object_info)
    semantic = graphs.describe(workflow.graph, object_info=object_info)
    dependencies = dict(semantic["dependencies"])
    missing_models, folder_errors = _missing_models(gateway, dependencies)
    dependencies["missing_models"] = sorted(set(missing_models))
    dependencies["folder_errors"] = sorted(folder_errors)
    dependencies["is_ready"] = bool(
        dependencies.get("coverage") == "complete"
        and not missing_models
        and not folder_errors
    )
    issues = list(result["issues"])
    try:
        # The store keeps normalized parameter metadata (name -> metadata);
        # re-feeding it through the schema shape re-runs the shared
        # node_id/field and metadata-object validation without any mutation.
        normalized = normalize_parameters({"parameters": workflow.parameters})
        validate_parameter_targets(normalized, workflow.graph)
        build_input_schema(normalized)
    except (TypeError, ValueError) as exc:
        issues.append(
            {
                "code": "invalid_parameter_schema",
                "message": str(exc),
                "node_id": "",
                "field": "",
            }
        )
    return {
        "workflow_id": workflow_id,
        "server_id": server_id,
        "valid": bool(result["valid"]) and not any(
            issue.get("code") == "invalid_parameter_schema" for issue in issues
        ),
        "issues": issues,
        "unsupported_nodes": sorted(result["unsupported_nodes"]),
        "node_count": int(semantic["node_count"]),
        "edge_count": int(semantic["edge_count"]),
        "dependencies": dependencies,
    }


def _missing_models(
    gateway: Any, dependencies: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Compare the extracted model contract against the server's model folders.

    A folder whose inventory cannot be read is reported as a folder error and
    forces ``is_ready=false``; an unavailable inventory is never treated as
    "no missing models".
    """
    models = dependencies.get("models")
    if not isinstance(models, list):
        return [], []
    by_folder: dict[str, list[str]] = {}
    for item in models:
        if not isinstance(item, dict):
            continue
        folder = str(item.get("folder", ""))
        filename = str(item.get("filename", ""))
        if folder and filename:
            by_folder.setdefault(folder, []).append(filename)
    missing: list[str] = []
    folder_errors: list[str] = []
    for folder, filenames in by_folder.items():
        try:
            available = gateway.get_models(folder)
            if not isinstance(available, list):
                raise TypeError("models response must be an array")
            if len(available) > _MODEL_INVENTORY_LIMIT:
                raise ValueError(
                    "models response exceeds the inventory limit "
                    f"({_MODEL_INVENTORY_LIMIT} entries)"
                )
            available_set = set(available)
        except (TypeError, ValueError, LookupError) as exc:
            folder_errors.append(f"{folder}: {exc}")
            continue
        missing.extend(filename for filename in filenames if filename not in available_set)
    return missing, folder_errors


def _required_string(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value:
        raise TypeError(f"{name} must be a non-empty string")
    if name == "request_id" and len(value) > MAX_ADMIN_REQUEST_ID_LENGTH:
        raise TypeError(f"request_id must be at most {MAX_ADMIN_REQUEST_ID_LENGTH} characters")
    return value


def _optional_request_id(arguments: dict[str, Any], fallback: str) -> str:
    if "request_id" not in arguments:
        return fallback
    return _required_string(arguments, "request_id")


def _optional_string(arguments: dict[str, Any], name: str, default: str) -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _validate_keys(arguments: dict[str, Any], allowed: set[str]) -> None:
    unexpected = set(arguments) - allowed
    if unexpected:
        raise ValueError(f"Unexpected arguments: {', '.join(sorted(unexpected))}")


def _result(value: dict[str, Any], *, error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
        structured_content=value,
        is_error=error,
    )
