"""Low-level MCP server with dynamic workflow schemas and durable jobs."""

from __future__ import annotations
import base64

import hashlib
import json
import logging
import math
import re
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import anyio
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS
from mcp.server import Server, ServerRequestContext
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import (
    BlobResourceContents,
    CallToolRequestParams,
    CallToolResult,
    ListResourcesResult,
    ListToolsResult,
    PaginatedRequestParams,
    ReadResourceRequestParams,
    ReadResourceResult,
    Resource,
    TextContent,
    TextResourceContents,
    Tool,
    ToolAnnotations,
)

from comfyui_mcp_skills import __version__
from comfyui_mcp_skills.application.assets import AssetService
from comfyui_mcp_skills.application.catalog import WorkflowCatalog
from comfyui_mcp_skills.application.execution import ExecutionService
from comfyui_mcp_skills.application.jobs import JobService
from comfyui_mcp_skills.application.ports import ComfyUIGateway
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import ComfyUISkillsError, ServerNotFound
from comfyui_mcp_skills.domain.models import Job, Workflow
from comfyui_mcp_skills.domain.workflow_schema import build_input_schema
from comfyui_mcp_skills.infrastructure.comfyui.gateway import create_gateway
from comfyui_mcp_skills.infrastructure.persistence.assets import FileAssetRepository
from comfyui_mcp_skills.infrastructure.persistence.runs import FileRunRepository
from comfyui_mcp_skills.infrastructure.persistence.workflows import FileWorkflowRepository


GatewayFactory = Callable[[dict[str, Any]], ComfyUIGateway]
logger = logging.getLogger(__name__)

_MAX_OUTPUT_RESOURCE_BYTES = 25 * 1024 * 1024

_JOB_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "prompt_id": {"type": "string"},
        "server_id": {"type": "string"},
        "workflow_id": {"type": "string"},
        "status": {
            "type": "string",
            "enum": [
                "reserved",
                "submission_unknown",
                "submitted",
                "queued",
                "running",
                "completed",
                "error",
                "interrupted",
                "cancelled",
            ],
        },
        "outputs": {"type": "array", "items": {"type": "object"}},
        "error": {"type": "string"},
        "idempotency_key": {"type": "string"},
        "client_id": {"type": "string"},
    },
    "required": [
        "prompt_id",
        "server_id",
        "workflow_id",
        "status",
        "outputs",
        "error",
        "idempotency_key",
        "client_id",
    ],
    "additionalProperties": False,
}
_EXECUTION_PROPERTY: dict[str, Any] = {
    "type": "object",
    "properties": {
        "idempotency_key": {"type": "string", "maxLength": 256},
        "wait": {"type": "boolean", "default": False},
        "wait_timeout_seconds": {
            "type": "number",
            "minimum": 0,
            "maximum": 300,
            "default": 120,
        },
    },
    "additionalProperties": False,
}


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-").lower()
    return slug or "workflow"


def _bounded_tool_name(base: str, identity: str, *, force_hash: bool = False) -> str:
    if not force_hash and len(base) <= 128:
        return base
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{base[:63]}-{digest}"


def _workflow_tool_names(workflows: list[Workflow]) -> dict[str, Workflow]:
    candidates: dict[str, list[Workflow]] = {}
    for workflow in workflows:
        name = f"comfyui.run.{_slug(workflow.server_id)}.{_slug(workflow.workflow_id)}"
        candidates.setdefault(name, []).append(workflow)
    result: dict[str, Workflow] = {}
    for base in sorted(candidates):
        grouped = sorted(
            candidates[base], key=lambda item: (item.server_id, item.workflow_id)
        )
        for workflow in grouped:
            identity = f"{workflow.server_id}/{workflow.workflow_id}"
            name = _bounded_tool_name(base, identity, force_hash=len(grouped) > 1)
            collision = 0
            while name in result:
                collision += 1
                name = _bounded_tool_name(
                    base, f"{identity}#{collision}", force_hash=True
                )
            result[name] = workflow
    return result


