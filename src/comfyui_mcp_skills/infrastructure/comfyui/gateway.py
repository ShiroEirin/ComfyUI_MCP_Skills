"""ComfyUI transport adapter implementing application ports."""

from __future__ import annotations

import os
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import requests
import websocket

from comfyui_mcp_skills.domain.errors import ExecutionFailed, ServerOffline, UploadFailed
from comfyui_mcp_skills.infrastructure.comfyui.capabilities import classify_capability_status
from comfyui_mcp_skills.infrastructure.comfyui.client import ComfyUIClient

_SECRET_SCHEMES = ("env:",)
_KNOWN_SECRET_FIELDS = frozenset({"auth", "comfy_api_key"})

# Upstream rejections must reach the agent without letting a hostile or broken
# server inflate the MCP error payload, so every field is bounded.
_MAX_REJECTED_NODES = 32
_MAX_ERRORS_PER_NODE = 8
_MAX_UPSTREAM_TEXT = 300


def _bounded_text(value: object) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return text.strip()[:_MAX_UPSTREAM_TEXT]


def _upstream_rejection_details(exc: requests.HTTPError) -> dict[str, Any]:
    """Project ComfyUI's validation rejection into bounded, agent-usable details.

    ComfyUI answers a bad /prompt with ``{"error": {...}, "node_errors": {...}}``
    listing the exact node and input that failed. Dropping that payload left
    agents with a bare "rejected" message and no way to fix their graph.
    """
    response = exc.response
    if response is None:
        return {}
    details: dict[str, Any] = {"http_status": response.status_code}
    try:
        payload = response.json()
    except ValueError:
        body = _bounded_text(getattr(response, "text", ""))
        if body:
            details["upstream_body"] = body
        return details
    if not isinstance(payload, dict):
        return details
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("type", "message", "details"):
            text = _bounded_text(error.get(key))
            if text:
                details[f"upstream_{key}"] = text
    elif isinstance(error, str):
        text = _bounded_text(error)
        if text:
            details["upstream_error"] = text
    node_errors = payload.get("node_errors")
    if isinstance(node_errors, dict) and node_errors:
        ordered = sorted(node_errors.items(), key=lambda item: str(item[0]))
        normalized: dict[str, list[dict[str, str]]] = {}
        for node_id, entry in ordered[:_MAX_REJECTED_NODES]:
            if not isinstance(entry, dict):
                continue
            errors = entry.get("errors")
            if not isinstance(errors, list) or not errors:
                continue
            normalized[str(node_id)] = [
                {
                    "type": _bounded_text(item.get("type")),
                    "message": _bounded_text(item.get("message")),
                    "details": _bounded_text(item.get("details")),
                }
                for item in errors[:_MAX_ERRORS_PER_NODE]
                if isinstance(item, dict)
            ]
        if normalized:
            details["node_errors"] = normalized
            details["rejected_nodes"] = sorted(normalized)
    return details


def _rejection_summary(details: dict[str, Any]) -> str:
    """Name the first few offending nodes so the message alone is actionable."""
    parts: list[str] = []
    node_errors = details.get("node_errors")
    if isinstance(node_errors, dict):
        for node_id in details.get("rejected_nodes") or sorted(node_errors):
            for item in (node_errors.get(node_id) or [])[:2]:
                text = ": ".join(
                    part for part in (item.get("message"), item.get("details")) if part
                )
                if text:
                    parts.append(f"node {node_id} {text}")
                if len(parts) >= 3:
                    break
            if len(parts) >= 3:
                break
    if not parts:
        for key in ("upstream_message", "upstream_error", "upstream_body"):
            upstream = details.get(key)
            if isinstance(upstream, str) and upstream:
                parts.append(upstream)
                break
    message = "ComfyUI rejected workflow submission"
    return f"{message}: {'; '.join(parts)}" if parts else message



def _resolve_secret_refs(config: dict[str, Any]) -> dict[str, str]:
    """Resolve ``secret_refs`` to concrete values without persisting secrets.

    Only the ``env:`` scheme is supported: the reference names an environment
    variable, so a Config Bundle can travel without carrying the secret itself.
    An unknown scheme or a missing variable fails loudly instead of silently
    connecting with an empty credential.
    """
    refs = config.get("secret_refs")
    if refs is None:
        return {}
    if not isinstance(refs, dict):
        raise ValueError("secret_refs must be an object")
    resolved: dict[str, str] = {}
    for field, ref in refs.items():
        if not isinstance(field, str) or not field:
            raise ValueError("secret_refs keys must be non-empty strings")
        if field not in _KNOWN_SECRET_FIELDS:
            raise ValueError(
                f"secret_refs[{field}] is not a supported credential field; "
                f"supported: {', '.join(sorted(_KNOWN_SECRET_FIELDS))}"
            )
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"secret_refs[{field}] must be a non-empty string")
        scheme = next((item for item in _SECRET_SCHEMES if ref.startswith(item)), "")
        if not scheme:
            raise ValueError(
                f"secret_refs[{field}] uses an unsupported scheme; supported: env:"
            )
        name = ref[len(scheme) :]
        value = os.environ.get(name, "")
        if not value:
            raise ValueError(
                f"secret_refs[{field}] references env var {name!r} which is not set"
            )
        resolved[field] = value
    return resolved


