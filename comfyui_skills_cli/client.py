"""ComfyUI HTTP client — all server communication goes through here."""

from __future__ import annotations
from collections.abc import Callable, Iterator

import json
import mimetypes
import os
import time
import uuid
from pathlib import Path
from typing import Any, Generator
from urllib.parse import quote

import requests


class _MultipartStream:
    """Lazily encode multipart fields while reading files in bounded chunks."""

    _CHUNK_SIZE = 64 * 1024

    def __init__(self, parts: list[tuple[bytes, Path | None, bytes]]) -> None:
        self._parts = parts
        self._length = sum(
            len(prefix) + (path.stat().st_size if path is not None else 0) + len(suffix)
            for prefix, path, suffix in parts
        )

    def __len__(self) -> int:
        return self._length

    def __iter__(self) -> Iterator[bytes]:
        for prefix, path, suffix in self._parts:
            yield prefix
            if path is not None:
                with path.open("rb") as file:
                    while chunk := file.read(self._CHUNK_SIZE):
                        yield chunk
            yield suffix


def _multipart_file_part(
    field: str, path: Path, content_type: str, boundary: str, suffix: bytes
) -> tuple[bytes, Path, bytes]:
    filename = quote(path.name, safe=" !#$&'()+,-.;=@[]^_`{}~")
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    return header, path, suffix


def _multipart_text_part(
    field: str, value: str, boundary: str, suffix: bytes
) -> tuple[bytes, None, bytes]:
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"\r\n\r\n'
    ).encode("utf-8")
    return header, None, value.encode("utf-8") + suffix


