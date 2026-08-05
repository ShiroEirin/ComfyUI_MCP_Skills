"""Versioned, secret-free Config Bundle export and two-phase import."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from comfyui_mcp_skills.application._phase_o import (
    bounded_public,
    bounded_string,
    digest,
    owner,
    require_digest,
    strip_secret_values,
    validate_http_url,
)
from comfyui_mcp_skills.application.provisioning_ports import ConfigBundleRepository
from comfyui_mcp_skills.domain.identifiers import validate_identifier

_BUNDLE_VERSION = 1
_PLAN_TTL = timedelta(hours=1)


class ConfigBundleService:
    def __init__(
        self, repository: ConfigBundleRepository, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def export(self, owner_id: str, revision: str = "") -> dict[str, Any]:
        owner_id = owner(owner_id)
        current = self._repository.current_revision(owner_id)
        if isinstance(current, bool) or not isinstance(current, int) or current < 0:
            raise ValueError("Configuration revision is invalid")
        if revision and _revision(revision) != current:
            existing = self._repository.get_bundle(_revision(revision), owner_id)
            if existing is None:
                raise LookupError("Configuration revision was not found")
            return _public_bundle(strip_secret_values(existing))
        snapshot = self._repository.export_snapshot(owner_id)
        if not isinstance(snapshot, Mapping):
            raise ValueError("Configuration snapshot is invalid")
        content = _normalize_content(strip_secret_values(snapshot))
        content_digest = digest(content)
        existing = self._repository.get_bundle(current, owner_id)
        if existing is not None and existing.get("content_digest") == content_digest:
            return _public_bundle(strip_secret_values(existing))
        bundle = {
            "bundle_id": "config_bundle_" + digest([owner_id, current, content_digest]),
            "owner_id": owner_id,
            "version": _BUNDLE_VERSION,
            "revision": current,
            "content": content,
            "content_digest": content_digest,
            "created_at": _time(self._clock()),
            "resource_uri": f"comfyui://config/bundles/{current}",
        }
        return _public_bundle(strip_secret_values(self._repository.save_bundle(bundle)))

    def plan_import(
        self, bundle: dict[str, Any], expected_revision: str, owner_id: str
    ) -> dict[str, Any]:
        owner_id = owner(owner_id)
        if not isinstance(bundle, dict) or strip_secret_values(bundle) != bundle:
            raise ValueError("Config Bundle is invalid or contains secret values")
        public_format = "format_version" in bundle
        content: Any
        if public_format:
            allowed = {
                "format_version",
                "revision",
                "servers",
                "workflows",
                "default_server",
                "bundle_digest",
                "created_at",
                "resource_uri",
            }
            if set(bundle) - allowed or bundle.get("format_version") != _BUNDLE_VERSION:
                raise ValueError("Config Bundle version or fields are unsupported")
            content = {
                "servers": bundle.get("servers", []),
                "workflows": bundle.get("workflows", []),
                "default_server": bundle.get("default_server"),
            }
            normalized = _normalize_content(content)
            supplied_digest = bundle.get("bundle_digest")
        else:
            allowed = {
                "bundle_id",
                "owner_id",
                "version",
                "revision",
                "content",
                "content_digest",
                "created_at",
                "resource_uri",
            }
            if set(bundle) - allowed or bundle.get("version") != _BUNDLE_VERSION:
                raise ValueError("Config Bundle version or fields are unsupported")
            content = bundle.get("content")
            if not isinstance(content, Mapping):
                raise ValueError("Config Bundle content is invalid")
            normalized = _normalize_content(content)
            supplied_digest = bundle.get("content_digest")
        source_digest = digest(normalized)
        if (
            supplied_digest is not None
            and require_digest(supplied_digest, "bundle_digest") != source_digest
        ):
            raise ValueError("Config Bundle content digest does not match")
        expected = _revision(expected_revision)
        current = self._repository.current_revision(owner_id)
        if expected != current:
            raise ValueError("Config Bundle expected_revision conflicts with current revision")
        summary = _merge_summary(
            strip_secret_values(self._repository.export_snapshot(owner_id)), normalized
        )
        now = self._clock()
        immutable = {
            "owner_id": owner_id,
            "expected_revision": expected,
            "bundle_version": _BUNDLE_VERSION,
            "source_digest": source_digest,
            "content": normalized,
            "merge_summary": summary,
            "created_at": _time(now),
            "expires_at": _time(now + _PLAN_TTL),
        }
        plan_digest = digest(immutable)
        plan = {
            "plan_id": "config_import_plan_" + plan_digest,
            "plan_digest": plan_digest,
            **immutable,
            "resource_uri": f"comfyui://config/bundles/{expected}",
        }
        self._repository.save_import_plan(plan)
        return bounded_public(plan)

    def commit_import(self, plan_id: str, plan_digest: str, owner_id: str) -> dict[str, Any]:
        plan_id = bounded_string(plan_id, "plan_id", maximum=128)
        plan_digest = require_digest(plan_digest)
        owner_id = owner(owner_id)
        try:
            result = self._repository.commit_import_plan(
                plan_id, plan_digest, owner_id, now=self._clock()
            )
        except LookupError as exc:
            raise LookupError("Config import Plan was not found") from exc
        except ValueError as exc:
            raise ValueError(
                "Config import Plan conflicts with its owner, digest, or revision"
            ) from exc
        return _public_bundle(strip_secret_values(result))


def _public_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    content = bundle.get("content", {})
    if not isinstance(content, Mapping):
        raise ValueError("Stored Config Bundle content is invalid")
    content = strip_secret_values(content)
    revision = _revision(bundle.get("revision"))
    result = {
        "format_version": _BUNDLE_VERSION,
        "revision": str(revision),
        "resource_uri": f"comfyui://config/bundles/{revision}",
        "bundle_digest": require_digest(bundle.get("content_digest"), "content_digest"),
        "servers": bounded_public(content.get("servers", [])),
        "workflows": bounded_public(content.get("workflows", [])),
        "default_server": content.get("default_server"),
        "created_at": str(bundle.get("created_at", ""))[:64],
    }
    return bounded_public(result)


def _normalize_content(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) - {"servers", "workflows", "default_server"}:
        raise ValueError("Config Bundle content fields are unsupported")
    raw_servers = value.get("servers", [])
    raw_workflows = value.get("workflows", [])
    if not isinstance(raw_servers, list) or len(raw_servers) > 64:
        raise ValueError("Config Bundle servers are invalid")
    if not isinstance(raw_workflows, list) or len(raw_workflows) > 256:
        raise ValueError("Config Bundle workflows are invalid")
    servers: list[dict[str, Any]] = []
    seen_servers: set[str] = set()
    for raw in raw_servers:
        if not isinstance(raw, Mapping):
            raise ValueError("Config Bundle Server is invalid")
        server_id = validate_identifier(str(raw.get("server_id", "")), field="server_id")
        if server_id in seen_servers:
            raise ValueError("Config Bundle contains duplicate Servers")
        seen_servers.add(server_id)
        endpoint = validate_http_url(
            raw.get("endpoint_url", raw.get("url")), field="endpoint_url", allow_loopback=True
        )
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("Config Bundle Server enabled must be boolean")
        item: dict[str, Any] = {
            "server_id": server_id,
            "endpoint_url": endpoint,
            "enabled": enabled,
            "is_default": bool(raw.get("is_default", False)),
        }
        display_name = raw.get("display_name", raw.get("name"))
        if display_name is not None:
            item["display_name"] = bounded_string(display_name, "display_name", maximum=256)
        refs = raw.get("secret_refs")
        if refs is not None:
            if not isinstance(refs, Mapping) or len(refs) > 16:
                raise ValueError("Config Bundle secret_refs are invalid")
            item["secret_refs"] = {
                bounded_string(key, "secret reference key", maximum=64): bounded_string(
                    reference, "secret reference", maximum=256
                )
                for key, reference in refs.items()
            }
        servers.append(item)
    default_server = value.get("default_server")
    if default_server is not None:
        default_server = validate_identifier(str(default_server), field="default_server")
        if default_server not in seen_servers:
            raise ValueError("Config Bundle default Server is missing")
    defaults = [item["server_id"] for item in servers if item["is_default"]]
    if len(defaults) > 1 or (defaults and default_server not in {None, defaults[0]}):
        raise ValueError("Config Bundle default Server conflicts")
    default_server = default_server or (defaults[0] if defaults else None)
    for item in servers:
        item["is_default"] = item["server_id"] == default_server
    workflows: list[dict[str, Any]] = []
    seen_workflows: set[tuple[str, str]] = set()
    for raw in raw_workflows:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("enabled"), bool):
            raise ValueError("Config Bundle Workflow is invalid")
        server_id = validate_identifier(str(raw.get("server_id", "")), field="server_id")
        workflow_id = validate_identifier(str(raw.get("workflow_id", "")), field="workflow_id")
        identity = (server_id, workflow_id)
        if server_id not in seen_servers or identity in seen_workflows:
            raise ValueError("Config Bundle Workflow identity is invalid")
        seen_workflows.add(identity)
        workflows.append(
            {"server_id": server_id, "workflow_id": workflow_id, "enabled": raw["enabled"]}
        )
    return bounded_public(
        {"servers": servers, "workflows": workflows, "default_server": default_server}
    )


def _revision(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("revision must be a non-negative integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value and value.isascii() and value.isdigit():
        result = int(value)
    else:
        raise ValueError("revision must be a non-negative integer")
    if not 0 <= result <= 2**63 - 1:
        raise ValueError("revision must be a non-negative integer")
    return result


def _merge_summary(current: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    current_keys, incoming_keys = set(current), set(incoming)
    changed = {key for key in current_keys & incoming_keys if current.get(key) != incoming.get(key)}
    groups = (incoming_keys - current_keys, changed, current_keys - incoming_keys)
    return {
        "add": sorted(groups[0])[:100],
        "change": sorted(groups[1])[:100],
        "remove": sorted(groups[2])[:100],
        "truncated": any(len(group) > 100 for group in groups),
    }


def _time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return an aware datetime")
    return value.astimezone(timezone.utc).isoformat()
