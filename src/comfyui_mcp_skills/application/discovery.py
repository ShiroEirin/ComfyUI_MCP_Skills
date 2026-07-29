"""Read-only discovery of ComfyUI servers, nodes, and models."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from comfyui_mcp_skills.application.ports import ComfyUIGateway
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import WorkflowArgumentsError, WorkflowNotFound

GatewayFactory = Callable[[dict[str, Any]], ComfyUIGateway]


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
