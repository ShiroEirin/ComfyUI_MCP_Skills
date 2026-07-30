"""ComfyUI userdata workflow operations."""

from __future__ import annotations

import urllib.parse
from typing import Any

import requests

from comfyui_mcp_skills.infrastructure.comfyui.client_protocol import SharedClient


class UserdataClient(SharedClient):
    """Own workflow discovery and reads through the userdata APIs."""

    def list_userdata_workflows(self) -> list[str]:
        # /v2/userdata uses "path" (not "dir") and returns a list of dicts
        # with a "path" key. /userdata uses "dir" and returns bare filenames.
        # Skip empty results so a working variant is always found.
        candidates = [
            ("/v2/userdata", {"path": "workflows"}),
            ("/userdata", {"dir": "workflows"}),
        ]
        for base, params in candidates:
            try:
                resp = self._get(base, params=params)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                paths: list[str] = []
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, str) and item.endswith(".json"):
                            # /userdata?dir= returns bare filenames; normalise
                            # to a full relative path for read_userdata_workflow.
                            paths.append(item if "/" in item else f"workflows/{item}")
                        elif isinstance(item, dict):
                            path = item.get("path") or item.get("name") or ""
                            if isinstance(path, str) and path.endswith(".json"):
                                paths.append(path)
                elif isinstance(data, dict) and "files" in data:
                    paths = []
                    for file_entry in data["files"]:
                        if not isinstance(file_entry, dict):
                            continue
                        candidate = file_entry.get("path") or file_entry.get("name")
                        if isinstance(candidate, str) and candidate.endswith(".json"):
                            paths.append(candidate)
                if paths:
                    return paths
            except (requests.RequestException, ValueError):
                continue
        return []

    def read_userdata_workflow(self, workflow_path: str) -> dict[str, Any] | None:
        # aiohttp matches /userdata/{file} as a single path segment. Percent-
        # encode the full relative path (including "/" separators) so it is
        # not split into multiple segments, which would return 404.
        encoded = urllib.parse.quote(workflow_path, safe="")
        try:
            resp = self._get(f"/userdata/{encoded}")
            if resp.status_code == 200:
                return resp.json()
        except (requests.RequestException, ValueError):
            pass
        return None
