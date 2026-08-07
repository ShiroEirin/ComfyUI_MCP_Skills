"""Read-only discovery of ComfyUI servers, nodes, and models."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from comfyui_mcp_skills.application.ports import ComfyUIGateway
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import WorkflowArgumentsError, WorkflowNotFound

GatewayFactory = Callable[[dict[str, Any]], ComfyUIGateway]

_BLUEPRINT_MAX_FIELDS = 8
_BLUEPRINT_MAX_ENUM_VALUES = 8
_BLUEPRINT_MAX_OUTPUTS = 4
_BLUEPRINT_MAX_FIELD_VALUE_CHARS = 64


def _compact_type(value: object) -> str:
    """Render one object_info field type slot compactly."""
    if isinstance(value, list):
        return ", ".join(_compact_type(item) for item in value[:4])
    return str(value)


def _advertised_options(value: object) -> list[str]:
    """Extract advertised enum options from a field type slot, bounded."""
    options: list[str] = []
    raw_options: object = value
    if isinstance(value, dict):
        raw_options = value.get("options", [])
    if isinstance(raw_options, list):
        for option in raw_options:
            if len(options) >= _BLUEPRINT_MAX_ENUM_VALUES:
                break
            if isinstance(option, str) and option:
                options.append(option[:_BLUEPRINT_MAX_FIELD_VALUE_CHARS])
    return options


def _blueprint_field(name: str, spec: list[Any], *, required: bool) -> dict[str, Any]:
    """Project one input field: type plus bounded advertised options.

    object_info spells enums both as a bare list (``[["a", "b"]]``) and as
    ``["COMBO", {"options": [...]}]``; both are projected as ``options``.
    """
    raw_type = spec[0] if spec else None
    field: dict[str, Any] = {"name": name, "type": "unknown", "required": required}
    options: list[str] = []
    if isinstance(raw_type, list):
        field["type"] = "COMBO"
        options = _advertised_options(raw_type)
    elif isinstance(raw_type, str):
        field["type"] = raw_type
        if len(spec) > 1:
            options = _advertised_options(spec[1])
    else:
        field["type"] = _compact_type(raw_type)
        if len(spec) > 1:
            options = _advertised_options(spec[1])
    if options:
        field["options"] = options
    return field


def _blueprint_item(node_class: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """Compact node signature: bounded input fields with types and options."""
    fields: list[dict[str, Any]] = []
    input_spec = metadata.get("input")
    input_order = metadata.get("input_order")
    ordered_required: list[str] = []
    if isinstance(input_order, dict):
        raw_required = input_order.get("required")
        if isinstance(raw_required, list):
            ordered_required = [str(name) for name in raw_required[: _BLUEPRINT_MAX_FIELDS]]
    seen: set[str] = set()
    for name in ordered_required:
        if name in seen or len(fields) >= _BLUEPRINT_MAX_FIELDS:
            continue
        seen.add(name)
        spec = input_spec.get("required", {}).get(name) if isinstance(input_spec, dict) else None
        if not isinstance(spec, list) or not spec:
            continue
        fields.append(_blueprint_field(name, spec, required=True))
    if isinstance(input_spec, dict):
        optional = input_spec.get("optional")
        if isinstance(optional, dict):
            for name in sorted(optional, key=str):
                if name in seen or len(fields) >= _BLUEPRINT_MAX_FIELDS:
                    continue
                seen.add(name)
                spec = optional[name]
                if not isinstance(spec, list) or not spec:
                    continue
                fields.append(_blueprint_field(str(name), spec, required=False))
    outputs = metadata.get("output")
    output_types: list[str] = []
    if isinstance(outputs, list):
        for output in outputs[: _BLUEPRINT_MAX_OUTPUTS]:
            if isinstance(output, str):
                output_types.append(output)
    item: dict[str, Any] = {
        "class_type": node_class,
        "display_name": str(metadata.get("display_name", node_class)),
        "category": str(metadata.get("category", "")),
        "inputs": fields,
    }
    if output_types:
        item["outputs"] = output_types
    return item


class DiscoveryService:
    def __init__(self, servers: ServerRegistry, gateway_factory: GatewayFactory) -> None:
        self._servers = servers
        self._gateway_factory = gateway_factory

    def servers(self) -> dict[str, Any]:
        return {
            "items": [
                {
                    "server_id": server.server_id,
                    "name": server.name,
                    "enabled": server.enabled,
                }
                for server in self._servers.list()
            ]
        }

    def health(self, server_id: str) -> dict[str, Any]:
        server = self._servers.get(server_id)
        gateway = self._gateway_factory(self._servers.connection(server_id))
        return {
            "server_id": server.server_id,
            "name": server.name,
            "status": "online",
            "stats": gateway.get_system_stats(),
            "cancel_running_supported": False,
        }

    def nodes(
        self,
        server_id: str,
        *,
        query: str = "",
        limit: int = 50,
        cursor: str = "",
    ) -> dict[str, Any]:
        gateway = self._gateway_factory(self._servers.connection(server_id))
        raw = gateway.get_object_info()
        items = [
            {
                "class": node_class,
                "display_name": str(metadata.get("display_name", node_class)),
                "category": str(metadata.get("category", "")),
            }
            for node_class, metadata in raw.items()
            if isinstance(node_class, str) and isinstance(metadata, dict)
        ]
        items.sort(key=lambda item: (item["class"].casefold(), item["class"]))
        return self._page(items, query=query, limit=limit, cursor=cursor, key="class")

    def node(self, server_id: str, node_class: str) -> dict[str, Any]:
        gateway = self._gateway_factory(self._servers.connection(server_id))
        node = gateway.get_object_info_node(node_class)
        if node is None:
            raise WorkflowNotFound(f"ComfyUI node not found: {node_class}")
        return {"server_id": server_id, "node_class": node_class, "node": node}

    def blueprint(
        self,
        server_id: str,
        *,
        query: str,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Project a goal-driven compact node blueprint from object_info.

        Matches nodes by keyword overlap on class/display_name/category and
        returns bounded compact signatures: at most ``limit`` nodes, each with
        up to _BLUEPRINT_MAX_FIELDS inputs whose names, types and (truncated)
        advertised options fit a token budget an agent can actually consume.
        """
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if len(query) > 256:
            raise ValueError("query must be at most 256 characters")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
            raise ValueError("limit must be an integer between 1 and 10")
        terms = [term.casefold() for term in re.split(r"[\s,]+", query.strip()) if term]
        if not terms:
            raise ValueError("query must contain at least one keyword")
        gateway = self._gateway_factory(self._servers.connection(server_id))
        raw = gateway.get_object_info()
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for node_class, metadata in raw.items():
            if not isinstance(node_class, str) or not isinstance(metadata, dict):
                continue
            display_name = str(metadata.get("display_name", node_class)).casefold()
            category = str(metadata.get("category", "")).casefold()
            class_name = node_class.casefold()
            score = 0
            for term in terms:
                if term in display_name:
                    score += 3
                elif term in category:
                    score += 2
                elif term in class_name:
                    score += 1
            if score:
                scored.append((score, node_class, metadata))
        scored.sort(key=lambda entry: (-entry[0], entry[1].casefold(), entry[1]))
        return {
            "server_id": server_id,
            "query": query,
            "items": [
                _blueprint_item(node_class, metadata)
                for _score, node_class, metadata in scored[:limit]
            ],
            "total_matches": len(scored),
        }

    def models(
        self,
        server_id: str,
        *,
        kind: str = "",
        query: str = "",
        limit: int = 50,
        cursor: str = "",
    ) -> dict[str, Any]:
        gateway = self._gateway_factory(self._servers.connection(server_id))
        if not kind:
            values = sorted(str(value) for value in gateway.get_model_folders())
            page = self._page(values, query=query, limit=limit, cursor=cursor)
            page["kind"] = ""
            return page
        values = sorted(str(value) for value in gateway.get_models(kind))
        page = self._page(values, query=query, limit=limit, cursor=cursor)
        page["kind"] = kind
        return page

    @staticmethod
    def _page(
        items: list[Any],
        *,
        query: str,
        limit: int,
        cursor: str,
        key: str = "",
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise WorkflowArgumentsError("limit must be an integer between 1 and 200")
        try:
            offset = int(cursor or "0")
        except ValueError as exc:
            raise WorkflowArgumentsError("cursor must be a non-negative integer") from exc
        if offset < 0:
            raise WorkflowArgumentsError("cursor must be a non-negative integer")
        needle = query.casefold()
        if needle:
            items = [
                item
                for item in items
                if needle
                in (
                    str(item.get(key, "")) if key and isinstance(item, dict) else str(item)
                ).casefold()
            ]
        page = items[offset : offset + limit]
        next_offset = offset + len(page)
        return {
            "items": page,
            "next_cursor": str(next_offset) if next_offset < len(items) else "",
            "total": len(items),
        }
