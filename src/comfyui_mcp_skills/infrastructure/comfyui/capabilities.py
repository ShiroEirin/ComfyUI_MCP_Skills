"""ComfyUI server capability discovery operations."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from comfyui_mcp_skills.infrastructure.comfyui.client_protocol import SharedClient


class CapabilitiesClient(SharedClient):
    """Own node, model, template, and diagnostics discovery."""

    def get_object_info(self) -> dict[str, Any]:
        resp = self._get("/object_info")
        resp.raise_for_status()
        return resp.json()

    def get_object_info_node(self, node_class: str) -> dict[str, Any] | None:
        resp = self._get(f"/object_info/{quote(node_class, safe='')}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        return data.get(node_class)

    def get_model_folders(self) -> list[str]:
        resp = self._get("/models")
        resp.raise_for_status()
        return resp.json()

    def get_models(self, folder: str) -> list[str]:
        resp = self._get(f"/models/{quote(folder, safe='')}")
        resp.raise_for_status()
        return resp.json()

    def get_node_replacements(self) -> dict[str, str]:
        resp = self._get("/node_replacements")
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        return resp.json()

    def get_logs(self) -> dict[str, Any]:
        resp = self._get("/internal/logs/raw")
        resp.raise_for_status()
        return resp.json()

    def get_subgraphs(self) -> dict[str, Any]:
        resp = self._get("/global_subgraphs")
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        return resp.json()

    def get_workflow_templates(self) -> dict[str, Any]:
        resp = self._get("/workflow_templates")
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        return resp.json()
