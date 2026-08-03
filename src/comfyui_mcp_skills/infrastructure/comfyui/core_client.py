"""Core transport, health, and file transfer operations for ComfyUI."""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

_MAX_JSON_NUMBER_CHARS = 128
_MAX_CONTENT_LENGTH_CHARS = 20
_MAX_UPLOAD_RECEIPT_BYTES = 64 * 1024
_STREAM_CHUNK_BYTES = 64 * 1024


def _bounded_json_int(value: str) -> int:
    if len(value) > _MAX_JSON_NUMBER_CHARS:
        raise ValueError("ComfyUI JSON integer is too large")
    return int(value)


def _bounded_json_float(value: str) -> float:
    if len(value) > _MAX_JSON_NUMBER_CHARS:
        raise ValueError("ComfyUI JSON number is too large")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("ComfyUI JSON number is not finite")
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"ComfyUI JSON constant is unsupported: {value}")


def _response_header(response: requests.Response, name: str) -> str | None:
    value = response.headers.get(name)
    if value is None:
        value = response.headers.get(name.lower())
    return value if isinstance(value, str) else None


def _reject_redirect(response: requests.Response) -> None:
    status_code = response.status_code
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        if 300 <= status_code < 400:
            raise requests.HTTPError(
                "ComfyUI redirect response is not permitted", response=response
            )


def _raise_for_status_without_redirects(response: requests.Response) -> None:
    _reject_redirect(response)
    response.raise_for_status()


def _validate_content_length(
    response: requests.Response, *, max_bytes: int, description: str
) -> None:
    value = _response_header(response, "Content-Length")
    if value is None:
        return
    if (
        not value
        or len(value) > _MAX_CONTENT_LENGTH_CHARS
        or not value.isascii()
        or not value.isdigit()
    ):
        raise ValueError(f"{description} Content-Length is invalid")
    if int(value) > max_bytes:
        raise ValueError(f"{description} is too large")


def _reject_encoded_response(response: requests.Response, *, description: str) -> None:
    value = _response_header(response, "Content-Encoding")
    if value is not None and value.strip().lower() not in {"", "identity"}:
        raise ValueError(f"{description} Content-Encoding is unsupported")


def _decode_bounded_json_response(
    response: requests.Response,
    *,
    max_bytes: int,
    description: str,
    reject_content_encoding: bool = False,
) -> Any:
    _raise_for_status_without_redirects(response)
    if reject_content_encoding:
        _reject_encoded_response(response, description=description)
    _validate_content_length(response, max_bytes=max_bytes, description=description)
    payload = bytearray()
    for chunk in response.iter_content(chunk_size=_STREAM_CHUNK_BYTES):
        if not chunk:
            continue
        if len(chunk) > max_bytes - len(payload):
            raise ValueError(f"{description} is too large")
        payload.extend(chunk)
    try:
        return json.loads(
            payload.decode("utf-8"),
            parse_int=_bounded_json_int,
            parse_float=_bounded_json_float,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValueError) as exc:
        raise ValueError(f"{description} is invalid") from exc


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
        if kwargs.pop("allow_redirects", False):
            raise ValueError("ComfyUI redirects are not permitted")
        headers = self._headers()
        headers.update(kwargs.pop("headers", {}))
        response = requests.get(
            f"{self.server_url}{path}",
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
            **kwargs,
        )
        try:
            _reject_redirect(response)
            return response
        except BaseException:
            response.close()
            raise

    def _get_json_bounded(self, path: str, *, max_bytes: int, **kwargs: Any) -> Any:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        response = self._get(path, stream=True, allow_redirects=False, **kwargs)
        try:
            return _decode_bounded_json_response(
                response,
                max_bytes=max_bytes,
                description="ComfyUI JSON response",
            )
        finally:
            response.close()

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
        try:
            payload = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                if len(chunk) > max_bytes - len(payload):
                    raise ValueError(f"Output exceeds {max_bytes} byte limit")
                payload.extend(chunk)
            return bytes(payload)
        finally:
            response.close()

    def download_output_to(
        self,
        filename: str,
        destination: str | Path,
        subfolder: str = "",
        storage_type: str = "output",
        *,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> dict[str, int | str]:
        destination_path = Path(destination)
        temporary = destination_path.with_name(f".{destination_path.name}.{uuid.uuid4().hex}.tmp")
        response = self._output_response(filename, subfolder, storage_type, max_bytes)
        try:
            digest = hashlib.sha256()
            total = 0
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("xb") as handle:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    if len(chunk) > max_bytes - total:
                        raise ValueError(f"Output exceeds {max_bytes} byte limit")
                    total += len(chunk)
                    handle.write(chunk)
                    digest.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination_path)
            return {"size_bytes": total, "sha256": digest.hexdigest()}
        finally:
            try:
                response.close()
            finally:
                temporary.unlink(missing_ok=True)

    def _output_response(
        self, filename: str, subfolder: str, output_type: str, max_bytes: int
    ) -> requests.Response:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        response = self._get(
            "/view",
            params={
                "filename": filename,
                "subfolder": subfolder,
                "type": output_type,
            },
            headers={"Accept-Encoding": "identity"},
            stream=True,
            allow_redirects=False,
        )
        try:
            _raise_for_status_without_redirects(response)
            _reject_encoded_response(response, description="ComfyUI output")
            _validate_content_length(response, max_bytes=max_bytes, description="ComfyUI output")
            return response
        except BaseException:
            response.close()
            raise

    def upload_file(self, filepath: str) -> dict[str, Any]:
        path = Path(filepath)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        boundary = f"----ComfyUIBoundary{uuid.uuid4().hex}"
        closing = f"\r\n--{boundary}--\r\n".encode("ascii")
        stream = _MultipartStream(
            [_multipart_file_part("image", path, content_type, boundary, closing)]
        )
        return self._upload_multipart("/upload/image", stream, boundary)

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
        return self._upload_multipart("/upload/mask", _MultipartStream(parts), boundary)

    def _upload_multipart(
        self, endpoint: str, stream: _MultipartStream, boundary: str
    ) -> dict[str, Any]:
        headers = self._headers()
        headers.update(
            {
                "Accept-Encoding": "identity",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(stream)),
            }
        )
        response = requests.post(
            f"{self.server_url}{endpoint}",
            data=stream,
            headers=headers,
            timeout=self.timeout,
            stream=True,
            allow_redirects=False,
        )
        try:
            receipt = _decode_bounded_json_response(
                response,
                max_bytes=_MAX_UPLOAD_RECEIPT_BYTES,
                description="ComfyUI upload receipt",
                reject_content_encoding=True,
            )
            if not isinstance(receipt, dict):
                raise ValueError("ComfyUI upload receipt is invalid")
            return receipt
        finally:
            response.close()
