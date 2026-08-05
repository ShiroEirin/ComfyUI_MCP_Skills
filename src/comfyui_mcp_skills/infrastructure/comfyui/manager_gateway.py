"""Hardened, bounded, stateless ComfyUI-Manager HTTP gateway."""

from __future__ import annotations

import ipaddress
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

import requests

from comfyui_mcp_skills.application._phase_o import (
    bounded_public,
    bounded_string,
    validate_http_url,
)
from comfyui_mcp_skills.application.provisioning_ports import ManagerGateway

_MAX_RESPONSE = 256 * 1024
_MAX_MODEL = 20 * 1024**3
_MAX_GIT = 512 * 1024**2
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_TAG = re.compile(r"tag:[A-Za-z0-9][A-Za-z0-9._/+\-]{0,126}")
_SECURE_FETCH_PREFLIGHT = object()


class SafeManagerGateway:
    """Validate every request and retain no server or queue routing state."""

    def __init__(
        self,
        *,
        allowed_source_hosts: set[str] | frozenset[str],
        allowed_server_origins: set[str] | frozenset[str],
        timeout_seconds: float = 10.0,
        max_response_bytes: int = _MAX_RESPONSE,
        max_model_bytes: int = _MAX_MODEL,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 0.1 <= float(timeout_seconds) <= 15
        ):
            raise ValueError("Manager timeout must be between 0.1 and 15 seconds")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not 1 <= max_response_bytes <= _MAX_RESPONSE
        ):
            raise ValueError("Manager response limit exceeds 256 KiB")
        if (
            isinstance(max_model_bytes, bool)
            or not isinstance(max_model_bytes, int)
            or not 1 <= max_model_bytes <= _MAX_MODEL
        ):
            raise ValueError("Manager model limit exceeds 20 GiB")
        self._timeout, self._max_response, self._max_model = (
            float(timeout_seconds),
            max_response_bytes,
            max_model_bytes,
        )
        self._hosts = frozenset(host.lower().rstrip(".") for host in allowed_source_hosts if host)
        self._server_origins = _server_origins(allowed_server_origins)

    def inspect(self, server: dict[str, Any]) -> dict[str, Any]:
        result = self.preflight_install(server)
        result.pop("preflight_token", None)
        return result

    def preflight_install(self, server: dict[str, Any]) -> dict[str, Any]:
        base, origin = _server(server, self._server_origins)
        self._require_secure_fetch_policy(base, origin)
        return {"state": "available", "preflight_token": _SECURE_FETCH_PREFLIGHT}

    def enqueue_install(
        self, server: dict[str, Any], item: dict[str, Any], *, queue_id: str
    ) -> dict[str, Any]:
        base, origin = _server(server, self._server_origins)
        queue_id = bounded_string(queue_id, "queue_id", maximum=128)
        fact = self._item(item)
        if server.get("_secure_fetch_preflight_token") is not _SECURE_FETCH_PREFLIGHT:
            self._require_secure_fetch_policy(base, origin)
        path = (
            "/manager/queue/secure-fetch-v1/install"
            if fact["kind"] == "node"
            else "/manager/queue/secure-fetch-v1/install_model"
        )
        payload = {
            "id": queue_id,
            "url": fact["source_url"],
            "version": fact["version"],
            "sha256": fact["checksum"],
            "size_bytes": fact["size_bytes"],
            "target_dir": fact["target_dir"],
            "source_policy": {
                "version": "comfyui-mcp-secure-fetch-v1",
                "allow_redirects": False,
                "require_public_ip": True,
                "max_bytes": fact["size_bytes"],
            },
            "receipt_required": True,
        }
        encoded = json.dumps(
            payload, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode()
        if len(encoded) > 64 * 1024:
            raise ValueError("Manager install request is too large")
        try:
            response = requests.post(
                base + path,
                data=encoded,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=self._timeout,
                allow_redirects=False,
                stream=True,
            )
            data = self._read(response, origin)
            _validate_receipt(data, fact, queue_id=queue_id)
        except (requests.Timeout, requests.ConnectionError):
            return {"queue_id": queue_id, "state": "unknown", "retryable": True}
        state = (
            _state(data.get("state", data.get("status", "queued")))
            if isinstance(data, Mapping)
            else "queued"
        )
        return {
            "queue_id": queue_id,
            "state": state if state != "unknown" else "queued",
            "retryable": False,
        }

    def _require_secure_fetch_policy(self, base: str, origin: tuple[str, str, int]) -> None:
        response = requests.get(
            base + "/manager/capabilities",
            headers={"Accept": "application/json"},
            timeout=self._timeout,
            allow_redirects=False,
            stream=True,
        )
        data = self._read(response, origin)
        policy = data.get("secure_fetch") if isinstance(data, Mapping) else None
        expected = {
            "version": "comfyui-mcp-secure-fetch-v1",
            "enqueue_receipt": True,
            "completion_receipt": True,
            "queue_id_bound": True,
        }
        if not isinstance(policy, Mapping) or any(
            policy.get(key) != value for key, value in expected.items()
        ):
            raise ValueError("Manager secure-fetch-v1 capability is not supported")

    def observe_install(
        self,
        server: dict[str, Any],
        queue_id: str,
        *,
        item: dict[str, Any],
    ) -> dict[str, Any]:
        base, origin = _server(server, self._server_origins)
        fact = self._item(item)
        result = self._status(base, origin, bounded_string(queue_id, "queue_id", maximum=128))
        if result["state"] == "completed":
            _validate_receipt(result.pop("receipt_payload", None), fact, queue_id=queue_id)
        else:
            result.pop("receipt_payload", None)
        return result

    def _status(self, base: str, origin: tuple[str, str, int], queue_id: str) -> dict[str, Any]:
        try:
            response = requests.get(
                base + "/manager/queue/secure-fetch-v1/status",
                headers={"Accept": "application/json"},
                timeout=self._timeout,
                allow_redirects=False,
                stream=True,
            )
            data = self._read(response, origin)
        except (requests.Timeout, requests.ConnectionError):
            return {"queue_id": queue_id, "state": "unknown", "retryable": True}
        state, receipt_payload = _find(data, queue_id)
        return {
            "queue_id": queue_id,
            "state": state,
            "retryable": state == "unknown",
            "receipt_payload": receipt_payload,
        }

    def _read(self, response: requests.Response, origin: tuple[str, str, int]) -> Any:
        try:
            if 300 <= response.status_code < 400 or _origin(urlparse(str(response.url))) != origin:
                raise ValueError("Manager redirects or cross-origin responses are not permitted")
            if response.status_code == 404:
                raise LookupError("ComfyUI Manager is unavailable")
            response.raise_for_status()
            if response.headers.get("Content-Encoding", "").strip().lower() not in {"", "identity"}:
                raise ValueError("Manager encoded responses are unsupported")
            length = response.headers.get("Content-Length")
            if length is not None and (
                not length.isascii() or not length.isdigit() or int(length) > self._max_response
            ):
                raise ValueError("Manager response is too large")
            payload = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if chunk:
                    if len(chunk) > self._max_response - len(payload):
                        raise ValueError("Manager response is too large")
                    payload.extend(chunk)
            return (
                {}
                if not payload
                else bounded_public(
                    json.loads(
                        payload.decode("utf-8"),
                        parse_constant=lambda _value: (_ for _ in ()).throw(
                            ValueError("invalid number")
                        ),
                    )
                )
            )
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
            raise ValueError("Manager response is invalid") from exc
        finally:
            response.close()

    def _item(self, item: object) -> dict[str, Any]:
        if not isinstance(item, Mapping) or (item.get("kind"), item.get("source_type")) not in {
            ("node", "git"),
            ("model", "model"),
        }:
            raise ValueError("Manager install item is invalid")
        kind = str(item["kind"])
        version = bounded_string(item.get("version"), "version", maximum=128)
        checksum = bounded_string(item.get("checksum"), "checksum", maximum=64)
        if (
            _COMMIT.fullmatch(version) is None and _TAG.fullmatch(version) is None
        ) or _SHA256.fullmatch(checksum) is None:
            raise ValueError("Manager requires fixed versions and known SHA-256 checksums")
        source = validate_http_url(
            item.get("source_url"), field="source_url", https_only=True, allowed_hosts=self._hosts
        )
        size = item.get("size_bytes")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or not 1 <= size <= (self._max_model if kind == "model" else _MAX_GIT)
        ):
            raise ValueError("Manager install size exceeds its hard limit")
        target = bounded_string(item.get("target_dir"), "target_dir", maximum=256)
        if target.startswith(("/", "\\")) or ".." in target.replace("\\", "/").split("/"):
            raise ValueError("Manager install target is unsafe")
        return {
            "kind": kind,
            "source_url": source,
            "version": version,
            "checksum": checksum,
            "size_bytes": size,
            "target_dir": target,
        }