def create_client(config: dict[str, Any]) -> ComfyUIClient:
    """Build one client from a server config, resolving ``secret_refs``.

    Every MCP surface must go through this factory: constructing
    ``ComfyUIClient`` directly skips ``secret_refs`` and silently connects
    without the configured credential.
    """
    secrets = _resolve_secret_refs(config)
    return ComfyUIClient(
        server_url=str(config.get("url", "http://127.0.0.1:8188")),
        auth=secrets.get("auth", str(config.get("auth", ""))),
        comfy_api_key=secrets.get("comfy_api_key", str(config.get("comfy_api_key", ""))),
        timeout=float(config.get("timeout", 30.0)),
    )


class ComfyUIGatewayAdapter:
    def __init__(self, config: dict[str, Any]) -> None:
        self._client = create_client(config)

    def queue_prompt(self, workflow: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        try:
            return self._client.queue_prompt(workflow, **kwargs)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if 400 <= status < 500:
                details = _upstream_rejection_details(exc)
                raise ExecutionFailed(_rejection_summary(details), details=details) from exc
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

    def get_history_bounded(
        self,
        *,
        prompt_id: str = "",
        max_items: int = 20,
    ) -> dict[str, Any]:
        return self._call(
            self._client.get_history_bounded,
            prompt_id=prompt_id,
            max_items=max_items,
        )

    def get_queue(self, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        try:
            return self._call(self._client.get_queue, timeout_seconds=timeout_seconds)
        except ValueError as exc:
            raise ExecutionFailed("ComfyUI queue response is invalid") from exc

    def interrupt(self, prompt_id: str = "") -> dict[str, Any]:
        return self._call(self._client.interrupt, prompt_id)

    def queue_delete(self, prompt_ids: list[str]) -> dict[str, Any]:
        return self._call(self._client.queue_delete, prompt_ids)

    def queue_clear(self) -> dict[str, Any]:
        return self._call(self._client.queue_clear)

    def get_logs(self) -> dict[str, Any]:
        return self._optional_call(self._client.get_logs)

    def get_workflow_templates(self) -> dict[str, Any]:
        return self._optional_call(self._client.get_workflow_templates)

    def get_subgraphs(self) -> dict[str, Any]:
        return self._optional_call(self._client.get_subgraphs)

    def get_subgraph(self, subgraph_id: str) -> dict[str, Any]:
        return self._optional_call(self._client.get_subgraph, subgraph_id)

    def get_capabilities(self) -> dict[str, Any]:
        result = self._optional_call(self._client.probe_capabilities)
        data = result.get("data")
        if result.get("state") == "supported" and isinstance(data, dict):
            capabilities = dict(data)
            capabilities["output_to_input_copy"] = {"state": "unsupported"}
            return {"state": "supported", "data": capabilities}
        return result

    @staticmethod
    def supports_output_to_input_copy() -> bool:
        """Report false until a real atomic ComfyUI copy endpoint is available."""
        return False

    def free_memory(self, *, unload_models: bool, free_memory: bool) -> dict[str, Any]:
        return self._call(
            self._client.free_memory,
            unload_models=unload_models,
            free_memory=free_memory,
        )

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

    @staticmethod
    def _optional_call(function: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return {"state": "supported", "data": function(*args, **kwargs)}
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            return {"state": classify_capability_status(status), "data": None}
        except (requests.RequestException, ValueError, OSError):
            return {"state": "temporarily_unavailable", "data": None}

    def upload_file(self, path: str, *, purpose: str, original_ref: str) -> dict[str, Any]:
        try:
            if purpose == "mask":
                return self._client.upload_mask(path, original_ref)
            return self._client.upload_file(path)
        except Exception as exc:
            raise UploadFailed("ComfyUI upload failed") from exc

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

    def download_output_to(
        self,
        filename: str,
        destination: str | Path,
        subfolder: str = "",
        storage_type: str = "output",
        *,
        max_bytes: int,
    ) -> dict[str, int | str]:
        try:
            return self._client.download_output_to(
                filename,
                destination,
                subfolder,
                storage_type,
                max_bytes=max_bytes,
            )
        except ValueError:
            raise
        except (requests.RequestException, OSError) as exc:
            raise ExecutionFailed("ComfyUI output transfer failed") from exc


def create_gateway(config: dict[str, Any]) -> ComfyUIGatewayAdapter:
    return ComfyUIGatewayAdapter(config)
