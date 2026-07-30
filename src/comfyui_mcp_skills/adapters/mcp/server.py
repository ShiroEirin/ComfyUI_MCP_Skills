"""Low-level MCP server with dynamic workflow schemas and durable jobs."""

from __future__ import annotations

import logging
import math
from collections.abc import AsyncIterator, Callable
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
    Tool,
    ToolAnnotations,
)
from mcp_types import INVALID_PARAMS

from comfyui_mcp_skills import __version__
from comfyui_mcp_skills.adapters.mcp.resources import create_resource_handlers
from comfyui_mcp_skills.adapters.mcp.subscriptions import WorkflowChangeMonitor
from comfyui_mcp_skills.adapters.mcp.tooling import (
    EXECUTION_PROPERTY,
    JOB_SCHEMA,
    current_owner,
    fixed_tools,
    job_dict,
    optional_string,
    required_string,
    tool_result,
    validate_fixed_arguments,
    workflow_tool_names,
)
from comfyui_mcp_skills.application.assets import AssetService
from comfyui_mcp_skills.application.catalog import WorkflowCatalog
from comfyui_mcp_skills.application.discovery import DiscoveryService
from comfyui_mcp_skills.application.execution import ExecutionService
from comfyui_mcp_skills.application.jobs import JobService
from comfyui_mcp_skills.application.ports import ComfyUIGateway
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import ComfyUISkillsError, ServerNotFound
from comfyui_mcp_skills.domain.models import Workflow
from comfyui_mcp_skills.domain.workflow_schema import build_input_schema
from comfyui_mcp_skills.infrastructure.comfyui.gateway import create_gateway
from comfyui_mcp_skills.infrastructure.persistence.repository_factory import (
    RepositoryBundle,
    create_repository_bundle,
)
from comfyui_mcp_skills.infrastructure.persistence.resource_aliases import (
    SQLiteLegacyResourceAliasReader,
)
from comfyui_mcp_skills.infrastructure.persistence.workflows import FileWorkflowRepository

GatewayFactory = Callable[[dict[str, Any]], ComfyUIGateway]
logger = logging.getLogger(__name__)


def create_server(
    base_dir: Path,
    *,
    gateway_factory: GatewayFactory = create_gateway,
    upload_roots: list[Path] | None = None,
    max_upload_bytes: int = 100 * 1024 * 1024,
    repositories: RepositoryBundle | None = None,
) -> Server[dict[str, object]]:
    """Create an MCP server backed by one configured project directory."""
    base_dir = base_dir.resolve()
    catalog = WorkflowCatalog(FileWorkflowRepository(base_dir))
    servers = ServerRegistry(base_dir)
    repositories = repositories or create_repository_bundle(base_dir)
    run_repository = repositories.runs
    asset_repository = repositories.assets
    assets = AssetService(
        asset_repository,
        upload_roots=upload_roots if upload_roots is not None else [base_dir / "uploads"],
        max_bytes=max_upload_bytes,
    )
    execution = ExecutionService(
        catalog, servers, run_repository, asset_repository, gateway_factory
    )
    jobs = JobService(servers, run_repository, gateway_factory)
    discovery = DiscoveryService(servers, gateway_factory)

    subscription_bus = InMemorySubscriptionBus()
    listen_handler = ListenHandler(subscription_bus, max_subscriptions=64, max_buffered_events=256)
    change_monitor = WorkflowChangeMonitor(base_dir, subscription_bus)

    @asynccontextmanager
    async def lifespan(_server: Server[dict[str, object]]) -> AsyncIterator[dict[str, object]]:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(change_monitor.run)
            try:
                yield {}
            finally:
                listen_handler.close()
                task_group.cancel_scope.cancel()

    def enabled_workflows() -> list[Workflow]:
        result: list[Workflow] = []
        for workflow in catalog.list_enabled():
            try:
                servers.connection(workflow.server_id)
            except ServerNotFound:
                continue
            result.append(workflow)
        return result

    resource_aliases = (
        SQLiteLegacyResourceAliasReader(repositories.store)
        if repositories.store is not None
        and (repositories.run_store == "sqlite" or repositories.asset_store == "sqlite")
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
    )

    def current_tools() -> tuple[list[Tool], dict[str, Workflow]]:
        workflow_map = workflow_tool_names(enabled_workflows())
        tools: list[Tool] = []
        for name in sorted(workflow_map):
            workflow = workflow_map[name]
            schema = build_input_schema(workflow.parameters)
            schema["properties"]["_execution"] = EXECUTION_PROPERTY
            tools.append(
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
                        open_world_hint=False,
                    ),
                )
            )
        tools.extend(fixed_tools())
        return tools, workflow_map

    async def list_tools(
        _ctx: ServerRequestContext[dict[str, object]],
        _params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        tools, _mapping = current_tools()
        return ListToolsResult(tools=tools, ttl_ms=5_000, cache_scope="private")

    async def call_tool(
        ctx: ServerRequestContext[dict[str, object]],
        params: CallToolRequestParams,
    ) -> CallToolResult:
        arguments = dict(params.arguments or {})
        owner_id = current_owner()
        _tools, workflow_map = current_tools()
        try:
            if params.name in workflow_map:
                workflow = workflow_map[params.name]
                execution_options = arguments.pop("_execution", {})
                if not isinstance(execution_options, dict):
                    raise TypeError("_execution must be an object")
                idempotency_key = execution_options.get("idempotency_key", "")
                unexpected_options = set(execution_options) - {
                    "idempotency_key",
                    "wait",
                    "wait_timeout_seconds",
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
                await ctx.session.report_progress(1, None, "Submitting ComfyUI workflow")
                job = await anyio.to_thread.run_sync(
                    lambda: execution.submit(
                        workflow.server_id,
                        workflow.workflow_id,
                        arguments,
                        idempotency_key=idempotency_key,
                        owner_id=owner_id,
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
            if params.name == "comfyui.job.get":
                validate_fixed_arguments(arguments, {"server_id", "prompt_id"})
                server_id = required_string(arguments, "server_id")
                prompt_id = required_string(arguments, "prompt_id")
                job = await anyio.to_thread.run_sync(
                    lambda: jobs.get(server_id, prompt_id, owner_id=owner_id)
                )
                return tool_result(job_dict(job))
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
                gateway = gateway_factory(servers.connection(server_id))
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
        except (ComfyUISkillsError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ComfyUISkillsError):
                error = exc.as_dict()
            else:
                error = {
                    "code": "INVALID_ARGUMENTS",
                    "message": str(exc),
                    "retryable": False,
                    "details": {},
                }
            return tool_result(error, error=True)
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

    return Server(
        "ComfyUI MCP Skills",
        version=__version__,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_resources=resource_handlers.list_resources,
        on_list_resource_templates=resource_handlers.list_templates,
        on_read_resource=resource_handlers.read_resource,
        lifespan=lifespan,
        on_subscriptions_listen=listen_handler,
    )
