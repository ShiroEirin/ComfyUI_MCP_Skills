"""ComfyUI server capability discovery operations."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests

from comfyui_mcp_skills.infrastructure.comfyui.client_protocol import SharedClient

CAPABILITY_STATES = frozenset(
    {"supported", "unsupported", "unauthorized", "temporarily_unavailable"}
)
_MAX_OBSERVATION_RESPONSE_BYTES = 4 * 1024 * 1024
_PROBE_TIMEOUT_SECONDS = 2.0


def classify_capability_status(status_code: int) -> str:
    """Classify endpoint availability without conflating optional failures with health."""
    if status_code == 404:
        return "unsupported"
    if status_code in {401, 403}:
        return "unauthorized"
    if status_code <= 0 or status_code >= 500 or status_code in {408, 425, 429}:
        return "temporarily_unavailable"
    return "supported"


def _fallback_capability(preferred: dict[str, str], fallback: dict[str, str]) -> dict[str, str]:
    if preferred["state"] == "supported":
        return {"state": "supported", "variant": "v2"}
    if fallback["state"] == "supported":
        return {"state": "supported", "variant": "traditional"}
    states = {preferred["state"], fallback["state"]}
    if "unauthorized" in states:
        state = "unauthorized"
    elif "temporarily_unavailable" in states:
        state = "temporarily_unavailable"
    else:
        state = "unsupported"
    return {"state": state, "variant": ""}


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

    def get_node_replacements(self) -> dict[str, Any]:
        resp = self._get("/node_replacements")
        resp.raise_for_status()
        return resp.json()

    def _read_bounded_json(self, path: str) -> Any:
        return self._get_json_bounded(path, max_bytes=_MAX_OBSERVATION_RESPONSE_BYTES)

    def get_logs(self) -> dict[str, Any]:
        data = self._read_bounded_json("/internal/logs/raw")
        if not isinstance(data, dict):
            raise ValueError("ComfyUI logs response is invalid")
        return data

    def get_subgraphs(self) -> dict[str, Any]:
        data = self._read_bounded_json("/global_subgraphs")
        if not isinstance(data, dict):
            raise ValueError("ComfyUI subgraphs response is invalid")
        return data

    def get_subgraph(self, subgraph_id: str) -> dict[str, Any] | None:
        data = self._read_bounded_json(f"/global_subgraphs/{quote(subgraph_id, safe='')}")
        return data if isinstance(data, dict) else None

    def get_workflow_templates(self) -> Any:
        return self._read_bounded_json("/workflow_templates")

    def probe_capabilities(self) -> dict[str, dict[str, str]]:
        """Probe optional routes independently using read-only requests only."""
        jobs_api = self._probe("/api/jobs", params={"limit": 1, "offset": 0})
        userdata_v2 = self._probe("/v2/userdata", params={"path": "workflows"})
        userdata_traditional = self._probe("/userdata", params={"dir": "workflows"})
        capabilities = {
            "jobs_api": jobs_api,
            "userdata_v2": userdata_v2,
            "userdata_traditional": userdata_traditional,
            "userdata": _fallback_capability(userdata_v2, userdata_traditional),
            "node_replacements": self._probe("/node_replacements"),
            "manager_queue_status": self._probe("/manager/queue/status"),
            # GET is deliberately used as a non-mutating route-presence probe. A
            # 405 response proves that the POST-only install route is registered.
            "manager_install": self._probe("/manager/queue/install"),
            "logs": self._probe("/internal/logs/raw"),
            "workflow_templates": self._probe("/workflow_templates"),
            "subgraphs": self._probe("/global_subgraphs"),
        }
        return capabilities

    def _probe(self, path: str, **kwargs: Any) -> dict[str, str]:
        response: requests.Response | None = None
        try:
            response = self._get(
                path,
                timeout=min(self.timeout, _PROBE_TIMEOUT_SECONDS),
                stream=True,
                allow_redirects=False,
                **kwargs,
            )
            status_code = response.status_code
        except requests.RequestException:
            return {"state": "temporarily_unavailable"}
        finally:
            if response is not None:
                response.close()
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            status_code = 0
        return {"state": classify_capability_status(status_code)}
