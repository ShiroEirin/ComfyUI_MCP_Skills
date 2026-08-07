"""Prompt execution, queue, and job operations for ComfyUI."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Generator
from typing import Any

from comfyui_mcp_skills.infrastructure.comfyui.client_protocol import SharedClient

_MAX_QUEUE_RESPONSE_BYTES = 8 * 1024 * 1024


class JobsClient(SharedClient):
    """Own prompt execution, history, queue, and WebSocket behavior."""

    def queue_prompt(
        self,
        workflow: dict[str, Any],
        client_id: str | None = None,
        targets: list[str] | None = None,
        priority: float | None = None,
    ) -> dict[str, Any]:
        cid = client_id or str(uuid.uuid4())
        payload: dict[str, Any] = {
            "prompt": workflow,
            "client_id": cid,
        }
        if priority is not None:
            payload["number"] = priority
        if targets:
            # ComfyUI /prompt validates partial_execution_targets against its
            # output node ids as a flat list of strings; nested arrays never
            # match and the engine would reject the prompt as output-less.
            payload["partial_execution_targets"] = list(targets)
        if self.comfy_api_key:
            payload["extra_data"] = {"api_key_comfy_org": self.comfy_api_key}
        resp = self._post("/prompt", json_data=payload)
        resp.raise_for_status()
        data = resp.json()
        data["client_id"] = cid
        return data

    def get_history(
        self, prompt_id: str, *, timeout_seconds: float | None = None
    ) -> dict[str, Any] | None:
        resp = self._get(
            f"/history/{prompt_id}",
            timeout=self._query_timeout(timeout_seconds),
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data.get(prompt_id)

    def get_history_list(self, max_items: int = 20, offset: int = 0) -> dict[str, Any]:
        resp = self._get("/history", params={"max_items": max_items, "offset": offset})
        resp.raise_for_status()
        return resp.json()

    def get_jobs(
        self,
        status: str = "",
        limit: int = 20,
        offset: int = 0,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "sort_by": sort_by,
            "sort_order": sort_order,
        }
        if status:
            params["status"] = status
        resp = self._get("/api/jobs", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        resp = self._get(f"/api/jobs/{job_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def get_queue(self, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        data = self._get_json_bounded(
            "/queue",
            max_bytes=_MAX_QUEUE_RESPONSE_BYTES,
            timeout=self._query_timeout(timeout_seconds),
        )
        if not isinstance(data, dict):
            raise ValueError("ComfyUI queue response is invalid")
        return data

    def _query_timeout(self, timeout_seconds: float | None) -> float:
        if timeout_seconds is None:
            return self.timeout
        return min(self.timeout, max(timeout_seconds, 0.0))

    def ws_events(
        self,
        client_id: str,
        prompt_id: str,
        timeout_seconds: float | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        import websocket

        ws_url = self.server_url.replace("http://", "ws://").replace("https://", "wss://")
        deadline = (
            time.monotonic() + max(timeout_seconds, 0) if timeout_seconds is not None else None
        )
        connect_timeout = self.timeout
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            connect_timeout = min(self.timeout, remaining)
        ws = websocket.create_connection(
            f"{ws_url}/ws?clientId={client_id}",
            timeout=connect_timeout,
            header=self._headers(),
        )
        try:
            while True:
                if cancel_check is not None:
                    cancel_check()
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    ws.settimeout(min(self.timeout, remaining))
                else:
                    ws.settimeout(self.timeout)
                try:
                    opcode, raw = ws.recv_data()
                except websocket.WebSocketTimeoutException:
                    if cancel_check is not None:
                        cancel_check()
                    if deadline is not None:
                        continue
                    raise
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if opcode == websocket.ABNF.OPCODE_BINARY:
                    continue
                if opcode != websocket.ABNF.OPCODE_TEXT:
                    continue
                msg = json.loads(raw)
                event_type = msg.get("type", "")
                data = msg.get("data", {})
                if data.get("prompt_id") and data["prompt_id"] != prompt_id:
                    continue
                yield {"type": event_type, "data": data}
                if event_type in ("execution_error", "execution_interrupted"):
                    break
                if event_type == "executing" and data.get("node") is None:
                    break
        finally:
            ws.close()

    def interrupt(self, prompt_id: str = "") -> dict[str, Any]:
        payload = {"prompt_id": prompt_id} if prompt_id else {}
        resp = self._post("/interrupt", json_data=payload)
        resp.raise_for_status()
        return {"success": True}

    def queue_delete(self, prompt_ids: list[str]) -> dict[str, Any]:
        resp = self._post("/queue", json_data={"delete": prompt_ids})
        resp.raise_for_status()
        return {"success": True}

    def queue_clear(self) -> dict[str, Any]:
        resp = self._post("/queue", json_data={"clear": True})
        resp.raise_for_status()
        return {"success": True}

    def free_memory(self, unload_models: bool = False, free_memory: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if unload_models:
            payload["unload_models"] = True
        if free_memory:
            payload["free_memory"] = True
        resp = self._post("/free", json_data=payload)
        resp.raise_for_status()
        return {"success": True}
