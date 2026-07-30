"""Core transport, health, and file transfer operations for ComfyUI."""

from __future__ import annotations

import mimetypes
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
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
    ).encode()
    return header, path, suffix


def _multipart_text_part(
    field: str, value: str, boundary: str, suffix: bytes
) -> tuple[bytes, None, bytes]:
    header = (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n').encode()
    return header, None, value.encode("utf-8") + suffix


class CoreClient:
    """Own shared HTTP transport, health checks, and file transfer behavior."""

    def __init__(
        self, server_url: str, auth: str = "", comfy_api_key: str = "", timeout: float = 30.0
    ):
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
        temporary = destination_path.with_name(f".{destination_path.name}.{uuid.uuid4().hex}.tmp")
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
                _multipart_text_part("original_ref", original_ref, boundary, b"\r\n" + closing)
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