def _server(
    server: object,
    allowed_origins: frozenset[str],
) -> tuple[str, tuple[str, str, int]]:
    if not isinstance(server, Mapping) or any(
        key in server for key in ("auth", "token", "password", "comfy_api_key")
    ):
        raise ValueError("Manager Server context must contain no credential values")
    base = validate_http_url(server.get("url"), field="server URL", allow_loopback=True).rstrip("/")
    parsed = urlparse(base)
    if parsed.path not in {"", "/"} or base not in allowed_origins:
        raise ValueError("Manager Server origin is not explicitly allowlisted")
    return base, _origin(parsed)


def _server_origins(values: set[str] | frozenset[str]) -> frozenset[str]:
    result: set[str] = set()
    for value in values:
        origin = validate_http_url(
            value, field="Manager Server origin", allow_loopback=True
        ).rstrip("/")
        parsed = urlparse(origin)
        if parsed.path not in {"", "/"}:
            raise ValueError("Manager Server allowlist entries must be origins")
        hostname = str(parsed.hostname or "").lower()
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            if hostname != "localhost":
                raise ValueError("Manager Server origins must use literal IPs or localhost")
        result.add(origin)
    return frozenset(result)


def _origin(parsed: Any) -> tuple[str, str, int]:
    scheme = str(parsed.scheme).lower()
    return (
        scheme,
        str(parsed.hostname or "").lower().rstrip("."),
        parsed.port or (443 if scheme == "https" else 80),
    )


