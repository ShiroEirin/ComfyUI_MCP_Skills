"""Read server configuration while keeping credentials inside the trust boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from comfyui_mcp_skills.domain.errors import ServerNotFound
from comfyui_mcp_skills.domain.identifiers import validate_identifier
from comfyui_mcp_skills.domain.models import Server


class ServerRegistry:
    def __init__(self, base_dir: Path) -> None:
        self._path = base_dir.resolve() / "config.json"

    def get(self, server_id: str) -> Server:
        config = self.connection(server_id)
        return Server(
            server_id=server_id,
            name=str(config.get("name", server_id)),
            url=str(config.get("url", "http://127.0.0.1:8188")),
            enabled=bool(config.get("enabled", True)),
            output_dir=str(config.get("output_dir", "./outputs")),
        )

    def list(self) -> list[Server]:
        result: list[Server] = []
        for item in self._load().get("servers", []):
            if not isinstance(item, dict) or item.get("enabled", True) is not True:
                continue
            server_id = validate_identifier(str(item.get("id", "")), field="server_id")
            result.append(
                Server(
                    server_id=server_id,
                    name=str(item.get("name", server_id)),
                    url=str(item.get("url", "http://127.0.0.1:8188")),
                    enabled=True,
                    output_dir=str(item.get("output_dir", "./outputs")),
                )
            )
        return result

    def default_server_id(self) -> str:
        data = self._load()
        return str(data.get("default_server", "local"))

    def connection(self, server_id: str) -> dict[str, Any]:
        server_id = validate_identifier(server_id, field="server_id")
        for server in self._load().get("servers", []):
            if isinstance(server, dict) and server.get("id") == server_id:
                if server.get("enabled", True) is not True:
                    raise ServerNotFound(f"Server is disabled: {server_id}")
                return dict(server)
        raise ServerNotFound(f"Server not found: {server_id}")

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ServerNotFound("Server configuration does not exist") from exc
        if not isinstance(data, dict):
            raise ServerNotFound("Server configuration is invalid")
        return data