class ComfyUIClient:
    def __init__(self, server_url: str, auth: str = "", comfy_api_key: str = "", timeout: float = 30.0):
        self.server_url = server_url.rstrip("/")
        self.auth = auth
        self.comfy_api_key = comfy_api_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.auth:
            headers["Authorization"] = f"Bearer {self.auth}"
        return headers

    def _get(self, path: str, **kwargs: Any) -> requests.Response:
        timeout = kwargs.pop("timeout", self.timeout)
        return requests.get(
            f"{self.server_url}{path}",
            headers=self._headers(),
            timeout=timeout,
            **kwargs,
        )

    def _post(self, path: str, json_data: Any = None, **kwargs: Any) -> requests.Response:
        timeout = kwargs.pop("timeout", self.timeout)
        return requests.post(
            f"{self.server_url}{path}",
            headers=self._headers(),
            json=json_data,
            timeout=timeout,
            **kwargs,
        )

    # -- Health --

    def check_health(self) -> dict[str, Any]:
        try:
            resp = self._get("/system_stats")
            resp.raise_for_status()
            return {"status": "online", "data": resp.json()}
        except (requests.RequestException, ValueError) as exc:
            return {"status": "offline", "error": str(exc)}

    def get_system_stats(self) -> dict[str, Any]:
        resp = self._get("/system_stats")
        resp.raise_for_status()
        return resp.json()

    # -- Prompt execution --

    def queue_prompt(self, workflow: dict[str, Any], client_id: str | None = None, targets: list[str] | None = None, priority: float | None = None) -> dict[str, Any]:
        cid = client_id or str(uuid.uuid4())
        payload: dict[str, Any] = {
            "prompt": workflow,
            "client_id": cid,
        }
        if priority is not None:
            payload["number"] = priority
        if targets:
            payload["partial_execution_targets"] = [[t] for t in targets]
        if self.comfy_api_key:
            payload["extra_data"] = {"api_key_comfy_org": self.comfy_api_key}
        resp = self._post("/prompt", json_data=payload)
        resp.raise_for_status()
        data = resp.json()
        data["client_id"] = cid
        return data

    def get_history(self, prompt_id: str) -> dict[str, Any] | None:
        resp = self._get(f"/history/{prompt_id}")
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data.get(prompt_id)

    def get_history_list(self, max_items: int = 20, offset: int = 0) -> dict[str, Any]:
        resp = self._get("/history", params={"max_items": max_items, "offset": offset})
        resp.raise_for_status()
        return resp.json()

    def get_jobs(self, status: str = "", limit: int = 20, offset: int = 0, sort_by: str = "created_at", sort_order: str = "desc") -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "offset": offset, "sort_by": sort_by, "sort_order": sort_order}
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

    def get_queue(self) -> dict[str, Any]:
        resp = self._get("/queue")
        resp.raise_for_status()
        return resp.json()

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
            time.monotonic() + max(timeout_seconds, 0)
            if timeout_seconds is not None
            else None
        )
        connect_timeout = self.timeout
        if deadline is not None:
            connect_timeout = min(connect_timeout, max(0.1, deadline - time.monotonic()))
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
                    ws.settimeout(min(1.0, remaining))
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

    def download_output(
        self,
        filename: str,
        subfolder: str = "",
        output_type: str = "output",
        *,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> bytes:
        response = self._output_response(filename, subfolder, output_type, max_bytes)
        payload = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                payload.extend(chunk)
                if len(payload) > max_bytes:
                    raise ValueError(f"Output exceeds {max_bytes} byte limit")
            return bytes(payload)
        finally:
            response.close()

    def download_output_to(
        self,
        filename: str,
        destination: str | Path,
        subfolder: str = "",
        output_type: str = "output",
        *,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> int:
        destination_path = Path(destination)
        temporary = destination_path.with_name(
            f".{destination_path.name}.{uuid.uuid4().hex}.tmp"
        )
        response = self._output_response(filename, subfolder, output_type, max_bytes)
        total = 0
        try:
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("xb") as handle:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"Output exceeds {max_bytes} byte limit")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination_path)
            return total
        finally:
            response.close()
            temporary.unlink(missing_ok=True)

    def _output_response(
        self, filename: str, subfolder: str, output_type: str, max_bytes: int
    ) -> requests.Response:
        response = self._get(
            "/view",
            params={
                "filename": filename,
                "subfolder": subfolder,
                "type": output_type,
            },
            stream=True,
        )
        response.raise_for_status()
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                declared = int(content_length)
            except ValueError:
                declared = -1
            if declared > max_bytes:
                response.close()
                raise ValueError(f"Output exceeds {max_bytes} byte limit")
        return response

    # -- Node info --

    def get_object_info(self) -> dict[str, Any]:
        resp = self._get("/object_info")
        resp.raise_for_status()
        return resp.json()

    def get_object_info_node(self, node_class: str) -> dict[str, Any] | None:
        resp = self._get(f"/object_info/{node_class}")
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
        resp = self._get(f"/models/{folder}")
        resp.raise_for_status()
        return resp.json()


    # -- Manager API (ComfyUI-Manager plugin) --

    def manager_start_queue(self) -> bool:
        try:
            resp = self._get("/manager/queue/start", timeout=10)
            return resp.status_code < 500
        except requests.RequestException:
            return False

    def manager_install_node(self, repo_url: str, pkg_name: str) -> dict[str, Any]:
        resp = self._post("/manager/queue/install", json_data={
            "id": pkg_name,
            "url": repo_url,
            "install_type": "git-clone",
        })
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

    # -- Queue management --

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

    # -- Memory management --

    def free_memory(self, unload_models: bool = False, free_memory: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if unload_models:
            payload["unload_models"] = True
        if free_memory:
            payload["free_memory"] = True
        resp = self._post("/free", json_data=payload)
        resp.raise_for_status()
        return {"success": True}

    # -- File upload --

    def upload_file(self, filepath: str) -> dict[str, Any]:
        path = Path(filepath)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        boundary = f"----ComfyUIBoundary{uuid.uuid4().hex}"
        closing = f"\r\n--{boundary}--\r\n".encode("ascii")
        stream = _MultipartStream(
            [_multipart_file_part("image", path, content_type, boundary, closing)]
        )

        headers = self._headers()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        headers["Content-Length"] = str(len(stream))
        resp = requests.post(
            f"{self.server_url}/upload/image",
            data=stream,
            headers=headers,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def upload_image(self, filepath: str) -> dict[str, Any]:
        return self.upload_file(filepath)

    def upload_mask(self, filepath: str, original_ref: str = "") -> dict[str, Any]:
        path = Path(filepath)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        boundary = f"----ComfyUIBoundary{uuid.uuid4().hex}"
        closing = f"--{boundary}--\r\n".encode("ascii")
        parts: list[tuple[bytes, Path | None, bytes]] = [
            _multipart_file_part("image", path, content_type, boundary, b"\r\n")
        ]
        if original_ref:
            parts.append(
                _multipart_text_part(
                    "original_ref", original_ref, boundary, b"\r\n" + closing
                )
            )
        else:
            header, file_path, suffix = parts[0]
            parts[0] = (header, file_path, suffix + closing)
        stream = _MultipartStream(parts)

        headers = self._headers()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        headers["Content-Length"] = str(len(stream))
        resp = requests.post(
            f"{self.server_url}/upload/mask",
            data=stream,
            headers=headers,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # -- Node replacements, logs, templates --

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

    # -- ComfyUI Userdata API --

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
                            p = item.get("path") or item.get("name") or ""
                            if isinstance(p, str) and p.endswith(".json"):
                                paths.append(p)
                elif isinstance(data, dict) and "files" in data:
                    paths = [
                        f.get("path", f.get("name", ""))
                        for f in data["files"]
                        if isinstance(f, dict) and (f.get("path", "") or f.get("name", "")).endswith(".json")
                    ]
                if paths:
                    return paths
            except (requests.RequestException, ValueError):
                continue
        return []

    def read_userdata_workflow(self, workflow_path: str) -> dict[str, Any] | None:
        import urllib.parse
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

    # -- Manager model install --

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