def _find(data: object, queue_id: str) -> tuple[str, object]:
    if isinstance(data, Mapping):
        for key in ("items", "queue", "tasks"):
            rows = data.get(key)
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)):
                for row in rows[:1000]:
                    if (
                        isinstance(row, Mapping)
                        and str(row.get("id", row.get("queue_id", ""))) == queue_id
                    ):
                        return _state(row.get("state", row.get("status", "unknown"))), row
        if not queue_id:
            return _state(data.get("state", data.get("status", "unknown"))), data
    return "unknown", {}


def _validate_receipt(value: object, fact: Mapping[str, Any], *, queue_id: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("Manager did not return a secure-fetch receipt")
    receipt = value.get("receipt", value)
    expected = {
        "queue_id": queue_id,
        "policy_version": "comfyui-mcp-secure-fetch-v1",
        "source_url": fact["source_url"],
        "version": fact["version"],
        "sha256": fact["checksum"],
        "size_bytes": fact["size_bytes"],
        "redirects_allowed": False,
        "public_ip_enforced": True,
    }
    if not isinstance(receipt, Mapping) or any(
        receipt.get(key) != item for key, item in expected.items()
    ):
        raise ValueError("Manager secure-fetch receipt conflicts with the approved dependency")


def _state(value: object) -> str:
    raw = str(value).strip().lower().replace("-", "_")
    groups = {
        "queued": {"pending", "accepted", "queued", "waiting"},
        "running": {"running", "installing", "processing", "in_progress"},
        "completed": {"completed", "complete", "done", "success", "succeeded", "installed"},
        "failed": {"failed", "failure", "error"},
        "cancelled": {"cancelled", "canceled"},
    }
    return next((name for name, values in groups.items() if raw in values), "unknown")


__all__ = ["ManagerGateway", "SafeManagerGateway"]
