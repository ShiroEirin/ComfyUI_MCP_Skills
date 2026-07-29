"""MCP resource discovery and reading handlers."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anyio
from mcp.server import ServerRequestContext
from mcp.shared.exceptions import MCPError
from mcp.types import (
    BlobResourceContents,
    ListResourcesResult,
    ListResourceTemplatesResult,
    PaginatedRequestParams,
    ReadResourceRequestParams,
    ReadResourceResult,
    Resource,
    ResourceTemplate,
    TextResourceContents,
)
from mcp_types import INVALID_PARAMS

from comfyui_mcp_skills.adapters.mcp.tooling import current_owner, job_dict
from comfyui_mcp_skills.application.assets import AssetService
from comfyui_mcp_skills.application.catalog import WorkflowCatalog
from comfyui_mcp_skills.application.jobs import JobService
from comfyui_mcp_skills.application.ports import ComfyUIGateway
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import (
    AssetNotFound,
    JobNotFound,
    ServerNotFound,
    WorkflowNotFound,
)
from comfyui_mcp_skills.domain.models import Workflow
from comfyui_mcp_skills.domain.workflow_schema import build_input_schema

GatewayFactory = Callable[[dict[str, Any]], ComfyUIGateway]
EnabledWorkflows = Callable[[], list[Workflow]]
_MAX_OUTPUT_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ResourceHandlers:
    list_templates: Callable[..., Any]
    list_resources: Callable[..., Any]
    read_resource: Callable[..., Any]


def create_resource_handlers(
    catalog: WorkflowCatalog,
    servers: ServerRegistry,
    assets: AssetService,
    jobs: JobService,
    gateway_factory: GatewayFactory,
    enabled_workflows: EnabledWorkflows,
) -> ResourceHandlers:
    async def list_templates(
        _ctx: ServerRequestContext[dict[str, object]],
        _params: PaginatedRequestParams | None,
    ) -> ListResourceTemplatesResult:
        return ListResourceTemplatesResult(
            resource_templates=[
                ResourceTemplate(
                    uri_template="comfyui://workflows/{server_id}/{workflow_id}",
                    name="Configured workflow",
                    mime_type="application/json",
                ),
                ResourceTemplate(
                    uri_template="comfyui://assets/{server_id}/{asset_id}",
                    name="Authorized input asset",
                    mime_type="application/json",
                ),
                ResourceTemplate(
                    uri_template="comfyui://jobs/{server_id}/{prompt_id}",
                    name="Durable execution job",
                    mime_type="application/json",
                ),
                ResourceTemplate(
                    uri_template="comfyui://outputs/{server_id}/{prompt_id}/{index}",
                    name="Generated output media",
                ),
            ],
            ttl_ms=60_000,
            cache_scope="public",
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
        return ListResourcesResult(resources=resources, ttl_ms=5_000, cache_scope="private")

    async def read_resource(
        ctx: ServerRequestContext[dict[str, object]],
        params: ReadResourceRequestParams,
    ) -> ReadResourceResult:
        try:
            return await _read_resource(
                ctx,
                params,
                catalog=catalog,
                servers=servers,
                assets=assets,
                jobs=jobs,
                gateway_factory=gateway_factory,
            )
        except MCPError:
            raise
        except (
            AssetNotFound,
            JobNotFound,
            ServerNotFound,
            WorkflowNotFound,
            ValueError,
        ) as exc:
            raise MCPError(
                code=INVALID_PARAMS,
                message="Resource not found",
                data={"uri": str(params.uri)},
            ) from exc

    return ResourceHandlers(list_templates, list_resources, read_resource)


async def _read_resource(
    _ctx: ServerRequestContext[dict[str, object]],
    params: ReadResourceRequestParams,
    *,
    catalog: WorkflowCatalog,
    servers: ServerRegistry,
    assets: AssetService,
    jobs: JobService,
    gateway_factory: GatewayFactory,
) -> ReadResourceResult:
    uri = str(params.uri)
    owner_id = current_owner()
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
        document = job_dict(job)
    elif uri.startswith("comfyui://outputs/"):
        return await _read_output(
            uri,
            servers=servers,
            jobs=jobs,
            gateway_factory=gateway_factory,
            owner_id=owner_id,
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


async def _read_output(
    uri: str,
    *,
    servers: ServerRegistry,
    jobs: JobService,
    gateway_factory: GatewayFactory,
    owner_id: str,
) -> ReadResourceResult:
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
                max_bytes=_MAX_OUTPUT_BYTES,
            )
        )
    except ValueError as exc:
        raise MCPError(code=INVALID_PARAMS, message=str(exc)) from exc
    return ReadResourceResult(
        contents=[
            BlobResourceContents(
                uri=uri,
                mime_type=str(output.get("mime_type", "application/octet-stream")),
                blob=base64.b64encode(payload).decode("ascii"),
            )
        ],
        ttl_ms=5_000,
        cache_scope="private",
    )
