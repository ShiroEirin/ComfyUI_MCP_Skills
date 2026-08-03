"""MCP resource discovery and reading handlers."""

from __future__ import annotations

import base64
import json
import mimetypes
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

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

from comfyui_mcp_skills.adapters.mcp.tooling import current_owner, current_scopes, job_dict
from comfyui_mcp_skills.application.asset_library import AssetLibraryService
from comfyui_mcp_skills.application.assets import AssetService
from comfyui_mcp_skills.application.authorization import is_authorized, scopes_for_resource
from comfyui_mcp_skills.application.catalog import WorkflowCatalog
from comfyui_mcp_skills.application.jobs import JobService
from comfyui_mcp_skills.application.ports import ComfyUIGateway
from comfyui_mcp_skills.application.resource_aliases import ResourceAliasReader, ResourceTarget
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.application.workflow_inspection import WorkflowInspectionService
from comfyui_mcp_skills.domain.control_plane import (
    parse_legacy_resource_uri,
    validate_control_plane_id,
)
from comfyui_mcp_skills.domain.errors import (
    AssetNotFound,
    ComfyUISkillsError,
    JobNotFound,
    ServerNotFound,
    WorkflowNotFound,
)
from comfyui_mcp_skills.domain.identifiers import validate_identifier
from comfyui_mcp_skills.domain.media import validate_media_locator
from comfyui_mcp_skills.domain.models import Workflow
from comfyui_mcp_skills.domain.workflow_schema import build_input_schema
from comfyui_mcp_skills.infrastructure.persistence.sqlite_asset_library import (
    SQLiteAssetLibraryRepository,
)

GatewayFactory = Callable[[dict[str, Any]], ComfyUIGateway]
EnabledWorkflows = Callable[[], list[Workflow]]
_MAX_OUTPUT_BYTES = 25 * 1024 * 1024
_MAX_JSON_RESOURCE_BYTES = 1024 * 1024


def _resource_scope_allowed(kind: str, *, required: bool) -> bool:
    granted = current_scopes()
    if granted is None:
        return not required
    return is_authorized(granted, scopes_for_resource(kind))


