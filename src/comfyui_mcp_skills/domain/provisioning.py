"""Canonical, bounded, secret-free values for Phase O control-plane persistence."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

MAX_BUNDLE_BYTES = 1024 * 1024
MAX_PLAN_BYTES = 2 * 1024 * 1024
MAX_CHECKPOINT_BYTES = 64 * 1024
MAX_RESULT_BYTES = 64 * 1024
MAX_DEPENDENCY_ITEMS = 512
MAX_MODEL_BYTES = 20 * 1024 * 1024 * 1024
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_GIT_TAG = re.compile(r"tag:[A-Za-z0-9][A-Za-z0-9._/+\-]{0,126}\Z")
_SECRET_REFERENCE = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{4,}")
_SECRET_VALUE = re.compile(r"(?i)\b(?:sk|api|token)-[A-Za-z0-9_-]{8,}\b")
_FLOATING_VERSIONS = frozenset({"", "*", "latest", "head", "main", "master", "default", "nightly"})
_SENSITIVE_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "secret",
        "token",
    }
)


def canonical_json(value: object, *, field: str = "value", max_bytes: int | None = None) -> str:
    """Encode one JSON value deterministically and optionally enforce a byte ceiling."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be finite JSON") from exc
    if max_bytes is not None and len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field} exceeds the {max_bytes}-byte limit")
    return encoded


def canonical_digest(value: object) -> str:
    """Return the lowercase SHA-256 digest of canonical JSON."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def json_copy(value: object, *, field: str, max_bytes: int) -> Any:
    """Copy a bounded finite JSON value without retaining caller-owned containers."""
    return json.loads(canonical_json(value, field=field, max_bytes=max_bytes))


def require_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def require_public_json(value: object, *, field: str, max_bytes: int) -> Any:
    """Return a bounded JSON copy after rejecting credential names and token-like values."""
    copied = json_copy(value, field=field, max_bytes=max_bytes)
    _reject_secrets(copied, field=field, in_reference_map=False)
    return copied


def validate_server_url(value: object, *, allow_loopback: bool = False) -> str:
    """Validate a persisted server URL; DNS/IP resolution remains a gateway responsibility."""
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("server URL must be a string of at most 2048 characters")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("server URL must use http or https")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("server URL must not contain credentials or a fragment")
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname in {"localhost", "localhost.localdomain"} and not allow_loopback:
        raise ValueError("server URL loopback targets are not allowed")
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        address = None
    if address is not None and not allow_loopback and not address.is_global:
        raise ValueError("server URL private, loopback, and special-use IP targets are not allowed")
    return value.rstrip("/")


def validate_dependency_item(value: object) -> dict[str, Any]:
    """Validate immutable exact source, version, checksum, size, and restart facts."""
    if not isinstance(value, Mapping):
        raise ValueError("dependency item must be an object")
    item = require_public_json(dict(value), field="dependency item", max_bytes=64 * 1024)
    required = {
        "item_id",
        "dependency_id",
        "kind",
        "source_type",
        "source_url",
        "version",
        "checksum",
        "size_bytes",
        "target_dir",
        "restart_required",
        "install_state",
        "license",
    }
    if set(item) != required:
        raise ValueError("dependency item fields conflict with the persisted contract")
    for field in (
        "item_id",
        "dependency_id",
        "kind",
        "source_type",
        "source_url",
        "version",
        "target_dir",
        "license",
    ):
        if not isinstance(item[field], str) or not item[field] or len(item[field]) > 2048:
            raise ValueError(f"dependency item {field} is invalid")
    if item["kind"] not in {"node", "model"}:
        raise ValueError("dependency item kind must be node or model")
    if item["source_type"] not in {"git", "model"}:
        raise ValueError("dependency item source_type must be git or model")
    _validate_source_url(item["source_url"])
    version = item["version"]
    if version.casefold() in _FLOATING_VERSIONS:
        raise ValueError("dependency item version must be immutable")
    if not (_GIT_COMMIT.fullmatch(version) or _GIT_TAG.fullmatch(version)):
        raise ValueError("dependency version must be an exact commit or tag")
    require_sha256(item["checksum"], field="dependency item checksum")
    size = item["size_bytes"]
    maximum = MAX_MODEL_BYTES if item["kind"] == "model" else MAX_PACKAGE_BYTES
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= maximum:
        raise ValueError("dependency item size exceeds the allowed bound")
    if not isinstance(item["restart_required"], bool):
        raise ValueError("dependency item restart_required must be boolean")
    if item["install_state"] not in {"missing", "installed", "update_available"}:
        raise ValueError("dependency install_state is unsupported")
    if item["kind"] == "model" and item["source_type"] != "model":
        raise ValueError("model dependencies must use a pinned model source")
    return item


def validate_dependency_items(values: object) -> list[dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ValueError("dependency plan items must be an array")
    if not 1 <= len(values) <= MAX_DEPENDENCY_ITEMS:
        raise ValueError("dependency plan item count exceeds the allowed bound")
    result = [validate_dependency_item(value) for value in values]
    identifiers = [str(item["item_id"]) for item in result]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("dependency plan item IDs must be unique")
    return result


def _validate_source_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("dependency source URL must use https")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("dependency source URL must not contain credentials or a fragment")
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname in {"localhost", "localhost.localdomain"}:
        raise ValueError("dependency source URL loopback targets are not allowed")
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        return
    if not address.is_global:
        raise ValueError(
            "dependency source URL private, loopback, and special-use IPs are not allowed"
        )


def _reject_secrets(value: Any, *, field: str, in_reference_map: bool) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
            reference_key = normalized == "secret_refs" or normalized.endswith(
                ("_secret_ref", "_env")
            )
            if normalized in _SENSITIVE_NAMES or any(
                normalized.endswith(f"_{name}") for name in _SENSITIVE_NAMES
            ):
                if not (reference_key or in_reference_map):
                    raise ValueError(f"{field} contains a secret-bearing field")
            _reject_secrets(
                item,
                field=field,
                in_reference_map=in_reference_map or normalized == "secret_refs" or reference_key,
            )
        return
    if isinstance(value, list):
        for item in value:
            _reject_secrets(item, field=field, in_reference_map=in_reference_map)
        return
    if isinstance(value, str):
        if _BEARER.search(value) or _SECRET_VALUE.search(value):
            raise ValueError(f"{field} contains a credential-like value")
        if in_reference_map and _SECRET_REFERENCE.fullmatch(value) is None:
            raise ValueError(f"{field} secret references must be environment-style names")