def _job_dict(job: Job) -> dict[str, Any]:
    data = asdict(job)
    data["outputs"] = list(job.outputs)
    data.pop("request_digest", None)
    data.pop("owner_id", None)
    return data


def _result(data: dict[str, Any], *, error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
        structured_content=None if error else data,
        is_error=error,
    )

def _current_owner() -> str:
    token = get_access_token()
    return token.client_id if token is not None else "stdio"


def create_server(
    base_dir: Path,
    *,
    gateway_factory: GatewayFactory = create_gateway,
    upload_roots: list[Path] | None = None,
    max_upload_bytes: int = 100 * 1024 * 1024,
) -> Server[dict[str, object]]:
    """Create an MCP server backed by one configured project directory."""
    base_dir = base_dir.resolve()
    catalog = WorkflowCatalog(FileWorkflowRepository(base_dir))
    servers = ServerRegistry(base_dir)
    run_repository = FileRunRepository(base_dir)
    asset_repository = FileAssetRepository(base_dir)
    assets = AssetService(
        asset_repository,
        upload_roots=upload_roots if upload_roots is not None else [base_dir / "uploads"],
        max_bytes=max_upload_bytes,
    )
    execution = ExecutionService(
        catalog, servers, run_repository, asset_repository, gateway_factory
    )
    jobs = JobService(servers, run_repository, gateway_factory)

    def enabled_workflows() -> list[Workflow]:
        result: list[Workflow] = []
        for workflow in catalog.list_enabled():
            try:
                servers.connection(workflow.server_id)
            except ServerNotFound:
                continue
            result.append(workflow)
        return result

    def current_tools() -> tuple[list[Tool], dict[str, Workflow]]:
        workflow_map = _workflow_tool_names(enabled_workflows())
        tools: list[Tool] = []
        for name in sorted(workflow_map):
            workflow = workflow_map[name]
            schema = build_input_schema(workflow.parameters)
            schema["properties"]["_execution"] = _EXECUTION_PROPERTY
            tools.append(
                Tool(
                    name=name,
                    title=f"Run {workflow.server_id}/{workflow.workflow_id}",
                    description=(
                        workflow.description
                        or f"Run ComfyUI workflow {workflow.server_id}/{workflow.workflow_id}"
                    ),
                    input_schema=schema,
                    output_schema=_JOB_SCHEMA,
                    annotations=ToolAnnotations(
                        read_only_hint=False,
                        destructive_hint=False,
                        idempotent_hint=False,
                        open_world_hint=False,
                    ),
                )
            )
        tools.extend(_fixed_tools())
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
        owner_id = _current_owner()
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
                        "Unexpected _execution fields: "
                        + ", ".join(sorted(unexpected_options))
                    )
                if not isinstance(idempotency_key, str):
                    raise TypeError("idempotency_key must be a string")
                wait = execution_options.get("wait", False)
                if len(idempotency_key) > 256:
                    raise ValueError("idempotency_key exceeds 256 characters")
                if not isinstance(wait, bool):
                    raise TypeError("wait must be a boolean")
                timeout_raw = execution_options.get("wait_timeout_seconds", 120)
                if isinstance(timeout_raw, bool) or not isinstance(
                    timeout_raw, (int, float)
                ):
                    raise TypeError("wait_timeout_seconds must be a number")
                timeout_seconds = float(timeout_raw)
                if not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= 300:
                    raise ValueError(
                        "wait_timeout_seconds must be between 0 and 300"
                    )
                await ctx.session.report_progress(0, 1, "Submitting ComfyUI workflow")
                job = await anyio.to_thread.run_sync(
                    lambda: execution.submit(
                        workflow.server_id,
                        workflow.workflow_id,
                        arguments,
                        idempotency_key=idempotency_key,
                        owner_id=owner_id,
                    )
                )
                await ctx.session.report_progress(1, 1, "Workflow submitted")
                if wait:

                    def report(event: dict[str, Any]) -> None:
                        data = event.get("data", {})
                        value = float(data.get("value", 0))
                        total_raw = data.get("max")
                        total = float(total_raw) if total_raw is not None else None
                        message = str(event.get("type", "progress"))
                        if data.get("node") is not None:
                            message = f"{message}: node {data['node']}"
                        anyio.from_thread.run(
                            ctx.session.report_progress, value, total, message
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
                return _result(_job_dict(job))
            if params.name == "comfyui.job.get":
                _validate_fixed_arguments(arguments, {"server_id", "prompt_id"})
                server_id = _required_string(arguments, "server_id")
                prompt_id = _required_string(arguments, "prompt_id")
                job = await anyio.to_thread.run_sync(
                    lambda: jobs.get(server_id, prompt_id, owner_id=owner_id)
                )
                return _result(_job_dict(job))
            if params.name == "comfyui.job.cancel":
                _validate_fixed_arguments(arguments, {"server_id", "prompt_id"})
                server_id = _required_string(arguments, "server_id")
                prompt_id = _required_string(arguments, "prompt_id")
                job = await anyio.to_thread.run_sync(
                    lambda: jobs.cancel(server_id, prompt_id, owner_id=owner_id)
                )
                return _result(_job_dict(job))
            if params.name == "comfyui.asset.upload":
                _validate_fixed_arguments(
                    arguments,
                    {"server_id", "local_path", "purpose", "original_asset_id"},
                )
                server_id = _required_string(arguments, "server_id")
                local_path = _required_string(arguments, "local_path")
                purpose = _optional_string(arguments, "purpose", "image")
                original_asset_id = _optional_string(
                    arguments, "original_asset_id", ""
                )
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
                return _result(asset.to_public_dict())
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
            return _result(error, error=True)
        except Exception:
            logger.exception("Unexpected MCP tool failure", extra={"tool": params.name})
            return _result(
                {
                    "code": "INTERNAL_ERROR",
                    "message": "Unexpected server error",
                    "retryable": False,
                    "details": {},
                },
                error=True,
            )

    async def list_resources(
        _ctx: ServerRequestContext[dict[str, object]],
        _params: PaginatedRequestParams | None,
    ) -> ListResourcesResult:
        resources = [
            Resource(
                uri=f"comfyui://workflows/{workflow.server_id}/{workflow.workflow_id}",
                name=f"{workflow.server_id}/{workflow.workflow_id}",
                title=f"Workflow {workflow.workflow_id}",
                description=workflow.description,
                mime_type="application/json",
            )
            for workflow in enabled_workflows()
        ]
        return ListResourcesResult(
            resources=resources, ttl_ms=5_000, cache_scope="private"
        )

    async def read_resource(
        _ctx: ServerRequestContext[dict[str, object]],
        params: ReadResourceRequestParams,
    ) -> ReadResourceResult:
        uri = str(params.uri)
        owner_id = _current_owner()
        if uri.startswith("comfyui://workflows/"):
            identity = uri.removeprefix("comfyui://workflows/").split("/", 1)
            if len(identity) != 2:
                raise ValueError(f"Invalid workflow URI: {uri}")
            servers.connection(identity[0])
            workflow = catalog.get(identity[0], identity[1])
            document = {
                "server_id": workflow.server_id,
                "workflow_id": workflow.workflow_id,
                "description": workflow.description,
                "enabled": workflow.enabled,
                "parameters": workflow.parameters,
                "input_schema": build_input_schema(workflow.parameters),
            }
        elif uri.startswith("comfyui://assets/"):
            identity = uri.removeprefix("comfyui://assets/").split("/", 1)
            if len(identity) != 2:
                raise ValueError(f"Invalid asset URI: {uri}")
            servers.connection(identity[0])
            asset = assets.get(identity[1], owner_id=owner_id)
            if asset.server_id != identity[0]:
                raise ValueError(f"Asset does not belong to server: {identity[0]}")
            document = asset.to_public_dict()
        elif uri.startswith("comfyui://jobs/"):
            identity = uri.removeprefix("comfyui://jobs/").split("/", 1)
            if len(identity) != 2:
                raise ValueError(f"Invalid job URI: {uri}")
            job = await anyio.to_thread.run_sync(
                lambda: jobs.get(identity[0], identity[1], owner_id=owner_id)
            )
            document = _job_dict(job)
        elif uri.startswith("comfyui://outputs/"):
            identity = uri.removeprefix("comfyui://outputs/").split("/", 2)
            if len(identity) != 3 or not identity[2].isdigit():
                raise ValueError(f"Invalid output URI: {uri}")
            job = await anyio.to_thread.run_sync(
                lambda: jobs.get(identity[0], identity[1], owner_id=owner_id)
            )
            index = int(identity[2])
            if index >= len(job.outputs):
                raise ValueError(f"Output not found: {uri}")
            output = dict(job.outputs[index])
            gateway = gateway_factory(servers.connection(identity[0]))
            try:
                payload = await anyio.to_thread.run_sync(
                    lambda: gateway.download_output(
                        str(output.get("filename", "")),
                        str(output.get("subfolder", "")),
                        str(output.get("type", "output")),
                        max_bytes=_MAX_OUTPUT_RESOURCE_BYTES,
                    )
                )
            except ValueError as exc:
                raise MCPError(code=INVALID_PARAMS, message=str(exc)) from exc
            return ReadResourceResult(
                contents=[
                    BlobResourceContents(
                        uri=uri,
                        mime_type=str(
                            output.get("mime_type", "application/octet-stream")
                        ),
                        blob=base64.b64encode(payload).decode("ascii"),
                    )
                ],
                ttl_ms=5_000,
                cache_scope="private",
            )
        else:
            raise ValueError(f"Unsupported resource URI: {uri}")
        return ReadResourceResult(
            contents=[
                TextResourceContents(
                    uri=uri,
                    mime_type="application/json",
                    text=json.dumps(document, ensure_ascii=False),
                )
            ],
            ttl_ms=5_000,
            cache_scope="private",
        )

    return Server(
        "ComfyUI MCP Skills",
        version=__version__,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_resources=list_resources,
        on_read_resource=read_resource,
    )


def _required_string(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value:
        raise TypeError(f"{name} must be a non-empty string")
    return value


def _optional_string(arguments: dict[str, Any], name: str, default: str) -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _validate_fixed_arguments(arguments: dict[str, Any], allowed: set[str]) -> None:
    unexpected = set(arguments) - allowed
    if unexpected:
        raise ValueError(f"Unexpected arguments: {', '.join(sorted(unexpected))}")


def _fixed_tools() -> list[Tool]:
    job_properties = {
        "server_id": {"type": "string", "minLength": 1},
        "prompt_id": {"type": "string", "minLength": 1},
    }
    return [
        Tool(
            name="comfyui.asset.upload",
            description=(
                "Upload an authorized local image, mask, audio, or video file "
                "to ComfyUI and return an asset handle."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string", "minLength": 1},
                    "local_path": {"type": "string", "minLength": 1},
                    "purpose": {
                        "type": "string",
                        "enum": ["image", "mask", "audio", "video"],
                        "default": "image",
                    },
                    "original_asset_id": {"type": "string", "default": ""},
                },
                "required": ["server_id", "local_path"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=False,
            ),
        ),
        Tool(
            name="comfyui.job.get",
            description="Get durable ComfyUI job status and output resource links.",
            input_schema={
                "type": "object",
                "properties": job_properties,
                "required": ["server_id", "prompt_id"],
                "additionalProperties": False,
            },
            output_schema=_JOB_SCHEMA,
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        ),
        Tool(
            name="comfyui.job.cancel",
            description=(
                "Remove this principal's queued ComfyUI job; running jobs are "
                "safely rejected because ComfyUI interrupt is global."
            ),
            input_schema={
                "type": "object",
                "properties": job_properties,
                "required": ["server_id", "prompt_id"],
                "additionalProperties": False,
            },
            output_schema=_JOB_SCHEMA,
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        ),
    ]