def _require_resource_scope(kind: str, *, required: bool) -> None:
    if not _resource_scope_allowed(kind, required=required):
        raise MCPError(code=INVALID_PARAMS, message="Resource not found")


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
    *,
    resource_aliases: ResourceAliasReader | None = None,
    require_authorization: bool = False,
    workflow_inspection: WorkflowInspectionService | None = None,
    asset_library: AssetLibraryService | None = None,
    asset_library_repository: SQLiteAssetLibraryRepository | None = None,
) -> ResourceHandlers:
    async def list_templates(
        _ctx: ServerRequestContext[dict[str, object]],
        _params: PaginatedRequestParams | None,
    ) -> ListResourceTemplatesResult:
        templates = [
            (
                "workflow",
                ResourceTemplate(
                    uri_template="comfyui://workflows/{server_id}/{workflow_id}",
                    name="Configured workflow",
                    mime_type="application/json",
                ),
            ),
            (
                "workflow",
                ResourceTemplate(
                    uri_template="comfyui://workflows/{workflow_id}",
                    name="Canonical workflow metadata",
                    mime_type="application/json",
                ),
            ),
            *(
                [
                    (
                        "workflow",
                        ResourceTemplate(
                            uri_template="comfyui://workflows/{workflow_id}/graph",
                            name="Semantic workflow graph",
                            mime_type="application/json",
                        ),
                    ),
                    *[
                        (
                            "workflow",
                            ResourceTemplate(
                                uri_template=f"comfyui://workflows/{{workflow_id}}/{view}",
                                name=f"Semantic workflow {view}",
                                mime_type="application/json",
                            ),
                        )
                        for view in ("nodes", "edges", "parameters", "outputs")
                    ],
                ]
                if workflow_inspection is not None
                else []
            ),
            (
                "revision",
                ResourceTemplate(
                    uri_template="comfyui://revisions/{revision_id}",
                    name="Canonical workflow revision metadata",
                    mime_type="application/json",
                ),
            ),
            (
                "deployment",
                ResourceTemplate(
                    uri_template="comfyui://deployments/{deployment_id}",
                    name="Canonical workflow deployment metadata",
                    mime_type="application/json",
                ),
            ),
            (
                "asset",
                ResourceTemplate(
                    uri_template="comfyui://assets/{server_id}/{asset_id}",
                    name="Authorized input asset",
                    mime_type="application/json",
                ),
            ),
            (
                "job",
                ResourceTemplate(
                    uri_template="comfyui://jobs/{server_id}/{prompt_id}",
                    name="Durable execution job",
                    mime_type="application/json",
                ),
            ),
            (
                "output",
                ResourceTemplate(
                    uri_template="comfyui://outputs/{server_id}/{prompt_id}/{index}",
                    name="Generated output media",
                ),
            ),
            (
                "asset",
                ResourceTemplate(
                    uri_template="comfyui://assets/{asset_id}",
                    name="Canonical input asset",
                    mime_type="application/json",
                ),
            ),
            (
                "job",
                ResourceTemplate(
                    uri_template="comfyui://jobs/{job_id}",
                    name="Canonical execution job",
                    mime_type="application/json",
                ),
            ),
            (
                "artifact",
                ResourceTemplate(
                    uri_template="comfyui://artifacts/{artifact_id}",
                    name="Canonical generated artifact",
                ),
            ),
            *(
                [
                    (
                        "lineage",
                        ResourceTemplate(
                            uri_template="comfyui://lineage/{artifact_id}",
                            name="Canonical Artifact lineage",
                            mime_type="application/json",
                        ),
                    )
                ]
                if asset_library_repository is not None
                else []
            ),
        ]
        return ListResourceTemplatesResult(
            resource_templates=[
                template
                for kind, template in templates
                if _resource_scope_allowed(kind, required=require_authorization)
            ],
            ttl_ms=60_000,
            cache_scope="private",
        )

    async def list_resources(
        _ctx: ServerRequestContext[dict[str, object]],
        _params: PaginatedRequestParams | None,
    ) -> ListResourcesResult:
        _require_resource_scope("workflow", required=require_authorization)
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
        if asset_library is not None and _resource_scope_allowed(
            "asset", required=require_authorization
        ):
            owner_id = current_owner()
            asset_page = await anyio.to_thread.run_sync(
                lambda: asset_library.list_assets(owner_id=owner_id, limit=100)
            )
            for item in asset_page.get("items", []):
                if not isinstance(item, dict):
                    continue
                uri = item.get("resource_uri")
                asset_id = item.get("asset_id")
                if not isinstance(uri, str) or not isinstance(asset_id, str):
                    continue
                resources.append(
                    Resource(
                        uri=uri,
                        name=asset_id,
                        title=f"Asset {asset_id}",
                        description="Owner-visible input asset",
                        mime_type=str(item.get("mime_type", "application/octet-stream")),
                    )
                )
            if asset_library_repository is not None:
                artifacts = await anyio.to_thread.run_sync(
                    lambda: asset_library_repository.list_artifacts(owner_id, limit=100)
                )
                for artifact in artifacts:
                    resources.append(
                        Resource(
                            uri=artifact.resource_uri,
                            name=artifact.artifact_id,
                            title=f"Artifact {artifact.artifact_id}",
                            description="Owner-visible generated Artifact",
                            mime_type=artifact.mime_type or "application/octet-stream",
                        )
                    )
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
                resource_aliases=resource_aliases,
                require_authorization=require_authorization,
                workflow_inspection=workflow_inspection,
                asset_library=asset_library,
                asset_library_repository=asset_library_repository,
            )
        except MCPError:
            raise
        except (
            AssetNotFound,
            JobNotFound,
            ServerNotFound,
            WorkflowNotFound,
            ValueError,
            ComfyUISkillsError,
            LookupError,
        ) as exc:
            raise MCPError(
                code=INVALID_PARAMS,
                message="Resource not found",
            ) from exc

    return ResourceHandlers(list_templates, list_resources, read_resource)


