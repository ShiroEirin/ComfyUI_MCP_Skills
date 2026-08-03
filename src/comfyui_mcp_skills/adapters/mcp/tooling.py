"""MCP tool schemas, naming, argument, and result serialization helpers."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from typing import Any
from urllib.parse import urlsplit

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
        legacy_uri = raw.get("legacy_uri")
        if isinstance(legacy_uri, str):
            legacy_parts = urlsplit(legacy_uri)
            legacy_segments = [part for part in legacy_parts.path.split("/") if part]
            if (
                legacy_parts.scheme != "comfyui"
                or legacy_parts.netloc != "outputs"
                or len(legacy_segments) != 3
                or legacy_parts.query
                or legacy_parts.fragment
            ):
                legacy_uri = ""
        else:
            legacy_uri = ""
        resource_uri = raw.get("resource_uri")
        if isinstance(resource_uri, str):
            resource_parts = urlsplit(resource_uri)
            resource_segments = [part for part in resource_parts.path.split("/") if part]
            if (
                resource_parts.scheme != "comfyui"
                or resource_parts.netloc != "artifacts"
                or len(resource_segments) != 1
                or not re.fullmatch(r"artifact_[A-Za-z0-9_-]{1,119}", resource_segments[0])
                or resource_parts.query
                or resource_parts.fragment
            ):
                resource_uri = ""
        else:
            resource_uri = ""
        if not resource_uri:
            resource_uri = f"comfyui://outputs/{job.server_id}/{job.prompt_id}/{index}"
        output = {
            "filename": filename,
            "subfolder": subfolder,
            "type": "output",
            "media_type": media_type,
            "mime_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
            "resource_uri": resource_uri,
        }
        if legacy_uri:
            output["legacy_uri"] = legacy_uri
        artifact_id = raw.get("artifact_id")
        if isinstance(artifact_id, str) and re.fullmatch(
            r"artifact_[A-Za-z0-9_-]{1,119}", artifact_id
        ):
            output["artifact_id"] = artifact_id
        for key in ("upstream_node_id", "output_key"):
            value = raw.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
                output[key] = value
        output_index = raw.get("upstream_output_index")
        if (
            isinstance(output_index, int)
            and not isinstance(output_index, bool)
            and output_index >= 0
        ):
            output["upstream_output_index"] = output_index
        legacy_index = raw.get("legacy_index")
        if (
            isinstance(legacy_index, int)
            and not isinstance(legacy_index, bool)
            and legacy_index >= 0
        ):
            output["legacy_index"] = legacy_index
        outputs.append(output)
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


_EXPERIMENT_PUBLIC_FIELDS = frozenset(
    {
        "plan_id",
        "plan_digest",
        "experiment_id",
        "workflow_id",
        "server_id",
        "status",
        "expansion",
        "budgets",
        "budget_totals",
        "variant_count",
        "failure_policy",
        "concurrency",
        "submission_window",
        "pinned_revision_id",
        "pinned_deployment_id",
        "pinned_content_digest",
        "execution_slots",
        "subject_submission_quota",
        "retained_plan_bytes",
        "expires_at",
        "pending_count",
        "submitted_count",
        "running_count",
        "completed_count",
        "failed_count",
        "cancelled_count",
        "lost_count",
        "cancel_mode",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "cancelled_at",
        "resource_uri",
    }
)
_VARIANT_PUBLIC_FIELDS = frozenset(
    {
        "experiment_id",
        "variant_id",
        "ordinal",
        "parameter_digest",
        "status",
        "job_id",
        "job_uri",
        "artifact_uris",
        "measured_pixels",
        "measured_outputs",
        "measured_seconds",
        "error_code",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "resource_uri",
        "ratings",
        "promotions",
    }
)


def experiment_dict(value: dict[str, Any]) -> dict[str, Any]:
    """Allowlist a public Experiment summary without expanded parameter payloads."""
    result = {name: value[name] for name in _EXPERIMENT_PUBLIC_FIELDS if name in value}
    expansion = result.get("expansion")
    if isinstance(expansion, dict):
        mode = expansion.get("mode")
        result["expansion"] = {"mode": mode} if isinstance(mode, str) else {}
    return result


def variant_dict(value: dict[str, Any]) -> dict[str, Any]:
    """Allowlist one public Variant summary without its resolved arguments."""
    return {name: value[name] for name in _VARIANT_PUBLIC_FIELDS if name in value}


def variant_page_dict(value: dict[str, Any]) -> dict[str, Any]:
    """Bound and allowlist one keyset-paginated Variant page."""
    raw_items = value.get("items", [])
    if not isinstance(raw_items, list) or len(raw_items) > 100:
        raise ValueError("Variant page exceeds 100 items")
    cursor = value.get("next_cursor", "")
    if not isinstance(cursor, str) or len(cursor) > 2048:
        raise ValueError("Invalid Variant cursor")
    return {
        "items": [variant_dict(item) for item in raw_items if isinstance(item, dict)],
        "next_cursor": cursor,
    }


def rating_dict(value: dict[str, Any]) -> dict[str, Any]:
    """Allowlist one immutable-rubric Variant rating."""
    fields = (
        "rating_id",
        "experiment_id",
        "variant_id",
        "rubric_version",
        "rubric_definition",
        "scores",
        "created_at",
        "updated_at",
    )
    return {name: value[name] for name in fields if name in value}


def promotion_dict(value: dict[str, Any]) -> dict[str, Any]:
    """Allowlist one preset or immutable unpublished Revision promotion."""
    fields = (
        "promotion_id",
        "experiment_id",
        "variant_id",
        "target",
        "preset_id",
        "revision_id",
        "published",
        "created_at",
    )
    return {name: value[name] for name in fields if name in value}


_DIAGNOSTIC_PUBLIC_FIELDS = frozenset(
    {
        "diagnostic_id",
        "registry_version",
        "subject_uri",
        "classification",
        "retryable",
        "evidence",
        "safe_actions",
        "approval_actions",
        "created_at",
    }
)
_DIAGNOSTIC_ACTION_FIELDS = frozenset({"tool", "name", "required_arguments", "risk"})
_REPAIR_PLAN_PUBLIC_FIELDS = frozenset(
    {
        "repair_plan_id",
        "plan_digest",
        "resource_uri",
        "original_job_id",
        "workflow_id",
        "server_id",
        "pinned_plan_id",
        "pinned_revision_id",
        "pinned_deployment_id",
        "normalized_changes",
        "diff",
        "original_arguments_digest",
        "resulting_arguments_digest",
        "status",
        "created_at",
        "expires_at",
        "result_job_id",
        "result_job_uri",
        "retry_of",
        "committed_at",
    }
)


def _bounded_public_string(value: Any, *, maximum: int = 2048) -> str | None:
    return value if isinstance(value, str) and len(value) <= maximum else None


def _diagnostic_action(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    tool = _bounded_public_string(value.get("tool"), maximum=128)
    name = _bounded_public_string(value.get("name"), maximum=256)
    required = value.get("required_arguments")
    risk = value.get("risk")
    if tool is not None and re.fullmatch(r"comfyui\.[A-Za-z0-9_.-]{1,127}", tool):
        result["tool"] = tool
    if name is not None:
        result["name"] = name
    if isinstance(required, dict) and len(required) <= 32:
        result["required_arguments"] = {
            key: item
            for key, item in required.items()
            if isinstance(key, str)
            and len(key) <= 128
            and (
                (isinstance(item, str) and len(item) <= 2048)
                or isinstance(item, bool)
                or (isinstance(item, dict) and not item)
            )
        }
    if isinstance(risk, str) and risk in {"safe", "approval_required", "low", "medium", "high"}:
        result["risk"] = risk
    return {key: result[key] for key in _DIAGNOSTIC_ACTION_FIELDS if key in result}


def _diagnostic_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    status = value.get("status")
    if isinstance(status, str):
        result["status"] = status[:256]
    failed_node = value.get("failed_node")
    if isinstance(failed_node, dict):
        result["failed_node"] = {
            key: str(failed_node[key])[:256]
            for key in ("node_id", "class_type", "error_type", "message")
            if isinstance(failed_node.get(key), str)
        }
    events = value.get("events")
    if isinstance(events, list):
        result["events"] = [
            {
                key: str(item[key])[:256]
                for key in ("event_type", "occurred_at", "message")
                if isinstance(item, dict) and isinstance(item.get(key), str)
            }
            for item in events[:32]
            if isinstance(item, dict)
        ]
    logs = value.get("log_window")
    if isinstance(logs, list):
        result["log_window"] = [str(item)[:2048] for item in logs[:32]]
    return result


def diagnostic_report_dict(value: dict[str, Any]) -> dict[str, Any]:
    """Project one bounded Diagnostic Report without raw errors or commands."""
    result = {name: value[name] for name in _DIAGNOSTIC_PUBLIC_FIELDS if name in value}
    result["evidence"] = _diagnostic_evidence(value.get("evidence", {}))
    for key in ("safe_actions", "approval_actions"):
        actions = value.get(key, [])
        result[key] = (
            [_diagnostic_action(item) for item in actions[:16] if isinstance(item, dict)]
            if isinstance(actions, list)
            else []
        )
    return result


def repair_plan_dict(value: dict[str, Any]) -> dict[str, Any]:
    """Project a repair plan while retaining no immutable raw argument snapshots."""
    return {name: value[name] for name in _REPAIR_PLAN_PUBLIC_FIELDS if name in value}


def tool_result(data: dict[str, Any], *, error: bool = False) -> CallToolResult:
    content: list[ContentBlock] = [
        TextContent(type="text", text=json.dumps(data, ensure_ascii=False))
    ]
    if not error:
        resource_uri = data.get("resource_uri")
        diagnostic_id = data.get("diagnostic_id")
        if isinstance(diagnostic_id, str) and re.fullmatch(
            r"diagnostic_[0-9a-f]{64}",
            diagnostic_id,
        ):
            diagnostic_uri = f"comfyui://diagnostics/{diagnostic_id}"
            content.append(
                ResourceLink(
                    type="resource_link",
                    uri=diagnostic_uri,
                    name=diagnostic_id,
                    mime_type="application/json",
                )
            )
        plan_uri = data.get("resource_uri")
        if not isinstance(plan_uri, str):
            plan_id = data.get("repair_plan_id")
            if isinstance(plan_id, str) and re.fullmatch(
                r"repair_plan_[A-Za-z0-9_-]{1,119}", plan_id
            ):
                plan_uri = f"comfyui://plans/{plan_id}"
        if isinstance(plan_uri, str) and re.fullmatch(
            r"comfyui://plans/repair_plan_[A-Za-z0-9_-]{1,119}", plan_uri
        ):
            content.append(
                ResourceLink(
                    type="resource_link",
                    uri=plan_uri,
                    name=plan_uri.rsplit("/", 1)[-1],
                    mime_type="application/json",
                )
            )
        if isinstance(resource_uri, str) and re.fullmatch(
            r"comfyui://experiments/experiment_[A-Za-z0-9_-]{1,117}"
            r"(?:/variants/variant_[A-Za-z0-9_-]{1,120})?",
            resource_uri,
        ):
            content.append(
                ResourceLink(
                    type="resource_link",
                    uri=resource_uri,
                    name=resource_uri.rsplit("/", 1)[-1],
                    mime_type="application/json",
                )
            )
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
                legacy_uri = output.get("legacy_uri")
                if isinstance(legacy_uri, str) and legacy_uri and legacy_uri != uri:
                    content.append(
                        ResourceLink(
                            type="resource_link",
                            uri=legacy_uri,
                            name=(
                                f"{filename} (legacy alias)"
                                if isinstance(filename, str) and filename
                                else legacy_uri
                            ),
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


def required_object(
    arguments: dict[str, Any],
    name: str,
    *,
    max_properties: int,
    max_bytes: int,
) -> dict[str, Any]:
    value = arguments.get(name)
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    if len(value) > max_properties:
        raise ValueError(f"{name} exceeds {max_properties} properties")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain finite JSON values") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{name} exceeds {max_bytes} bytes")
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
            name="comfyui.revision.diff",
            description="Compare two immutable revisions of one workflow.",
            input_schema={
                "type": "object",
                "properties": {
                    "from_revision_id": {"type": "string", "minLength": 1},
                    "to_revision_id": {"type": "string", "minLength": 1},
                },
                "required": ["from_revision_id", "to_revision_id"],
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


def phase_l_tools() -> list[Tool]:
    """Return the stable Phase L asset, Artifact, and transfer Tool surface."""
    identifier = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": r"^(?!.*[\r\n])[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    }
    asset_id = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": r"^(?!.*[\r\n])asset_[A-Za-z0-9_-]{1,121}$",
    }
    artifact_id = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": r"^(?!.*[\r\n])artifact_[A-Za-z0-9_-]{1,119}$",
    }
    cursor = {"type": "string", "maxLength": 2048, "default": ""}
    media_type = {
        "type": "string",
        "enum": ["", "image", "audio", "video"],
        "default": "",
    }
    collection = {
        "type": "string",
        "maxLength": 128,
        "pattern": r"^(?!.*[\r\n])[A-Za-z0-9_. -]{0,128}$",
        "default": "",
    }
    object_result = {"type": "object"}
    page_result = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "object"}},
            "next_cursor": {"type": "string"},
            "total": {"type": "integer", "minimum": 0},
        },
        "required": ["items", "next_cursor"],
        "additionalProperties": False,
    }

    tools = [
        Tool(
            name="comfyui.asset.list",
            description="List owner-visible assets with bounded cursor pagination and filters.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                    "cursor": cursor,
                    "media_type": media_type,
                    "collection": collection,
                },
                "additionalProperties": False,
            },
            output_schema=page_result,
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        ),
        Tool(
            name="comfyui.asset.describe",
            description="Describe one owner-visible asset without host paths or private locators.",
            input_schema={
                "type": "object",
                "properties": {"asset_id": asset_id},
                "required": ["asset_id"],
                "additionalProperties": False,
            },
            output_schema=object_result,
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        ),
        Tool(
            name="comfyui.asset.collection.update",
            description="Add or remove owned assets from a named collection.",
            input_schema={
                "type": "object",
                "properties": {
                    "collection": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "pattern": r"^(?!.*[\r\n])[A-Za-z0-9_. -]{1,128}$",
                    },
                    "asset_ids": {
                        "type": "array",
                        "items": asset_id,
                        "minItems": 1,
                        "maxItems": 100,
                        "uniqueItems": True,
                    },
                    "action": {"type": "string", "enum": ["add", "remove"]},
                },
                "required": ["collection", "asset_ids", "action"],
                "additionalProperties": False,
            },
            output_schema=object_result,
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=False,
            ),
        ),
        Tool(
            name="comfyui.asset.metadata.extract",
            description="Extract safe generation metadata from one owned asset.",
            input_schema={
                "type": "object",
                "properties": {"asset_id": asset_id},
                "required": ["asset_id"],
                "additionalProperties": False,
            },
            output_schema=object_result,
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        ),
        Tool(
            name="comfyui.asset.import_output",
            description=(
                "Directly reuse a graph-compatible owned Artifact or return a reviewable "
                "verified transfer plan."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_id": artifact_id,
                    "target_server_id": identifier,
                    "workflow_id": identifier,
                    "parameter_name": identifier,
                },
                "required": [
                    "artifact_id",
                    "target_server_id",
                    "workflow_id",
                    "parameter_name",
                ],
                "additionalProperties": False,
            },
            output_schema=object_result,
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=True,
            ),
        ),
        Tool(
            name="comfyui.asset.delete.plan",
            description="Plan deletion of an owned asset and return its bounded impact.",
            input_schema={
                "type": "object",
                "properties": {"asset_id": asset_id},
                "required": ["asset_id"],
                "additionalProperties": False,
            },
            output_schema=object_result,
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=False,
            ),
        ),
        Tool(
            name="comfyui.asset.delete.commit",
            description="Commit an unexpired digest-bound asset deletion plan.",
            input_schema={
                "type": "object",
                "properties": {"plan_id": identifier, "plan_digest": identifier},
                "required": ["plan_id", "plan_digest"],
                "additionalProperties": False,
            },
            output_schema=object_result,
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=False,
                open_world_hint=False,
            ),
        ),
        Tool(
            name="comfyui.asset.transfer.plan",
            description="Verify source bytes and plan a digest-bound Artifact upload.",
            input_schema={
                "type": "object",
                "properties": {"artifact_id": artifact_id, "target_server_id": identifier},
                "required": ["artifact_id", "target_server_id"],
                "additionalProperties": False,
            },
            output_schema=object_result,
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=True,
            ),
        ),
        Tool(
            name="comfyui.asset.transfer.commit",
            description=("Upload and read back an unexpired digest-bound Artifact transfer plan."),
            input_schema={
                "type": "object",
                "properties": {"transfer_id": identifier, "plan_digest": identifier},
                "required": ["transfer_id", "plan_digest"],
                "additionalProperties": False,
            },
            output_schema=object_result,
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=True,
            ),
        ),
        Tool(
            name="comfyui.asset.transfer.get",
            description="Read one owner-bound Artifact transfer state.",
            input_schema={
                "type": "object",
                "properties": {"transfer_id": identifier},
                "required": ["transfer_id"],
                "additionalProperties": False,
            },
            output_schema=object_result,
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        ),
    ]
    return [decorate_tool(tool) for tool in tools]


def phase_m_tools() -> list[Tool]:
    """Return the stable Phase M experiment and Variant Tool surface."""
    identifier = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": r"^(?!.*[\r\n])[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    }
    experiment_id = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": r"^(?!.*[\r\n])experiment_[A-Za-z0-9_-]{1,117}$",
    }
    variant_id = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": r"^(?!.*[\r\n])variant_[A-Za-z0-9_-]{1,120}$",
    }
    parameter_name = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": r"^(?!.*[\r\n])[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    }
    parameter_sets = {
        "type": "object",
        "minProperties": 1,
        "maxProperties": 64,
        "propertyNames": parameter_name,
        "additionalProperties": {
            "type": "array",
            "minItems": 1,
            "maxItems": 10_000,
        },
    }
    explicit_variant = {
        "type": "object",
        "maxProperties": 64,
        "propertyNames": parameter_name,
    }
    expansion = {
        "oneOf": [
            {
                "type": "object",
                "properties": {"mode": {"const": "matrix"}, "parameters": parameter_sets},
                "required": ["mode", "parameters"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"mode": {"const": "zip"}, "parameters": parameter_sets},
                "required": ["mode", "parameters"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "mode": {"const": "sample"},
                    "parameters": parameter_sets,
                    "seed": {"type": "integer", "minimum": -(2**63), "maximum": 2**63 - 1},
                    "count": {"type": "integer", "minimum": 1, "maximum": 10_000},
                },
                "required": ["mode", "parameters", "seed", "count"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "mode": {"const": "explicit"},
                    "variants": {
                        "type": "array",
                        "items": explicit_variant,
                        "minItems": 1,
                        "maxItems": 10_000,
                    },
                },
                "required": ["mode", "variants"],
                "additionalProperties": False,
            },
        ]
    }
    budgets = {
        "type": "object",
        "properties": {
            "max_variants": {"type": "integer", "minimum": 1, "maximum": 10_000},
            "max_concurrency": {"type": "integer", "minimum": 1, "maximum": 64},
            "max_pixels": {"type": "integer", "minimum": 1, "maximum": 10**15},
            "max_outputs": {"type": "integer", "minimum": 1, "maximum": 100_000},
            "max_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 31_536_000},
        },
        "required": [
            "max_variants",
            "max_concurrency",
            "max_pixels",
            "max_outputs",
            "max_seconds",
        ],
        "additionalProperties": False,
    }
    object_result = {"type": "object"}
    tools = [
        Tool(
            name="comfyui.experiment.plan",
            description=(
                "Plan against one pinned published workflow context, validate every Variant, "
                "and calculate trusted bounded costs without inlining expanded Variants."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workflow_id": identifier,
                    "server_id": identifier,
                    "expansion": expansion,
                    "preset_id": {
                        "type": "string",
                        "pattern": r"^(?!.*[\r\n])preset_[A-Za-z0-9_-]{1,120}$",
                    },
                    "base_arguments": {"type": "object", "maxProperties": 64},
                    "budgets": budgets,
                    "failure_policy": {
                        "type": "string",
                        "enum": ["continue", "stop_new", "cancel_queued"],
                    },
                    "concurrency": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 64,
                        "default": 1,
                    },
                    "submission_window": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 10_000,
                        "default": 0,
                    },
                },
                "required": [
                    "workflow_id",
                    "server_id",
                    "expansion",
                    "base_arguments",
                    "budgets",
                    "failure_policy",
                ],
                "additionalProperties": False,
            },
            output_schema=object_result,
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=False,
            ),
        ),
        Tool(
            name="comfyui.experiment.commit",
            description="Commit one owned digest-bound experiment plan and return its Resource.",
            input_schema={
                "type": "object",
                "properties": {"plan_id": identifier, "plan_digest": identifier},
                "required": ["plan_id", "plan_digest"],
                "additionalProperties": False,
            },
            output_schema=object_result,
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=True,
            ),
        ),
        Tool(
            name="comfyui.experiment.get",
            description="Read one owner-bound experiment summary without inlining Variants.",
            input_schema={
                "type": "object",
                "properties": {"experiment_id": experiment_id},
                "required": ["experiment_id"],
                "additionalProperties": False,
            },
            output_schema=object_result,
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        ),
        Tool(
            name="comfyui.experiment.cancel",
            description=(
                "Stop new work for one owned experiment using an explicit cancellation mode."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "experiment_id": experiment_id,
                    "mode": {"type": "string", "enum": ["stop_new", "cancel_queued"]},
                },
                "required": ["experiment_id", "mode"],
                "additionalProperties": False,
            },
            output_schema=object_result,
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=True,
                open_world_hint=True,
            ),
        ),
        Tool(
            name="comfyui.experiment.variant.list",
            description="List owner-visible experiment Variants with bounded keyset pagination.",
            input_schema={
                "type": "object",
                "properties": {
                    "experiment_id": experiment_id,
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 50,
                    },
                    "cursor": {"type": "string", "maxLength": 2048, "default": ""},
                },
                "required": ["experiment_id"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": {"type": "object"}, "maxItems": 100},
                    "next_cursor": {"type": "string"},
                },
                "required": ["items", "next_cursor"],
                "additionalProperties": False,
            },
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        ),
        Tool(
            name="comfyui.experiment.variant.rate",
            description="Record immutable-rubric bounded numeric scores for one owned Variant.",
            input_schema={
                "type": "object",
                "properties": {
                    "experiment_id": experiment_id,
                    "variant_id": variant_id,
                    "rubric_version": identifier,
                    "scores": {
                        "type": "object",
                        "minProperties": 1,
                        "maxProperties": 32,
                        "propertyNames": parameter_name,
                        "additionalProperties": {
                            "type": "number",
                            "minimum": -1_000_000,
                            "maximum": 1_000_000,
                        },
                    },
                },
                "required": ["experiment_id", "variant_id", "rubric_version", "scores"],
                "additionalProperties": False,
            },
            output_schema=object_result,
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=False,
            ),
        ),
        Tool(
            name="comfyui.experiment.variant.promote",
            description="Promote one owned Variant to a preset or immutable unpublished Revision.",
            input_schema={
                "type": "object",
                "properties": {
                    "experiment_id": experiment_id,
                    "variant_id": variant_id,
                    "target": {"type": "string", "enum": ["preset", "revision"]},
                },
                "required": ["experiment_id", "variant_id", "target"],
                "additionalProperties": False,
            },
            output_schema=object_result,
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=False,
            ),
        ),
    ]
    return [decorate_tool(tool) for tool in tools]


def phase_n_tools() -> list[Tool]:
    """Return fixed structured diagnostics and safe recovery tools."""
    job_id = {
        "type": "string",
        "minLength": 34,
        "maxLength": 128,
        "pattern": r"^(?!.*[\r\n])job_[A-Za-z0-9_-]{32,119}$",
    }
    server_id = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": r"^(?!.*[\r\n])[A-Za-z0-9][A-Za-z0-9_-]{0,127}$",
    }
    diagnostic_id = {
        "type": "string",
        "minLength": 75,
        "maxLength": 75,
        "pattern": r"^(?!.*[\r\n])diagnostic_[0-9a-f]{64}$",
    }
    repair_plan_id = {
        "type": "string",
        "minLength": 76,
        "maxLength": 76,
        "pattern": r"^(?!.*[\r\n])repair_plan_[0-9a-f]{64}$",
    }
    digest = {
        "type": "string",
        "minLength": 64,
        "maxLength": 64,
        "pattern": r"^[0-9a-f]{64}$",
    }
    diagnostic_output = {
        "type": "object",
        "properties": {
            "diagnostic_id": diagnostic_id,
            "registry_version": {"type": "string", "maxLength": 128},
            "subject_uri": {"type": "string", "maxLength": 2048},
            "classification": {"type": "string", "maxLength": 128},
            "retryable": {"type": "boolean"},
            "evidence": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "failed_node": {"type": "object"},
                    "events": {"type": "array", "maxItems": 8},
                    "log_window": {"type": "array", "maxItems": 8},
                },
                "required": ["status", "failed_node", "events", "log_window"],
                "additionalProperties": False,
            },
            "safe_actions": {"type": "array", "maxItems": 16, "items": {"type": "object"}},
            "approval_actions": {"type": "array", "maxItems": 16, "items": {"type": "object"}},
            "created_at": {"type": "string", "maxLength": 64},
        },
        "required": [
            "diagnostic_id",
            "registry_version",
            "subject_uri",
            "classification",
            "retryable",
            "evidence",
            "safe_actions",
            "approval_actions",
            "created_at",
        ],
        "additionalProperties": False,
    }
    plan_properties: dict[str, Any] = {
        name: {"type": "object" if name == "normalized_changes" else "string"}
        for name in _REPAIR_PLAN_PUBLIC_FIELDS
    }
    plan_properties["diff"] = {"type": "array"}
    planned_fields = [
        "repair_plan_id",
        "plan_digest",
        "resource_uri",
        "original_job_id",
        "workflow_id",
        "server_id",
        "pinned_plan_id",
        "pinned_revision_id",
        "pinned_deployment_id",
        "normalized_changes",
        "diff",
        "original_arguments_digest",
        "resulting_arguments_digest",
        "status",
        "created_at",
        "expires_at",
    ]
    plan_output: dict[str, Any] = {
        "type": "object",
        "properties": plan_properties,
        "required": planned_fields,
        "additionalProperties": False,
    }
    committed_plan_output: dict[str, Any] = {
        **plan_output,
        "required": [
            *planned_fields,
            "result_job_id",
            "result_job_uri",
            "retry_of",
            "committed_at",
        ],
    }
    return [
        decorate_tool(
            Tool(
                name="comfyui.job.diagnose",
                description=(
                    "Generate one bounded, redacted structured Diagnostic Report for an owned Job."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"job_id": job_id},
                    "required": ["job_id"],
                    "additionalProperties": False,
                },
                output_schema=diagnostic_output,
                annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
            ),
            risk="low",
            toolset="execution",
        ),
        decorate_tool(
            Tool(
                name="comfyui.server.diagnose",
                description=(
                    "Generate one bounded, redacted structured Diagnostic Report "
                    "for an observed server."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"server_id": server_id},
                    "required": ["server_id"],
                    "additionalProperties": False,
                },
                output_schema=diagnostic_output,
                annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
            ),
            risk="low",
            toolset="operations",
        ),
        decorate_tool(
            Tool(
                name="comfyui.job.retry.plan",
                description=(
                    "Create an owner-bound, digest-bound retry plan with an exact change diff."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "job_id": job_id,
                        "changes": {"type": "object", "maxProperties": 64},
                    },
                    "required": ["job_id", "changes"],
                    "additionalProperties": False,
                },
                output_schema=plan_output,
                annotations=ToolAnnotations(
                    read_only_hint=False, destructive_hint=False, open_world_hint=False
                ),
            ),
            risk="medium",
            toolset="execution",
        ),
        decorate_tool(
            Tool(
                name="comfyui.job.retry.commit",
                description=(
                    "Commit one unexpired owner-bound retry plan by exact digest "
                    "and create a new Job."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"repair_plan_id": repair_plan_id, "plan_digest": digest},
                    "required": ["repair_plan_id", "plan_digest"],
                    "additionalProperties": False,
                },
                output_schema=committed_plan_output,
                annotations=ToolAnnotations(
                    read_only_hint=False, destructive_hint=False, open_world_hint=False
                ),
            ),
            risk="medium",
            toolset="execution",
        ),
    ]
