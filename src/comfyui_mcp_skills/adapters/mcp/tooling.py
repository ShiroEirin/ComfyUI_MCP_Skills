"""MCP tool schemas, naming, argument, and result serialization helpers."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import (
    CallToolResult,
    ContentBlock,
    Icon,
    ResourceLink,
    TextContent,
    Tool,
    ToolAnnotations,
)

from comfyui_mcp_skills.application.auth_context import current_authorization
from comfyui_mcp_skills.application.authorization import Scope, parse_scopes
from comfyui_mcp_skills.application.capabilities import CAPABILITY_BY_NAME, PROJECT_ICON_SRC
from comfyui_mcp_skills.domain.media import validate_media_locator
from comfyui_mcp_skills.domain.models import Job, Workflow

JOB_SCHEMA: dict[str, Any] = {
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
                "lost",
            ],
        },
        "outputs": {"type": "array", "items": {"type": "object"}},
        "error": {"type": "string"},
        "job_id": {"type": "string"},
        "plan_id": {"type": "string"},
        "revision_id": {"type": "string"},
        "deployment_id": {"type": "string"},
        "plan_digest": {"type": "string"},
        "job_uri": {"type": "string"},
    },
    "required": [
        "prompt_id",
        "server_id",
        "workflow_id",
        "status",
        "outputs",
        "error",
        "job_id",
        "plan_id",
        "revision_id",
        "deployment_id",
        "plan_digest",
        "job_uri",
    ],
    "additionalProperties": False,
}

EXECUTION_PROPERTY: dict[str, Any] = {
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


def workflow_tool_names(workflows: list[Workflow]) -> dict[str, Workflow]:
    candidates: dict[str, list[Workflow]] = {}
    for workflow in workflows:
        name = f"comfyui.run.{_slug(workflow.server_id)}.{_slug(workflow.workflow_id)}"
        candidates.setdefault(name, []).append(workflow)
    result: dict[str, Workflow] = {}
    for base in sorted(candidates):
        grouped = sorted(candidates[base], key=lambda item: (item.server_id, item.workflow_id))
        for workflow in grouped:
            identity = f"{workflow.server_id}/{workflow.workflow_id}"
            name = _bounded_tool_name(base, identity, force_hash=len(grouped) > 1)
            collision = 0
            while name in result:
                collision += 1
                name = _bounded_tool_name(base, f"{identity}#{collision}", force_hash=True)
            result[name] = workflow
    return result


def job_dict(job: Job) -> dict[str, Any]:
    outputs: list[dict[str, Any]] = []
    for index, raw in enumerate(job.outputs):
        if raw.get("type", "output") != "output":
            continue
        try:
            filename, subfolder = validate_media_locator(
                raw.get("filename"), raw.get("subfolder", "")
            )
        except ValueError:
            continue
        media_type = raw.get("media_type")
        if media_type not in {"image", "audio", "video"}:
            media_type = "image"
        outputs.append(
            {
                "filename": filename,
                "subfolder": subfolder,
                "type": "output",
                "media_type": media_type,
                "mime_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
                "resource_uri": f"comfyui://outputs/{job.server_id}/{job.prompt_id}/{index}",
            }
        )
    return {
        "prompt_id": job.prompt_id,
        "server_id": job.server_id,
        "workflow_id": job.workflow_id,
        "status": job.status,
        "outputs": outputs,
        "error": "Workflow execution failed" if job.error else "",
        "job_id": job.job_id,
        "plan_id": job.plan_id,
        "revision_id": job.revision_id,
        "deployment_id": job.deployment_id,
        "plan_digest": job.plan_digest,
        "job_uri": f"comfyui://jobs/{job.job_id}" if job.job_id else "",
    }


def tool_result(data: dict[str, Any], *, error: bool = False) -> CallToolResult:
    content: list[ContentBlock] = [
        TextContent(type="text", text=json.dumps(data, ensure_ascii=False))
    ]
    if not error:
        outputs = data.get("outputs", [])
        if isinstance(outputs, list):
            for output in outputs:
                if not isinstance(output, dict):
                    continue
                uri = output.get("resource_uri")
                if not isinstance(uri, str) or not uri:
                    continue
                filename = output.get("filename")
                mime_type = output.get("mime_type")
                content.append(
                    ResourceLink(
                        type="resource_link",
                        uri=uri,
                        name=filename if isinstance(filename, str) and filename else uri,
                        mime_type=mime_type if isinstance(mime_type, str) else None,
                    )
                )
    return CallToolResult(
        content=content,
        structured_content=None if error else data,
        is_error=error,
    )


def current_owner() -> str:
    token = get_access_token()
    if token is not None:
        return token.client_id
    authorization = current_authorization()
    return authorization.principal_id if authorization is not None else "local-stdio"


def current_scopes() -> frozenset[Scope] | None:
    token = get_access_token()
    if token is not None:
        return parse_scopes(",".join(token.scopes))
    authorization = current_authorization()
    return authorization.scopes if authorization is not None else None


def required_string(arguments: dict[str, Any], name: str, *, max_length: int | None = None) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value:
        raise TypeError(f"{name} must be a non-empty string")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{name} exceeds {max_length} characters")
    return value


def optional_string(
    arguments: dict[str, Any],
    name: str,
    default: str,
    *,
    max_length: int | None = None,
) -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{name} exceeds {max_length} characters")
    return value


def bounded_integer(
    arguments: dict[str, Any], name: str, default: int, *, minimum: int, maximum: int
) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def optional_boolean(arguments: dict[str, Any], name: str, default: bool) -> bool:
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


def validate_fixed_arguments(arguments: dict[str, Any], allowed: set[str]) -> None:
    unexpected = set(arguments) - allowed
    if unexpected:
        raise ValueError(f"Unexpected arguments: {', '.join(sorted(unexpected))}")


def decorate_tool(tool: Tool, *, risk: str | None = None, toolset: str | None = None) -> Tool:
    spec = CAPABILITY_BY_NAME.get(tool.name)
    title = spec.title if spec is not None else tool.title
    risk_value = spec.risk.value if spec is not None else risk
    toolsets = (
        sorted(item.value for item in spec.toolsets)
        if spec is not None
        else ([toolset] if toolset else [])
    )
    return tool.model_copy(
        update={
            "title": title,
            "icons": [Icon(src=PROJECT_ICON_SRC, mime_type="image/svg+xml")],
            "meta": {"comfyui/risk": risk_value, "comfyui/toolsets": toolsets},
        }
    )


def fixed_tools() -> list[Tool]:
    job_properties = {
        "server_id": {"type": "string", "minLength": 1},
        "prompt_id": {"type": "string", "minLength": 1},
    }
    discovery_properties = {
        "server_id": {"type": "string", "minLength": 1},
        "query": {"type": "string", "default": ""},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        "cursor": {"type": "string", "default": ""},
    }
    tools = [
        Tool(
            name="comfyui.capability.search",
            description="Search authorized backend capabilities without changing tools/list.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "default": ""},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                },
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        ),
        Tool(
            name="comfyui.capability.describe",
            description=(
                "Describe one authorized capability, schema, risk, and safe Host fallbacks."
            ),
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string", "minLength": 1}},
                "required": ["name"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        ),
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
                open_world_hint=True,
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
            output_schema=JOB_SCHEMA,
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
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
            output_schema=JOB_SCHEMA,
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=True,
                open_world_hint=True,
            ),
        ),
        Tool(
            name="comfyui.server.list",
            description="List enabled ComfyUI servers without credentials or private URLs.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object"},
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        ),
        Tool(
            name="comfyui.server.health",
            description="Check one ComfyUI server and report runtime device information.",
            input_schema={
                "type": "object",
                "properties": {"server_id": discovery_properties["server_id"]},
                "required": ["server_id"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
        ),
        Tool(
            name="comfyui.node.list",
            description="Search installed ComfyUI node classes with cursor pagination.",
            input_schema={
                "type": "object",
                "properties": discovery_properties,
                "required": ["server_id"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
        ),
        Tool(
            name="comfyui.node.describe",
            description="Return the complete definition of one installed ComfyUI node class.",
            input_schema={
                "type": "object",
                "properties": {
                    "server_id": discovery_properties["server_id"],
                    "node_class": {"type": "string", "minLength": 1},
                },
                "required": ["server_id", "node_class"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
        ),
        Tool(
            name="comfyui.model.list",
            description="List model folders or search models within one folder.",
            input_schema={
                "type": "object",
                "properties": {
                    **discovery_properties,
                    "kind": {"type": "string", "default": ""},
                },
                "required": ["server_id"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
        ),
        Tool(
            name="comfyui.revision.list",
            description="List immutable revisions for one workflow.",
            input_schema={
                "type": "object",
                "properties": {"workflow_id": {"type": "string", "minLength": 1}},
                "required": ["workflow_id"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        ),
        Tool(
            name="comfyui.workflow.describe",
            description="Describe one server's published workflow revision and deployment.",
            input_schema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string", "minLength": 1},
                    "server_id": {"type": "string", "minLength": 1},
                },
                "required": ["workflow_id", "server_id"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        ),
        Tool(
            name="comfyui.workflow.dependencies.check",
            description="Check required nodes and models for a published workflow revision.",
            input_schema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string", "minLength": 1},
                    "server_id": {"type": "string", "minLength": 1},
                },
                "required": ["workflow_id", "server_id"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
        ),
    ]
    return [decorate_tool(tool) for tool in tools]


def phase_h_tools() -> list[Tool]:
    """Return the stable Phase H observability and operations Tool surface."""

    server_identifier = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": r"^(?!.*[\r\n])[A-Za-z0-9][A-Za-z0-9_-]{0,127}$",
    }
    public_identifier = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": r"^(?!.*[\r\n])[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
    }
    observation_cursor = {"type": "string", "maxLength": 512, "default": ""}
    job_cursor = {"type": "string", "maxLength": 2048, "default": ""}
    page_limit = {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}
    capability_state = {
        "type": "string",
        "enum": ["supported", "unsupported", "unauthorized", "temporarily_unavailable"],
    }

    def page_output(item_schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "server_id": {"type": "string"},
                "capability_state": capability_state,
                "items": {"type": "array", "items": item_schema},
                "next_cursor": {"type": "string"},
                "total": {"type": "integer", "minimum": 0},
            },
            "required": ["server_id", "capability_state", "items", "next_cursor", "total"],
            "additionalProperties": False,
        }

    job_list_output = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        name: {"type": "string"}
                        for name in (
                            "job_uri",
                            "job_id",
                            "workflow_id",
                            "revision_id",
                            "deployment_id",
                            "server_id",
                            "status",
                            "created_at",
                        )
                    },
                    "required": [
                        "job_uri",
                        "job_id",
                        "workflow_id",
                        "revision_id",
                        "deployment_id",
                        "server_id",
                        "status",
                        "created_at",
                    ],
                    "additionalProperties": False,
                },
            },
            "next_cursor": {"type": "string"},
        },
        "required": ["items", "next_cursor"],
        "additionalProperties": False,
    }
    queue_output = page_output(
        {
            "type": "object",
            "properties": {
                "state": {"type": "string", "enum": ["running", "pending"]},
                "prompt_id": {"type": "string"},
                "job_id": {"type": "string"},
                "queue_number": {"type": "number"},
            },
            "required": ["state"],
            "additionalProperties": False,
        }
    )
    log_output = page_output(
        {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "timestamp": {"type": "string"},
                "level": {"type": "string"},
            },
            "required": ["message"],
            "additionalProperties": False,
        }
    )
    template_output = page_output(
        {
            "type": "object",
            "properties": {
                name: {"type": "string"}
                for name in ("template_id", "name", "description", "category", "source")
            },
            "required": ["template_id", "name", "description", "category", "source"],
            "additionalProperties": False,
        }
    )
    subgraph_item = {
        "type": "object",
        "properties": {
            "subgraph_id": {"type": "string"},
            "name": {"type": "string"},
            "source": {"type": "string"},
            "node_pack": {"type": "string"},
            "node_count": {"type": "integer", "minimum": 0},
            "link_count": {"type": "integer", "minimum": 0},
        },
        "required": ["subgraph_id", "name", "source", "node_pack"],
        "additionalProperties": False,
    }
    capability_value = {
        "type": "object",
        "properties": {"state": capability_state},
        "required": ["state"],
        "additionalProperties": False,
    }
    capability_names = [
        "jobs_api",
        "userdata_v2",
        "userdata_traditional",
        "node_replacements",
        "manager_queue_status",
        "manager_install",
        "logs",
        "workflow_templates",
        "subgraphs",
    ]
    capabilities_output = {
        "type": "object",
        "properties": {
            "server_id": {"type": "string"},
            "capabilities": {
                "type": "object",
                "properties": {
                    **{name: capability_value for name in capability_names},
                    "userdata": {
                        "type": "object",
                        "properties": {
                            "state": capability_state,
                            "variant": {
                                "type": "string",
                                "enum": ["v2", "traditional", ""],
                            },
                        },
                        "required": ["state", "variant"],
                        "additionalProperties": False,
                    },
                },
                "required": [*capability_names, "userdata"],
                "additionalProperties": False,
            },
        },
        "required": ["server_id", "capabilities"],
        "additionalProperties": False,
    }
    subgraph_output = {
        "type": "object",
        "properties": {
            "server_id": {"type": "string"},
            "capability_state": capability_state,
            "subgraph": {"anyOf": [subgraph_item, {"type": "null"}]},
        },
        "required": ["server_id", "capability_state", "subgraph"],
        "additionalProperties": False,
    }
    free_output = {
        "type": "object",
        "properties": {
            "server_id": {"type": "string"},
            "success": {"type": "boolean"},
            "unload_models": {"type": "boolean"},
            "free_memory": {"type": "boolean"},
            "impact": {
                "type": "array",
                "items": {"type": "string", "enum": ["loaded_models", "runtime_memory"]},
                "uniqueItems": True,
            },
            "audit_status": {"type": "string", "enum": ["not_configured"]},
        },
        "required": [
            "server_id",
            "success",
            "unload_models",
            "free_memory",
            "impact",
            "audit_status",
        ],
        "additionalProperties": False,
    }
    tools = [
        Tool(
            name="comfyui.job.list",
            description="List this principal's durable jobs with owner-bound cursor pagination.",
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": [
                            "",
                            "reserved",
                            "submission_unknown",
                            "submitted",
                            "queued",
                            "running",
                            "completed",
                            "error",
                            "interrupted",
                            "cancelled",
                            "lost",
                        ],
                        "default": "",
                    },
                    "workflow_id": {
                        "type": "string",
                        "maxLength": 128,
                        "pattern": r"^(?!.*[\r\n])(?:|[A-Za-z0-9][A-Za-z0-9_-]{0,127})$",
                        "default": "",
                    },
                    "server_id": {
                        "type": "string",
                        "maxLength": 128,
                        "pattern": r"^(?!.*[\r\n])(?:|[A-Za-z0-9][A-Za-z0-9_-]{0,127})$",
                        "default": "",
                    },
                    "created_after": {"type": "string", "maxLength": 64, "default": ""},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 50,
                    },
                    "cursor": job_cursor,
                },
                "additionalProperties": False,
            },
            output_schema=job_list_output,
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        ),
        Tool(
            name="comfyui.queue.list",
            description="List bounded running and pending queue entries without prompt payloads.",
            input_schema={
                "type": "object",
                "properties": {
                    "server_id": server_identifier,
                    "limit": page_limit,
                    "cursor": observation_cursor,
                },
                "required": ["server_id"],
                "additionalProperties": False,
            },
            output_schema=queue_output,
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
        ),
        Tool(
            name="comfyui.log.read",
            description="Read a bounded cursor page of redacted ComfyUI log lines.",
            input_schema={
                "type": "object",
                "properties": {
                    "server_id": server_identifier,
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1000,
                        "default": 100,
                    },
                    "cursor": observation_cursor,
                },
                "required": ["server_id"],
                "additionalProperties": False,
            },
            output_schema=log_output,
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
        ),
        Tool(
            name="comfyui.server.capabilities",
            description=(
                "Report optional ComfyUI API capability states without exposing connection data."
            ),
            input_schema={
                "type": "object",
                "properties": {"server_id": server_identifier},
                "required": ["server_id"],
                "additionalProperties": False,
            },
            output_schema=capabilities_output,
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
        ),
        Tool(
            name="comfyui.template.list",
            description="List redacted workflow template summaries with cursor pagination.",
            input_schema={
                "type": "object",
                "properties": {
                    "server_id": server_identifier,
                    "limit": page_limit,
                    "cursor": observation_cursor,
                },
                "required": ["server_id"],
                "additionalProperties": False,
            },
            output_schema=template_output,
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
        ),
        Tool(
            name="comfyui.subgraph.list",
            description="List redacted global subgraph summaries with cursor pagination.",
            input_schema={
                "type": "object",
                "properties": {
                    "server_id": server_identifier,
                    "limit": page_limit,
                    "cursor": observation_cursor,
                },
                "required": ["server_id"],
                "additionalProperties": False,
            },
            output_schema=page_output(subgraph_item),
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
        ),
        Tool(
            name="comfyui.subgraph.get",
            description=(
                "Read one redacted global subgraph summary without its workflow graph payload."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "server_id": server_identifier,
                    "subgraph_id": public_identifier,
                },
                "required": ["server_id", "subgraph_id"],
                "additionalProperties": False,
            },
            output_schema=subgraph_output,
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
        ),
        Tool(
            name="comfyui.server.free",
            description="Unload models or free memory on one server and report the audited impact.",
            input_schema={
                "type": "object",
                "properties": {
                    "server_id": server_identifier,
                    "unload_models": {"type": "boolean", "default": False},
                    "free_memory": {"type": "boolean", "default": False},
                },
                "required": ["server_id"],
                "anyOf": [
                    {
                        "required": ["unload_models"],
                        "properties": {"unload_models": {"const": True}},
                    },
                    {"required": ["free_memory"], "properties": {"free_memory": {"const": True}}},
                ],
                "additionalProperties": False,
            },
            output_schema=free_output,
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=True,
                open_world_hint=True,
            ),
        ),
    ]
    return [decorate_tool(tool) for tool in tools]
