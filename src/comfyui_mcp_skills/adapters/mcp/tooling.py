"""MCP tool schemas, naming, argument, and result serialization helpers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import CallToolResult, ContentBlock, ResourceLink, TextContent, Tool, ToolAnnotations

from comfyui_mcp_skills.application.auth_context import current_authorization
from comfyui_mcp_skills.application.authorization import Scope, parse_scopes
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
    data = asdict(job)
    data["outputs"] = list(job.outputs)
    data.pop("request_digest", None)
    data.pop("owner_id", None)
    data.pop("idempotency_key", None)
    data.pop("client_id", None)
    data["job_uri"] = f"comfyui://jobs/{job.job_id}" if job.job_id else ""
    return data


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


def required_string(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value:
        raise TypeError(f"{name} must be a non-empty string")
    return value


def optional_string(arguments: dict[str, Any], name: str, default: str) -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def validate_fixed_arguments(arguments: dict[str, Any], allowed: set[str]) -> None:
    unexpected = set(arguments) - allowed
    if unexpected:
        raise ValueError(f"Unexpected arguments: {', '.join(sorted(unexpected))}")


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
            output_schema=JOB_SCHEMA,
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
            output_schema=JOB_SCHEMA,
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=True,
                open_world_hint=False,
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
    ]