async def _read_resource(
    _ctx: ServerRequestContext[dict[str, object]],
    params: ReadResourceRequestParams,
    *,
    asset_library: AssetLibraryService | None = None,
    asset_library_repository: SQLiteAssetLibraryRepository | None = None,
    catalog: WorkflowCatalog,
    servers: ServerRegistry,
    assets: AssetService,
    jobs: JobService,
    gateway_factory: GatewayFactory,
    resource_aliases: ResourceAliasReader | None = None,
    require_authorization: bool = False,
    workflow_inspection: WorkflowInspectionService | None = None,
) -> ReadResourceResult:
    uri = str(params.uri)
    parsed = urlsplit(uri)
    if parsed.scheme == "comfyui" and parsed.netloc == "lineage":
        _require_resource_scope("lineage", required=require_authorization)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 1 or parsed.query or parsed.fragment:
            raise ValueError("Invalid lineage URI")
        artifact_id = validate_identifier(parts[0], field="artifact_id")
        if asset_library_repository is None:
            raise ValueError("Artifact lineage is unavailable")
        lineage = await anyio.to_thread.run_sync(
            lambda: asset_library_repository.artifact_lineage(artifact_id, current_owner())
        )
        if lineage is None:
            raise ValueError("Resource not found")
        return _json_resource(uri, lineage)
    semantic_ref = _semantic_workflow_ref(uri)
    if semantic_ref is not None:
        _require_resource_scope("workflow", required=require_authorization)
        if workflow_inspection is None:
            raise ValueError("Semantic workflow Resources are unavailable")
        workflow_id, view = semantic_ref
        document = await anyio.to_thread.run_sync(
            lambda: workflow_inspection.graph_resource(workflow_id)
        )
        if view != "graph":
            semantic = document["semantic_graph"]
            document = {
                "workflow_id": document["workflow_id"],
                "revision_id": document["revision_id"],
                "content_digest": document["content_digest"],
                view: semantic[view],
            }
        return _json_resource(uri, document)
    legacy_kind = parse_legacy_resource_uri(uri)
    if legacy_kind is not None:
        _require_resource_scope(legacy_kind.kind, required=require_authorization)
    owner_id = current_owner()
    legacy = parse_legacy_resource_uri(uri)
    response_uri = uri
    resolved_target = None
    if resource_aliases is not None and legacy is None:
        resolved_target = await anyio.to_thread.run_sync(
            lambda: resource_aliases.resolve(uri, owner_id=owner_id)
        )
    elif resource_aliases is not None and legacy is not None and legacy.kind != "workflow":
        resolved_target = await anyio.to_thread.run_sync(
            lambda: resource_aliases.resolve(uri, owner_id=owner_id)
        )
    if legacy is not None and legacy.kind == "workflow":
        servers.connection(legacy.server_id)
        workflow = catalog.get(legacy.server_id, legacy.upstream_id)
        document = {
            "server_id": workflow.server_id,
            "workflow_id": workflow.workflow_id,
            "description": workflow.description,
            "enabled": workflow.enabled,
            "parameters": workflow.parameters,
            "input_schema": build_input_schema(workflow.parameters),
        }
    elif resolved_target is not None:
        _require_resource_scope(resolved_target.kind, required=require_authorization)
        return await _read_resolved_resource(
            resolved_target,
            servers=servers,
            assets=assets,
            jobs=jobs,
            gateway_factory=gateway_factory,
            owner_id=owner_id,
        )
    elif resource_aliases is None and parsed.scheme == "comfyui" and parsed.netloc == "assets":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 1 or parsed.query or parsed.fragment:
            raise ValueError("Invalid Asset URI")
        _require_resource_scope("asset", required=require_authorization)
        asset_id = validate_control_plane_id("asset", parts[0])
        document = assets.get(asset_id, owner_id=owner_id).to_public_dict()
    elif legacy is not None and legacy.kind == "asset":
        servers.connection(legacy.server_id)
        asset = assets.get(legacy.upstream_id, owner_id=owner_id)
        if asset.server_id != legacy.server_id:
            raise ValueError(f"Asset does not belong to server: {legacy.server_id}")
        document = asset.to_public_dict()
    elif legacy is not None and legacy.kind == "job":
        job = await anyio.to_thread.run_sync(
            lambda: jobs.get(legacy.server_id, legacy.upstream_id, owner_id=owner_id)
        )
        document = job_dict(job)
    elif legacy is not None and legacy.kind == "output":
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
                uri=response_uri,
                mime_type="application/json",
                text=json.dumps(document, ensure_ascii=False),
            )
        ],
        ttl_ms=5_000,
        cache_scope="private",
    )


