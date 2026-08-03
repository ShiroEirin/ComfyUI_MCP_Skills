"""Central G2 scope, Toolset, and principal authorization contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum


class Scope(str, Enum):
    EXECUTE = "comfyui:execute"
    OBSERVE = "comfyui:observe"
    AUTHOR = "comfyui:author"
    OPERATE = "comfyui:operate"
    CONFIGURE = "comfyui:configure"
    PROVISION = "comfyui:provision"
    AUDIT = "comfyui:audit"


class Toolset(str, Enum):
    EXECUTION = "execution"
    AUTHORING = "authoring"
    OPERATIONS = "operations"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class AuthorizationContext:
    principal_id: str
    scopes: frozenset[Scope]
    toolset: Toolset


_TOOLSET_SCOPES: dict[Toolset, frozenset[Scope]] = {
    Toolset.EXECUTION: frozenset({Scope.EXECUTE}),
    Toolset.AUTHORING: frozenset({Scope.OBSERVE, Scope.AUTHOR}),
    Toolset.OPERATIONS: frozenset({Scope.OBSERVE, Scope.OPERATE}),
    Toolset.ADMIN: frozenset({Scope.CONFIGURE, Scope.PROVISION, Scope.AUDIT}),
}
_TOOL_SCOPES: dict[str, frozenset[Scope]] = {
    "comfyui.capability.search": frozenset(Scope),
    "comfyui.capability.describe": frozenset(Scope),
    "comfyui.asset.upload": frozenset({Scope.EXECUTE}),
    "comfyui.asset.list": frozenset({Scope.EXECUTE}),
    "comfyui.asset.describe": frozenset({Scope.EXECUTE}),
    "comfyui.asset.collection.update": frozenset({Scope.EXECUTE}),
    "comfyui.asset.metadata.extract": frozenset({Scope.EXECUTE}),
    "comfyui.asset.import_output": frozenset({Scope.EXECUTE}),
    "comfyui.asset.delete.plan": frozenset({Scope.EXECUTE}),
    "comfyui.asset.delete.commit": frozenset({Scope.EXECUTE}),
    "comfyui.asset.transfer.plan": frozenset({Scope.EXECUTE}),
    "comfyui.asset.transfer.commit": frozenset({Scope.EXECUTE}),
    "comfyui.asset.transfer.get": frozenset({Scope.EXECUTE}),
    "comfyui.job.get": frozenset({Scope.EXECUTE}),
    "comfyui.job.cancel": frozenset({Scope.EXECUTE}),
    "comfyui.job.list": frozenset({Scope.EXECUTE}),
    "comfyui.server.list": frozenset({Scope.OBSERVE}),
    "comfyui.server.health": frozenset({Scope.OBSERVE}),
    "comfyui.queue.list": frozenset({Scope.OBSERVE}),
    "comfyui.log.read": frozenset({Scope.OBSERVE}),
    "comfyui.server.capabilities": frozenset({Scope.OBSERVE}),
    "comfyui.template.list": frozenset({Scope.OBSERVE}),
    "comfyui.subgraph.list": frozenset({Scope.OBSERVE}),
    "comfyui.subgraph.get": frozenset({Scope.OBSERVE}),
    "comfyui.server.free": frozenset({Scope.OPERATE}),
    "comfyui.node.list": frozenset({Scope.OBSERVE}),
    "comfyui.node.describe": frozenset({Scope.OBSERVE}),
    "comfyui.model.list": frozenset({Scope.OBSERVE}),
    "comfyui.revision.list": frozenset({Scope.OBSERVE, Scope.AUTHOR}),
    "comfyui.revision.diff": frozenset({Scope.OBSERVE, Scope.AUTHOR}),
    "comfyui.workflow.describe": frozenset({Scope.OBSERVE, Scope.AUTHOR}),
    "comfyui.workflow.dependencies.check": frozenset({Scope.OBSERVE, Scope.AUTHOR}),
}
_RESOURCE_SCOPES: dict[str, frozenset[Scope]] = {
    "workflow": frozenset({Scope.EXECUTE, Scope.OBSERVE, Scope.AUTHOR}),
    "revision": frozenset({Scope.OBSERVE, Scope.AUTHOR}),
    "deployment": frozenset({Scope.OBSERVE, Scope.AUTHOR}),
    "asset": frozenset({Scope.EXECUTE}),
    "job": frozenset({Scope.EXECUTE}),
    "output": frozenset({Scope.EXECUTE}),
    "artifact": frozenset({Scope.EXECUTE}),
    "lineage": frozenset({Scope.EXECUTE}),
    "workflows": frozenset({Scope.EXECUTE, Scope.OBSERVE, Scope.AUTHOR}),
    "revisions": frozenset({Scope.OBSERVE, Scope.AUTHOR}),
    "deployments": frozenset({Scope.OBSERVE, Scope.AUTHOR}),
    "assets": frozenset({Scope.EXECUTE}),
    "jobs": frozenset({Scope.EXECUTE}),
    "outputs": frozenset({Scope.EXECUTE}),
    "artifacts": frozenset({Scope.EXECUTE}),
    "lineages": frozenset({Scope.EXECUTE}),
}
_PRINCIPAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def all_scope_values() -> frozenset[str]:
    return frozenset(scope.value for scope in Scope)


def admitted_scopes(toolset: Toolset) -> frozenset[Scope]:
    return _TOOLSET_SCOPES[toolset]


def scopes_for_tool(name: str, *, dynamic: bool = False) -> frozenset[Scope]:
    if dynamic:
        return frozenset({Scope.EXECUTE})
    return _TOOL_SCOPES.get(name, frozenset())


def scopes_for_resource(kind: str) -> frozenset[Scope]:
    return _RESOURCE_SCOPES.get(kind, frozenset())


def is_authorized(granted: frozenset[Scope], required_any: frozenset[Scope]) -> bool:
    return bool(granted & required_any)


def tool_visible(
    name: str,
    toolset: Toolset,
    granted: frozenset[Scope],
    *,
    dynamic: bool = False,
) -> bool:
    required = scopes_for_tool(name, dynamic=dynamic)
    return (
        bool(required)
        and is_authorized(granted, required)
        and is_authorized(admitted_scopes(toolset), required)
    )


def parse_scopes(value: str) -> frozenset[Scope]:
    raw = [item.strip() for item in value.split(",") if item.strip()]
    if not raw:
        raise ValueError("scopes must contain at least one value")
    try:
        parsed = tuple(Scope(item) for item in raw)
    except ValueError as exc:
        raise ValueError("unknown scope") from exc
    if len(parsed) != len(set(parsed)):
        raise ValueError("scopes must not contain duplicates")
    return frozenset(parsed)


def authorization_for_stdio(environment: Mapping[str, str]) -> AuthorizationContext:
    principal = environment.get("COMFYUI_MCP_PRINCIPAL_ID", "").strip()
    scopes_value = environment.get("COMFYUI_MCP_SCOPES", "").strip()
    toolset_value = environment.get("COMFYUI_MCP_TOOLSET", "").strip().lower()
    explicit = bool(principal or scopes_value or toolset_value)
    if not explicit:
        return AuthorizationContext("local-stdio", frozenset({Scope.EXECUTE}), Toolset.EXECUTION)
    if not principal or _PRINCIPAL.fullmatch(principal) is None:
        raise ValueError("COMFYUI_MCP_PRINCIPAL_ID must be a safe identifier")
    if not scopes_value or not toolset_value:
        raise ValueError("stdio principal, scopes, and toolset must be configured together")
    try:
        toolset = Toolset(toolset_value)
    except ValueError as exc:
        raise ValueError("unknown MCP toolset") from exc
    scopes = parse_scopes(scopes_value)
    if not scopes <= admitted_scopes(toolset):
        raise PermissionError("configured scope does not admit the selected Toolset")
    if (
        toolset is not Toolset.EXECUTION
        and environment.get("COMFYUI_MCP_ENABLE_HIGH_RISK", "") != "1"
    ):
        raise PermissionError("high-risk stdio Toolset requires explicit enablement")
    return AuthorizationContext(principal, scopes, toolset)
