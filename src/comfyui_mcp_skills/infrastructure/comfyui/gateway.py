"""ComfyUI transport adapter implementing application ports."""

from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Any

import requests
import websocket

from comfyui_mcp_skills.domain.errors import ExecutionFailed, ServerOffline
from comfyui_mcp_skills.infrastructure.comfyui.client import ComfyUIClient


class ComfyUIGatewayAdapter:
    def __init__(self, config: dict[str, Any]) -> None:
        self._client = ComfyUIClient(
            server_url=str(config.get("url", "http://127.0.0.1:8188")),
            auth=str(config.get("auth", "")),
            comfy_api_key=str(config.get("comfy_api_key", "")),
            timeout=float(config.get("timeout", 30.0)),
        )

    def queue_prompt(self, workflow: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        try:
            return self._client.queue_prompt(workflow, **kwargs)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if 400 <= status < 500:
                raise ExecutionFailed("ComfyUI rejected workflow submission") from exc
            raise ServerOffline("ComfyUI submission outcome is unknown") from exc
        except (requests.RequestException, ValueError, OSError) as exc:
            raise ServerOffline("ComfyUI submission outcome is unknown") from exc

    def get_system_stats(self) -> dict[str, Any]:
        return self._call(self._client.get_system_stats)

    def get_object_info(self) -> dict[str, Any]:
        return self._call(self._client.get_object_info)

    def get_object_info_node(self, node_class: str) -> dict[str, Any] | None:
        return self._call(self._client.get_object_info_node, node_class)

    def get_model_folders(self) -> list[str]:
        return self._call(self._client.get_model_folders)

    def get_models(self, folder: str) -> list[str]:
        return self._call(self._client.get_models, folder)

    def get_history(
        self, prompt_id: str, *, timeout_seconds: float | None = None
    ) -> dict[str, Any] | None:
        return self._call(
            self._client.get_history,
            prompt_id,
            timeout_seconds=timeout_seconds,
        )

    def get_history_list(self, max_items: int = 20, offset: int = 0) -> dict[str, Any]:
        return self._call(self._client.get_history_list, max_items, offset)

    def get_queue(self, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        return self._call(self._client.get_queue, timeout_seconds=timeout_seconds)

    def interrupt(self, prompt_id: str = "") -> dict[str, Any]:
        return self._call(self._client.interrupt, prompt_id)

    def queue_delete(self, prompt_ids: list[str]) -> dict[str, Any]:
        return self._call(self._client.queue_delete, prompt_ids)

    def ws_events(
        self,
        client_id: str,
        prompt_id: str,
        timeout_seconds: float | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        try:
            yield from self._client.ws_events(client_id, prompt_id, timeout_seconds, cancel_check)
        except websocket.WebSocketException as exc:
            raise ServerOffline("ComfyUI WebSocket is unavailable") from exc

    @staticmethod
    def _call(function: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except (requests.ConnectionError, requests.Timeout, OSError) as exc:
            raise ServerOffline("ComfyUI server is unavailable") from exc
        except requests.RequestException as exc:
            raise ExecutionFailed("ComfyUI request failed") from exc

    def upload_file(self, path: str, *, purpose: str, original_ref: str) -> dict[str, Any]:
        if purpose == "mask":
            return self._client.upload_mask(path, original_ref)
        return self._client.upload_file(path)

    def download_output(
        self,
        filename: str,
        subfolder: str = "",
        output_type: str = "output",
        *,
        max_bytes: int,
    ) -> bytes:
        try:
            return self._client.download_output(
                filename,
                subfolder,
                output_type,
                max_bytes=max_bytes,
            )
        except ValueError:
            raise
        except requests.RequestException as exc:
            raise ExecutionFailed("ComfyUI output download failed") from exc


def create_gateway(config: dict[str, Any]) -> ComfyUIGatewayAdapter:
    return ComfyUIGatewayAdapter(config)
