"""Canonical, bounded public-data helpers shared by Phase O services."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

_MAX_PUBLIC_BYTES = 1024 * 1024
_MAX_DEPTH = 12
_MAX_ITEMS = 1000
_MAX_STRING = 8192
_SECRET_TOKENS = frozenset(
    {
        "auth",
        "authorization",
        "credential",
        "credentials",
        "password",
        "passwd",
        "secret",
        "token",
        "cookie",
        "api_key",
        "apikey",
        "private_key",
    }
)


def owner(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or "\x00" in value:
        raise ValueError("owner_id must be a non-empty bounded string")
    return value


def bounded_string(value: object, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{field} must be a non-empty bounded string")
    return value


def canonical_json(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("value must be canonical JSON") from exc
    if len(encoded) > _MAX_PUBLIC_BYTES:
        raise ValueError("payload exceeds 1 MiB")
    return encoded


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def require_digest(value: object, field: str = "plan_digest") -> str:
    text = bounded_string(value, field, maximum=64)
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return text


def bounded_public(value: object) -> Any:
    projected = _project(value, depth=0)
    canonical_json(projected)
    return projected


def strip_secret_values(value: object) -> Any:
    projected = _strip(value, depth=0)
    canonical_json(projected)
    return projected


def _project(value: object, *, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        raise ValueError("payload nesting is too deep")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > _MAX_STRING or "\x00" in value:
            raise ValueError("payload string is invalid or too large")
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_ITEMS:
            raise ValueError("payload object has too many fields")
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            if not isinstance(raw_key, str) or not raw_key or len(raw_key) > 128:
                raise ValueError("payload keys must be bounded strings")
            result[raw_key] = _project(child, depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > _MAX_ITEMS:
            raise ValueError("payload array has too many items")
        return [_project(child, depth=depth + 1) for child in value]
    raise ValueError("payload contains a non-JSON value")


def _strip(value: object, *, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        raise ValueError("payload nesting is too deep")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise ValueError("payload keys must be bounded strings")
            if _secret_key(key) and not _reference_key(key) and not isinstance(child, Mapping):
                continue
            stripped = (
                _project(child, depth=depth + 1)
                if _reference_key(key) and isinstance(child, Mapping)
                else _strip(child, depth=depth + 1)
            )
            if not (_secret_key(key) and not _reference_key(key) and stripped in ({}, [])):
                result[key] = stripped
        return bounded_public(result)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return bounded_public([_strip(child, depth=depth + 1) for child in value])
    return _project(value, depth=depth)


def _canonical_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def _reference_key(key: str) -> bool:
    canonical = _canonical_key(key)
    return canonical.endswith(("_ref", "_refs", "_reference", "_references"))


def _secret_key(key: str) -> bool:
    canonical = _canonical_key(key)
    tokens = set(canonical.split("_"))
    return canonical in _SECRET_TOKENS or bool(tokens & _SECRET_TOKENS) or {"api", "key"} <= tokens


def validate_http_url(
    value: object,
    *,
    field: str,
    https_only: bool = False,
    allowed_hosts: frozenset[str] | None = None,
    allow_loopback: bool = False,
) -> str:
    url = bounded_string(value, field, maximum=2048)
    parsed = urlparse(url)
    schemes = {"https"} if https_only else {"http", "https"}
    if (
        parsed.scheme.lower() not in schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
    ):
        raise ValueError(f"{field} is unsafe")
    hostname = parsed.hostname.rstrip(".").lower()
    if allowed_hosts is not None and hostname not in allowed_hosts:
        raise ValueError(f"{field} host is not allowlisted")
    _validate_host_addresses(hostname, allow_loopback=allow_loopback)
    return url


def _validate_host_addresses(hostname: str, *, allow_loopback: bool) -> None:
    try:
        addresses = {ipaddress.ip_address(hostname)}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            }
        except (OSError, ValueError) as exc:
            raise ValueError("URL host cannot be safely resolved") from exc
    if not addresses:
        raise ValueError("URL host cannot be safely resolved")
    for address in addresses:
        if address.is_loopback and allow_loopback:
            continue
        if not address.is_global:
            raise ValueError("URL host resolves to a non-public address")
