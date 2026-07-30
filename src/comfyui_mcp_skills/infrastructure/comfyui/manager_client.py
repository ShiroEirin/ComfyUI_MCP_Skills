"""ComfyUI-Manager plugin operations."""

from __future__ import annotations

import time
from typing import Any

import requests

from comfyui_mcp_skills.infrastructure.comfyui.client_protocol import SharedClient


class ManagerClient(SharedClient):
    """Own interactions with the optional ComfyUI-Manager plugin."""

    def manager_start_queue(self) -> bool:
        try:
            resp = self._get("/manager/queue/start", timeout=10)
            return resp.status_code < 500
        except requests.RequestException:
            return False

    def manager_install_node(self, repo_url: str, pkg_name: str) -> dict[str, Any]:
        resp = self._post(
            "/manager/queue/install",
            json_data={
                "id": pkg_name,
                "url": repo_url,
                "install_type": "git-clone",
            },
        )
        if resp.status_code == 404:
            return {"success": False, "error": "ComfyUI Manager not installed"}
        if resp.status_code >= 400:
            return {"success": False, "error": f"Manager API error: {resp.status_code}"}
        return {"success": True}

    def manager_queue_status(self) -> dict[str, Any] | None:
        try:
            resp = self._get("/manager/queue/status", timeout=10)
            if resp.status_code != 200:
                return None
            return resp.json()
        except (requests.RequestException, ValueError):
            return None

    def manager_install_model(self, model_info: dict[str, str]) -> dict[str, Any]:
        resp = self._post("/manager/queue/install_model", json_data=model_info)
        if resp.status_code == 404:
            return {"success": False, "error": "ComfyUI Manager not installed"}
        if resp.status_code >= 400:
            return {"success": False, "error": f"Manager API error: {resp.status_code}"}
        return {"success": True}

    def manager_wait_for_queue(self, max_polls: int = 60, interval: float = 3.0) -> bool:
        for _ in range(max_polls):
            time.sleep(interval)
            status = self.manager_queue_status()
            if status is None:
                continue
            total = status.get("total", 0)
            done = status.get("done", 0)
            if total > 0 and done >= total:
                return True
        return False