def _semantic_workflow_ref(uri: str) -> tuple[str, str] | None:
    parsed = urlsplit(uri)
    if parsed.scheme != "comfyui" or parsed.netloc != "workflows":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if (
        len(parts) != 2
        or parts[1] not in {"graph", "nodes", "edges", "parameters", "outputs"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    return validate_identifier(parts[0], field="workflow_id"), parts[1]


def _json_resource(uri: str, document: dict[str, Any]) -> ReadResourceResult:
    text = json.dumps(document, ensure_ascii=False)
    if len(text.encode("utf-8")) > _MAX_JSON_RESOURCE_BYTES:
        raise ValueError("JSON Resource exceeds 1 MiB")
    return ReadResourceResult(
        contents=[
            TextResourceContents(
                uri=uri,
                mime_type="application/json",
                text=text,
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
    identity = parse_legacy_resource_uri(uri)
    if identity is None or identity.kind != "output" or identity.index is None:
        raise ValueError(f"Invalid output URI: {uri}")
    job = await anyio.to_thread.run_sync(
        lambda: jobs.get(identity.server_id, identity.upstream_id, owner_id=owner_id)
    )
    if identity.index >= len(job.outputs):
        raise ValueError(f"Output not found: {uri}")
    output = dict(job.outputs[identity.index])
    return await _download_output(
        uri=uri,
        mime_type=str(output.get("mime_type", "application/octet-stream")),
        server_id=identity.server_id,
        filename=str(output.get("filename", "")),
        subfolder=str(output.get("subfolder", "")),
        storage_type=str(output.get("type", "output")),
        servers=servers,
        gateway_factory=gateway_factory,
    )


async def _read_resolved_resource(
    target: ResourceTarget,
    *,
    servers: ServerRegistry,
    assets: AssetService,
    jobs: JobService,
    gateway_factory: GatewayFactory,
    owner_id: str,
) -> ReadResourceResult:
    if target.kind in {"workflow", "revision", "deployment"}:
        document = dict(target.metadata)
        return ReadResourceResult(
            contents=[
                TextResourceContents(
                    uri=target.canonical_uri,
                    mime_type="application/json",
                    text=json.dumps(document, ensure_ascii=False),
                )
            ],
            ttl_ms=5_000,
            cache_scope="private",
        )
    if target.kind == "asset":
        servers.connection(target.server_id)
        asset = assets.get(target.object_id, owner_id=owner_id)
        if asset.server_id != target.server_id:
            raise ValueError(f"Asset does not belong to server: {target.server_id}")
        document = asset.to_public_dict()
        document["canonical_uri"] = target.canonical_uri
    elif target.kind == "job":
        job = await anyio.to_thread.run_sync(
            lambda: jobs.get(target.server_id, target.prompt_id, owner_id=owner_id)
        )
        document = job_dict(job)
        document["canonical_uri"] = target.canonical_uri
    elif target.kind == "artifact":
        return await _download_output(
            uri=target.canonical_uri,
            mime_type=mimetypes.guess_type(target.filename)[0] or "application/octet-stream",
            server_id=target.server_id,
            filename=target.filename,
            subfolder=target.subfolder,
            storage_type=target.storage_type,
            servers=servers,
            gateway_factory=gateway_factory,
        )
    else:
        raise ValueError(f"Unsupported canonical resource kind: {target.kind}")
    return ReadResourceResult(
        contents=[
            TextResourceContents(
                uri=target.canonical_uri,
                mime_type="application/json",
                text=json.dumps(document, ensure_ascii=False),
            )
        ],
        ttl_ms=5_000,
        cache_scope="private",
    )


async def _download_output(
    *,
    uri: str,
    mime_type: str,
    server_id: str,
    filename: str,
    subfolder: str,
    storage_type: str,
    servers: ServerRegistry,
    gateway_factory: GatewayFactory,
) -> ReadResourceResult:
    if storage_type != "output":
        raise MCPError(code=INVALID_PARAMS, message="Unsupported output storage type")
    try:
        filename, subfolder = validate_media_locator(filename, subfolder)
    except ValueError as exc:
        raise MCPError(code=INVALID_PARAMS, message="Unsafe output media locator") from exc
    gateway = gateway_factory(servers.connection(server_id))
    try:
        payload = await anyio.to_thread.run_sync(
            lambda: gateway.download_output(
                filename,
                subfolder,
                storage_type,
                max_bytes=_MAX_OUTPUT_BYTES,
            )
        )
    except ValueError as exc:
        raise MCPError(code=INVALID_PARAMS, message="Output resource unavailable") from exc
    return ReadResourceResult(
        contents=[
            BlobResourceContents(
                uri=uri,
                mime_type=mime_type,
                blob=base64.b64encode(payload).decode("ascii"),
            )
        ],
        ttl_ms=5_000,
        cache_scope="private",
    )
