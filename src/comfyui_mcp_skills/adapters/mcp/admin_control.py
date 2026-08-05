"""Phase O MCP projections, bounded tool schemas, and private resources."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import anyio
from mcp.server import ServerRequestContext
from mcp.server.subscriptions import SubscriptionBus
from mcp.shared.exceptions import MCPError
from mcp.shared.subscriptions import ResourceUpdated
from mcp.types import (
    ListResourcesResult,
    ListResourceTemplatesResult,
    PaginatedRequestParams,
    ReadResourceRequestParams,
    ReadResourceResult,
    ResourceTemplate,
    TextResourceContents,
    Tool,
    ToolAnnotations,
)
from mcp_types import INVALID_PARAMS

from comfyui_mcp_skills.adapters.mcp.tooling import decorate_tool
from comfyui_mcp_skills.application.authorization import (
    Scope,
    is_authorized,
    scopes_for_resource,
)

logger = logging.getLogger(__name__)

PHASE_O_TOOL_NAMES = frozenset(
    {
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
    }
)
IDENT = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
    "pattern": r"^(?!.*[\r\n])[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
}
DIGEST = {"type": "string", "minLength": 64, "maxLength": 64, "pattern": r"^[0-9a-f]{64}$"}
URI = {"type": "string", "minLength": 1, "maxLength": 2048}
TIME = {"type": "string", "maxLength": 64}
SERVER_FIELDS = {
    "server_id": IDENT,
    "resource_uri": URI,
    "display_name": {"type": "string", "maxLength": 256},
    "endpoint_url": URI,
    "enabled": {"type": "boolean"},
    "is_default": {"type": "boolean"},
    "status": {"type": "string", "maxLength": 64},
    "health": {"type": "string", "maxLength": 64},
    "generation": {"type": "integer", "minimum": 0},
    "revision": {"type": "integer", "minimum": 1},
    "config_digest": DIGEST,
    "created_at": TIME,
    "updated_at": TIME,
}
PLAN_FIELDS = {
    "plan_id": IDENT,
    "plan_digest": DIGEST,
    "resource_uri": URI,
    "operation": {"type": "string", "maxLength": 64},
    "server_id": IDENT,
    "base_revision": {"type": "string", "maxLength": 128},
    "target_revision": {"type": "string", "maxLength": 128},
    "server_revision": {"type": "integer", "minimum": 1},
    "server_config_digest": DIGEST,
    "expected_revision": {"type": "integer", "minimum": 0},
    "approval_id": IDENT,
    "status": {"type": "string", "maxLength": 64},
    "restart_required": {"type": "boolean"},
    "change_fields": {
        "type": "array",
        "maxItems": 32,
        "items": {"type": "string", "maxLength": 128},
    },
    "summary": {"type": "string", "maxLength": 2048},
    "created_at": TIME,
    "expires_at": TIME,
}
APPROVAL_FIELDS = {
    "approval_id": IDENT,
    "resource_uri": URI,
    "plan_id": IDENT,
    "plan_digest": DIGEST,
    "status": {"type": "string", "maxLength": 64},
    "decision": {"type": "string", "maxLength": 64},
    "risk": {"type": "string", "maxLength": 64},
    "summary": {"type": "string", "maxLength": 2048},
    "requested_at": TIME,
    "decided_at": TIME,
    "expires_at": TIME,
}
JOB_FIELDS = {
    "job_id": IDENT,
    "resource_uri": URI,
    "plan_id": IDENT,
    "approval_id": IDENT,
    "server_id": IDENT,
    "status": {"type": "string", "maxLength": 64},
    "stage": {"type": "string", "maxLength": 128},
    "progress": {"type": "number", "minimum": 0, "maximum": 1},
    "attempts": {"type": "integer", "minimum": 0, "maximum": 1000},
    "restart_required": {"type": "boolean"},
    "error_code": {"type": "string", "maxLength": 128},
    "created_at": TIME,
    "updated_at": TIME,
    "completed_at": TIME,
}
REQ_FIELDS = {
    "dependency_id": {"type": "string", "maxLength": 256},
    "kind": {"type": "string", "enum": ["node", "model"]},
    "name": {"type": "string", "maxLength": 256},
    "source_type": {"type": "string", "enum": ["git", "model"]},
    "source_url": URI,
    "version": {"type": "string", "maxLength": 128},
    "license": {"type": "string", "maxLength": 128},
    "checksum": DIGEST,
    "size_bytes": {"type": "integer", "minimum": 1},
    "target_dir": {"type": "string", "maxLength": 256},
    "restart_required": {"type": "boolean"},
    "install_state": {"type": "string", "maxLength": 64},
}
REQ_INPUT = {
    "type": "object",
    "properties": {
        "dependency_id": {"type": "string", "maxLength": 256},
        "kind": {"type": "string", "enum": ["node", "model"]},
        "name": {"type": "string", "minLength": 1, "maxLength": 256},
        "source_url": URI,
        "version": {"type": "string", "minLength": 1, "maxLength": 128},
        "checksum": DIGEST,
        "size_bytes": {"type": "integer", "minimum": 1, "maximum": 21474836480},
    },
    "required": ["kind", "name"],
    "additionalProperties": False,
}
SERVER_INPUT = {
    "type": "object",
    "properties": {
        "display_name": {"type": "string", "maxLength": 256},
        "endpoint_url": URI,
        "enabled": {"type": "boolean"},
        "expected_revision": {"type": "integer", "minimum": 0},
        "secret_refs": {
            "type": "object",
            "maxProperties": 16,
            "additionalProperties": {"type": "string", "maxLength": 256},
        },
    },
    "maxProperties": 5,
    "additionalProperties": False,
}
BUNDLE_SERVER = {
    "type": "object",
    "properties": {
        "server_id": IDENT,
        "display_name": {"type": "string", "maxLength": 256},
        "endpoint_url": URI,
        "enabled": {"type": "boolean"},
        "is_default": {"type": "boolean"},
        "secret_refs": {
            "type": "object",
            "maxProperties": 16,
            "additionalProperties": {"type": "string", "maxLength": 256},
        },
    },
    "required": ["server_id", "endpoint_url", "enabled", "is_default"],
    "additionalProperties": False,
}
BUNDLE_WORKFLOW = {
    "type": "object",
    "properties": {"server_id": IDENT, "workflow_id": IDENT, "enabled": {"type": "boolean"}},
    "required": ["server_id", "workflow_id", "enabled"],
    "additionalProperties": False,
}
BUNDLE_INPUT = {
    "type": "object",
    "properties": {
        "format_version": {"type": "integer", "minimum": 1, "maximum": 1},
        "revision": {"type": "string", "maxLength": 128},
        "servers": {"type": "array", "maxItems": 64, "items": BUNDLE_SERVER},
        "workflows": {"type": "array", "maxItems": 256, "items": BUNDLE_WORKFLOW},
        "default_server": {"anyOf": [IDENT, {"type": "null"}]},
        "bundle_digest": DIGEST,
        "resource_uri": URI,
        "created_at": TIME,
    },
    "required": [
        "format_version",
        "revision",
        "servers",
        "workflows",
        "default_server",
        "bundle_digest",
        "resource_uri",
        "created_at",
    ],
    "additionalProperties": False,
}


def obj(p: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": p, "additionalProperties": False}


def phase_o_tools(
    *, servers_available: bool, config_available: bool, dependencies_available: bool
) -> list[Tool]:
    ts: list[Tool] = []
    if servers_available:
        ts += [
            Tool(
                name="comfyui.admin.server.list",
                description="List bounded owner-visible servers.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                        "cursor": {"type": "string", "maxLength": 256, "default": ""},
                    },
                    "additionalProperties": False,
                },
                output_schema=obj(
                    {
                        "items": {"type": "array", "maxItems": 100, "items": obj(SERVER_FIELDS)},
                        "next_cursor": {"type": "string", "maxLength": 256},
                    }
                ),
                annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
            ),
            Tool(
                name="comfyui.admin.server.inspect",
                description="Inspect one owner-bound server.",
                input_schema={
                    "type": "object",
                    "properties": {"server_id": IDENT},
                    "required": ["server_id"],
                    "additionalProperties": False,
                },
                output_schema=obj(SERVER_FIELDS),
                annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
            ),
            *(
                Tool(
                    name=f"comfyui.admin.server.{operation}",
                    description=f"Plan or commit an owner-bound server {operation} operation.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "phase": {"type": "string", "enum": ["plan", "commit"]},
                            "server_id": IDENT,
                            "changes": SERVER_INPUT,
                            "expected_revision": {"type": "integer", "minimum": 0},
                            "plan_id": IDENT,
                            "plan_digest": DIGEST,
                        },
                        "required": ["phase"],
                        "oneOf": [
                            {
                                "properties": {"phase": {"const": "plan"}},
                                "required": ["phase", "server_id"],
                            },
                            {
                                "properties": {"phase": {"const": "commit"}},
                                "required": ["phase", "plan_id", "plan_digest"],
                            },
                        ],
                        "additionalProperties": False,
                    },
                    output_schema=obj({**PLAN_FIELDS, **SERVER_FIELDS}),
                    annotations=ToolAnnotations(
                        read_only_hint=False,
                        destructive_hint=operation == "delete",
                        idempotent_hint=True,
                        open_world_hint=False,
                    ),
                )
                for operation in ("upsert", "set_enabled", "set_default", "delete")
            ),
        ]
    if config_available:
        bundle = obj(
            {
                "format_version": {"type": "integer", "minimum": 1},
                "revision": {"type": "string", "maxLength": 128},
                "resource_uri": URI,
                "bundle_digest": DIGEST,
                "servers": {"type": "array", "maxItems": 64, "items": BUNDLE_SERVER},
                "workflows": {"type": "array", "maxItems": 256, "items": BUNDLE_WORKFLOW},
                "created_at": TIME,
                "default_server": {"anyOf": [IDENT, {"type": "null"}]},
            }
        )
        ts += [
            Tool(
                name="comfyui.admin.config.export",
                description="Export a bounded secret-free config Bundle.",
                input_schema={
                    "type": "object",
                    "properties": {"revision": {"type": "string", "maxLength": 128, "default": ""}},
                    "additionalProperties": False,
                },
                output_schema=bundle,
                annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
            ),
            Tool(
                name="comfyui.admin.config.import",
                description="Plan or commit a secret-free revision-fenced Config Bundle import.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "phase": {"type": "string", "enum": ["plan", "commit"]},
                        "bundle": BUNDLE_INPUT,
                        "expected_revision": {"type": "string", "maxLength": 128},
                        "plan_id": IDENT,
                        "plan_digest": DIGEST,
                    },
                    "required": ["phase"],
                    "oneOf": [
                        {
                            "properties": {"phase": {"const": "plan"}},
                            "required": ["phase", "bundle", "expected_revision"],
                        },
                        {
                            "properties": {"phase": {"const": "commit"}},
                            "required": ["phase", "plan_id", "plan_digest"],
                        },
                    ],
                    "additionalProperties": False,
                },
                output_schema=obj({**PLAN_FIELDS, **bundle["properties"]}),
                annotations=ToolAnnotations(
                    read_only_hint=False,
                    destructive_hint=True,
                    idempotent_hint=True,
                    open_world_hint=False,
                ),
            ),
        ]
    if dependencies_available:
        requirement_array = {
            "type": "array",
            "maxItems": 100,
            "items": obj(REQ_FIELDS),
        }
        dep = obj(
            {
                "server_id": IDENT,
                "status": {"type": "string", "maxLength": 64},
                "requirements": requirement_array,
                "unresolved": {
                    "type": "array",
                    "maxItems": 100,
                    "items": obj(
                        {
                            "dependency_id": {"type": "string", "maxLength": 256},
                            "kind": {"type": "string", "enum": ["node", "model"]},
                            "name": {"type": "string", "maxLength": 256},
                        }
                    ),
                },
                "restart_required": {"type": "boolean"},
            }
        )
        dependency_plan = obj({**PLAN_FIELDS, **dep["properties"]})
        reqs = {"type": "array", "maxItems": 64, "items": REQ_INPUT}
        ts += [
            Tool(
                name="comfyui.admin.dependency.inspect",
                description="Inspect pinned dependency readiness without installing.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "server_id": IDENT,
                        "requirements": {**reqs, "minItems": 0},
                        "workflow_id": IDENT,
                        "revision_id": IDENT,
                    },
                    "required": ["server_id"],
                    "additionalProperties": False,
                },
                output_schema=dep,
                annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
            ),
            Tool(
                name="comfyui.admin.dependency.plan",
                description="Plan fixed-source checksum-verified dependency installation.",
                input_schema={
                    "type": "object",
                    "properties": {"server_id": IDENT, "requirements": {**reqs, "minItems": 1}},
                    "required": ["server_id", "requirements"],
                    "additionalProperties": False,
                },
                output_schema=dependency_plan,
                annotations=ToolAnnotations(
                    read_only_hint=False, destructive_hint=False, open_world_hint=True
                ),
            ),
            Tool(
                name="comfyui.admin.dependency.install",
                description=(
                    "Commit approved install with exact digest, approval, request, "
                    "and confirmation."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "plan_id": IDENT,
                        "plan_digest": DIGEST,
                        "approval_id": IDENT,
                        "request_id": IDENT,
                        "confirmation": {
                            "type": "string",
                            "const": "INSTALL APPROVED DEPENDENCIES",
                        },
                    },
                    "required": [
                        "plan_id",
                        "plan_digest",
                        "approval_id",
                        "request_id",
                        "confirmation",
                    ],
                    "additionalProperties": False,
                },
                output_schema=obj(JOB_FIELDS),
                annotations=ToolAnnotations(
                    read_only_hint=False,
                    destructive_hint=True,
                    idempotent_hint=True,
                    open_world_hint=True,
                ),
            ),
            Tool(
                name="comfyui.admin.approval.get",
                description="Read one owner-bound approval.",
                input_schema={
                    "type": "object",
                    "properties": {"approval_id": IDENT},
                    "required": ["approval_id"],
                    "additionalProperties": False,
                },
                output_schema=obj(APPROVAL_FIELDS),
                annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
            ),
            Tool(
                name="comfyui.admin.approval.decision.plan",
                description="Plan an approval decision without applying it.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "approval_id": IDENT,
                        "decision": {"type": "string", "enum": ["approved", "rejected"]},
                        "reason": {"type": "string", "maxLength": 512, "default": ""},
                    },
                    "required": ["approval_id", "decision"],
                    "additionalProperties": False,
                },
                output_schema=obj(PLAN_FIELDS),
                annotations=ToolAnnotations(
                    read_only_hint=False, destructive_hint=False, open_world_hint=False
                ),
            ),
            Tool(
                name="comfyui.admin.approval.decision.commit",
                description="Commit an exact digest-bound approval decision.",
                input_schema={
                    "type": "object",
                    "properties": {"plan_id": IDENT, "plan_digest": DIGEST},
                    "required": ["plan_id", "plan_digest"],
                    "additionalProperties": False,
                },
                output_schema=obj(APPROVAL_FIELDS),
                annotations=ToolAnnotations(
                    read_only_hint=False,
                    destructive_hint=True,
                    idempotent_hint=True,
                    open_world_hint=False,
                ),
            ),
            Tool(
                name="comfyui.admin.provisioning.get",
                description="Read a bounded owner provisioning Job.",
                input_schema={
                    "type": "object",
                    "properties": {"job_id": IDENT},
                    "required": ["job_id"],
                    "additionalProperties": False,
                },
                output_schema=obj(JOB_FIELDS),
                annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
            ),
        ]
        ts += [
            Tool(
                name="comfyui.admin.provisioning.cancel",
                description="Plan or commit digest-bound cancellation of a provisioning Job.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "phase": {"type": "string", "enum": ["plan", "commit"]},
                        "job_id": IDENT,
                        "plan_id": IDENT,
                        "plan_digest": DIGEST,
                    },
                    "required": ["phase"],
                    "additionalProperties": False,
                    "oneOf": [
                        {
                            "properties": {"phase": {"const": "plan"}},
                            "required": ["phase", "job_id"],
                        },
                        {
                            "properties": {"phase": {"const": "commit"}},
                            "required": ["phase", "plan_id", "plan_digest"],
                        },
                    ],
                },
                output_schema=obj({**PLAN_FIELDS, **JOB_FIELDS}),
                annotations=ToolAnnotations(
                    read_only_hint=False,
                    destructive_hint=True,
                    idempotent_hint=True,
                    open_world_hint=False,
                ),
            ),
        ]
    return [decorate_tool(t, toolset="admin") for t in ts]


def _safe_url(v: Any) -> str | None:
    if not isinstance(v, str) or len(v) > 2048:
        return None
    p = urlsplit(v)
    if (
        p.scheme not in {"http", "https"}
        or not p.hostname
        or p.username is not None
        or p.password is not None
        or p.query
        or p.fragment
    ):
        return None
    host = p.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return urlunsplit((p.scheme, host if p.port is None else f"{host}:{p.port}", p.path, "", ""))


def _bounded(v: dict[str, Any]) -> dict[str, Any]:
    if (
        len(json.dumps(v, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode())
        > 256 * 1024
    ):
        raise ValueError("Projected payload is too large")
    return v


def _scalars(v: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in fields:
        item = v.get(field)
        if isinstance(item, str) and len(item) <= 2048:
            result[field] = item
        elif isinstance(item, (bool, int, float)):
            result[field] = item
    return result


def server_dict(v: dict[str, Any]) -> dict[str, Any]:
    r = _scalars(v, set(SERVER_FIELDS) - {"endpoint_url"})
    u = _safe_url(v.get("endpoint_url", v.get("url")))
    if u:
        r["endpoint_url"] = u
    return _bounded(r)


def server_page_dict(v: dict[str, Any]) -> dict[str, Any]:
    r = {"items": [server_dict(x) for x in v.get("items", [])[:100] if isinstance(x, dict)]}
    if isinstance(v.get("next_cursor"), str):
        r["next_cursor"] = v["next_cursor"][:256]
    return _bounded(r)


def plan_dict(v: dict[str, Any]) -> dict[str, Any]:
    r = _scalars(v, set(PLAN_FIELDS) - {"change_fields"})
    if "plan_id" not in r:
        for field in ("approval_plan_id", "cancel_plan_id"):
            value = v.get(field)
            if isinstance(value, str) and len(value) <= 128:
                r["plan_id"] = value
                break
    f = v.get("change_fields")
    if not isinstance(f, list):
        f = sorted(v.get("changes", {})) if isinstance(v.get("changes"), dict) else []
    r["change_fields"] = [x for x in f[:32] if isinstance(x, str) and len(x) <= 128]
    return _bounded(r)


def _bundle_server_dict(v: dict[str, Any]) -> dict[str, Any]:
    result = _scalars(v, {"server_id", "display_name", "enabled", "is_default"})
    endpoint = _safe_url(v.get("endpoint_url", v.get("url")))
    if endpoint is not None:
        result["endpoint_url"] = endpoint
    refs = v.get("secret_refs")
    if isinstance(refs, dict):
        result["secret_refs"] = {
            key: value
            for key, value in list(refs.items())[:16]
            if isinstance(key, str)
            and len(key) <= 64
            and isinstance(value, str)
            and len(value) <= 256
        }
    return result


def config_bundle_dict(v: dict[str, Any]) -> dict[str, Any]:
    r = _scalars(v, {"format_version", "revision", "resource_uri", "bundle_digest", "created_at"})
    r["servers"] = [
        _bundle_server_dict(x) for x in v.get("servers", [])[:64] if isinstance(x, dict)
    ]
    r["workflows"] = [
        _scalars(x, {"server_id", "workflow_id", "enabled"})
        for x in v.get("workflows", [])[:256]
        if isinstance(x, dict)
    ]
    default_server = v.get("default_server")
    if default_server is None or isinstance(default_server, str):
        r["default_server"] = default_server
    return _bounded(r)


def dependency_report_dict(v: dict[str, Any]) -> dict[str, Any]:
    r = _scalars(v, {"server_id", "status", "restart_required"})
    out = []
    source = v.get("requirements", v.get("items", []))
    for item in source[:100] if isinstance(source, list) else []:
        if not isinstance(item, dict):
            continue
        projected = _scalars(item, set(REQ_FIELDS) - {"source_url"})
        url = _safe_url(item.get("source_url"))
        if url:
            projected["source_url"] = url
        if "name" not in projected and isinstance(projected.get("dependency_id"), str):
            projected["name"] = projected["dependency_id"].partition(":")[2]
        out.append(projected)
    r["requirements"] = out
    unresolved = []
    for item in v.get("unresolved", [])[:100]:
        if isinstance(item, dict):
            unresolved.append(_scalars(item, {"dependency_id", "kind", "name"}))
    r["unresolved"] = unresolved
    return _bounded(r)


def dependency_plan_dict(v: dict[str, Any]) -> dict[str, Any]:
    return _bounded({**plan_dict(v), **dependency_report_dict(v)})


def approval_dict(v: dict[str, Any]) -> dict[str, Any]:
    return _bounded(_scalars(v, set(APPROVAL_FIELDS)))


def provisioning_job_dict(v: dict[str, Any]) -> dict[str, Any]:
    return _bounded(_scalars(v, set(JOB_FIELDS)))


@dataclass(frozen=True, slots=True)
class AdminResourceHandlers:
    list_templates: Callable[..., Any]
    list_resources: Callable[..., Any]
    read_resource: Callable[..., Any]


def _resource_ref(uri: str) -> tuple[str, str]:
    p = urlsplit(uri)
    parts = [x for x in p.path.split("/") if x]
    if p.scheme != "comfyui" or p.query or p.fragment:
        raise ValueError("bad uri")
    if p.netloc == "servers" and len(parts) == 1:
        k, i = "server", parts[0]
    elif p.netloc == "config" and len(parts) == 2 and parts[0] == "bundles":
        k, i = "bundle", parts[1]
    elif p.netloc == "dependencies" and len(parts) == 2 and parts[0] == "plans":
        k, i = "plan", parts[1]
    elif p.netloc == "approvals" and len(parts) == 1:
        k, i = "approval", parts[0]
    elif p.netloc == "provisioning" and len(parts) == 2 and parts[0] == "jobs":
        k, i = "job", parts[1]
    else:
        raise ValueError("bad uri")
    if not 1 <= len(i) <= 128 or any(c in i for c in "\r\n"):
        raise ValueError("bad identity")
    return k, i


def create_admin_resource_handlers(
    repository: Any,
    owner_id: str,
    scopes: frozenset[Scope],
) -> AdminResourceHandlers:
    ts: tuple[ResourceTemplate, ...] = (
        ResourceTemplate(
            uri_template="comfyui://servers/{server_id}",
            name="Owned server",
            mime_type="application/json",
        ),
        ResourceTemplate(
            uri_template="comfyui://config/bundles/{revision}",
            name="Owned secret-free Config Bundle",
            mime_type="application/json",
        ),
        ResourceTemplate(
            uri_template="comfyui://dependencies/plans/{plan_id}",
            name="Owned dependency plan",
            mime_type="application/json",
        ),
        ResourceTemplate(
            uri_template="comfyui://approvals/{approval_id}",
            name="Owned approval",
            mime_type="application/json",
        ),
        ResourceTemplate(
            uri_template="comfyui://provisioning/jobs/{job_id}",
            name="Owned provisioning Job",
            mime_type="application/json",
        ),
    )
    template_kinds = (
        "admin_server",
        "admin_bundle",
        "admin_plan",
        "admin_approval",
        "admin_provisioning",
    )
    ts = tuple(
        template
        for template, kind in zip(ts, template_kinds, strict=True)
        if is_authorized(scopes, scopes_for_resource(kind))
    )

    async def lt(
        _c: ServerRequestContext[dict[str, object]], _p: PaginatedRequestParams | None
    ) -> ListResourceTemplatesResult:
        return ListResourceTemplatesResult(
            resource_templates=list(ts), ttl_ms=60000, cache_scope="private"
        )

    async def lr(
        _c: ServerRequestContext[dict[str, object]], _p: PaginatedRequestParams | None
    ) -> ListResourcesResult:
        return ListResourcesResult(resources=[], ttl_ms=5000, cache_scope="private")

    async def rr(
        _c: ServerRequestContext[dict[str, object]], p: ReadResourceRequestParams
    ) -> ReadResourceResult:
        uri = str(p.uri)
        try:
            k, i = _resource_ref(uri)
            resource_kind = {
                "server": "admin_server",
                "bundle": "admin_bundle",
                "plan": "admin_plan",
                "approval": "admin_approval",
                "job": "admin_provisioning",
            }[k]
            if not is_authorized(scopes, scopes_for_resource(resource_kind)):
                raise LookupError("missing")
            g, pr = {
                "server": (repository.get_server, server_dict),
                "bundle": (repository.get_bundle, config_bundle_dict),
                "plan": (repository.get_plan, dependency_plan_dict),
                "approval": (repository.get_approval, approval_dict),
                "job": (repository.get_job, provisioning_job_dict),
            }[k]
            d = await anyio.to_thread.run_sync(g, i, owner_id)
            if not isinstance(d, dict):
                raise LookupError("missing")
            x = pr(d)
            return ReadResourceResult(
                contents=[
                    TextResourceContents(
                        uri=uri,
                        mime_type="application/json",
                        text=json.dumps(x, ensure_ascii=False),
                    )
                ],
                ttl_ms=5000,
                cache_scope="private",
            )
        except MCPError:
            raise
        except (KeyError, LookupError, TypeError, ValueError) as e:
            raise MCPError(code=INVALID_PARAMS, message="Resource not found") from e

    return AdminResourceHandlers(lt, lr, rr)


class AdminOutboxRuntime:
    def __init__(
        self,
        repository: Any,
        bus: SubscriptionBus,
        *,
        owner_id: str,
        idle_seconds: float = 1.0,
    ) -> None:
        if idle_seconds <= 0:
            raise ValueError("idle_seconds must be positive")
        if not owner_id or len(owner_id) > 128:
            raise ValueError("owner_id must be a bounded string")
        self._r = repository
        self._b = bus
        self._owner_id = owner_id
        self._idle = idle_seconds

    async def run(self) -> None:
        while True:
            try:
                count = await self.dispatch_once()
            except Exception:
                logger.exception("Phase O outbox dispatch failed")
                count = 0
            if not count:
                await anyio.sleep(self._idle)

    async def dispatch_once(self) -> int:
        messages = await anyio.to_thread.run_sync(
            partial(self._r.pending_outbox, self._owner_id, limit=100)
        )
        delivered = 0
        for message in messages:
            uri = message.payload.get("uri")
            try:
                kind, _ = _resource_ref(uri) if isinstance(uri, str) else ("", "")
                owner_id = message.payload.get("owner_id")
                if kind not in {"server", "bundle", "plan", "approval", "job"} or (
                    owner_id != self._owner_id
                ):
                    raise ValueError("outbox resource ownership mismatch")
                await self._b.publish(ResourceUpdated(uri))
                await anyio.to_thread.run_sync(
                    partial(
                        self._r.mark_outbox_delivered,
                        message.outbox_id,
                        now=datetime.now(timezone.utc),
                    )
                )
                delivered += 1
            except Exception:
                logger.exception("Retaining failed Phase O outbox message for retry")
        return delivered
