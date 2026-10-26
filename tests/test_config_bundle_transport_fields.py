"""Config Bundles must carry a Server's transport facts across export/import."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from comfyui_mcp_skills.application._phase_o import digest
from comfyui_mcp_skills.application.config_bundles import ConfigBundleService

_OWNER = "owner-a"
_NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)

_FACTS: dict[str, Any] = {
    "local_root": "D:/ComfyUI",
    "output_dir": "./outputs",
    "timeout": 30.0,
    "runtime": {"adapter": "systemd", "unit": "comfyui-local.service"},
}


class _MemoryRepository:
    """Minimal ConfigBundleRepository that keeps content verbatim."""

    def __init__(self, servers: list[dict[str, Any]] | None = None) -> None:
        self.servers = {str(item["server_id"]): dict(item) for item in servers or []}
        self.revision = 0
        self.plans: dict[str, dict[str, Any]] = {}

    def current_revision(self, owner_id: str) -> int:
        return self.revision

    def export_snapshot(self, owner_id: str) -> dict[str, Any]:
        return {
            "servers": [dict(item) for item in self.servers.values()],
            "workflows": [],
            "default_server": next(iter(self.servers), None),
        }

    def save_bundle(self, bundle: dict[str, Any]) -> dict[str, Any]:
        return dict(bundle)

    def get_bundle(self, revision: int, owner_id: str) -> dict[str, Any] | None:
        return None

    def save_import_plan(self, plan: dict[str, Any]) -> None:
        self.plans[plan["plan_id"]] = dict(plan)

    def commit_import_plan(
        self, plan_id: str, plan_digest: str, owner_id: str, *, now: datetime
    ) -> dict[str, Any]:
        plan = self.plans[plan_id]
        if plan["plan_digest"] != plan_digest:
            raise ValueError("digest conflict")
        content = plan["content"]
        self.servers = {
            str(item["server_id"]): dict(item) for item in content.get("servers", [])
        }
        self.revision += 1
        return {
            "bundle_id": "config_bundle_" + digest([owner_id, self.revision]),
            "owner_id": owner_id,
            "version": 1,
            "revision": self.revision,
            "content": content,
            "content_digest": digest(content),
            "created_at": now.isoformat(),
            "resource_uri": f"comfyui://config/bundles/{self.revision}",
        }


def _server(**overrides: Any) -> dict[str, Any]:
    return {
        "server_id": "local",
        "url": "http://127.0.0.1:8188",
        "enabled": True,
        **overrides,
    }


def test_export_keeps_local_root_runtime_timeout_and_output_dir() -> None:
    service = ConfigBundleService(
        _MemoryRepository([_server(**_FACTS)]), clock=lambda: _NOW
    )

    bundle = service.export(_OWNER)

    exported = bundle["servers"][0]
    assert exported["local_root"] == "D:/ComfyUI"
    assert exported["output_dir"] == "./outputs"
    assert exported["timeout"] == 30.0
    assert exported["runtime"] == {"adapter": "systemd", "unit": "comfyui-local.service"}


def test_export_import_round_trip_preserves_every_fact() -> None:
    repository = _MemoryRepository([_server(**_FACTS)])
    service = ConfigBundleService(repository, clock=lambda: _NOW)
    bundle = service.export(_OWNER)

    plan = service.plan_import(bundle, "0", _OWNER)
    committed = service.commit_import(plan["plan_id"], plan["plan_digest"], _OWNER)

    assert committed["servers"][0]["local_root"] == "D:/ComfyUI"
    assert committed["servers"][0]["runtime"]["adapter"] == "systemd"
    assert service.export(_OWNER) == committed


def test_import_keeps_stored_facts_the_bundle_omits() -> None:
    repository = _MemoryRepository([_server(**_FACTS)])
    service = ConfigBundleService(repository, clock=lambda: _NOW)
    bundle = service.export(_OWNER)
    # Simulate an older bundle that predates the transport facts.
    for server in bundle["servers"]:
        for field in _FACTS:
            server.pop(field, None)
    bundle.pop("bundle_digest", None)

    plan = service.plan_import(bundle, "0", _OWNER)

    planned = plan["content"]["servers"][0]
    assert planned["local_root"] == "D:/ComfyUI"
    assert planned["runtime"] == {"adapter": "systemd", "unit": "comfyui-local.service"}
    assert planned["timeout"] == 30.0


def test_import_overwrites_facts_the_bundle_states() -> None:
    repository = _MemoryRepository([_server(**_FACTS)])
    service = ConfigBundleService(repository, clock=lambda: _NOW)
    bundle = service.export(_OWNER)
    bundle["servers"][0]["local_root"] = "E:/OtherComfyUI"
    bundle["servers"][0]["timeout"] = 5
    bundle.pop("bundle_digest", None)

    plan = service.plan_import(bundle, "0", _OWNER)

    planned = plan["content"]["servers"][0]
    assert planned["local_root"] == "E:/OtherComfyUI"
    assert planned["timeout"] == 5  # descending from the bundle, not the store


def test_invalid_transport_facts_are_rejected() -> None:
    service = ConfigBundleService(_MemoryRepository(), clock=lambda: _NOW)
    base = {
        "format_version": 1,
        "revision": "0",
        "servers": [{"server_id": "local", "endpoint_url": "http://127.0.0.1:8188"}],
        "workflows": [],
        "default_server": "local",
    }

    for field, value in (
        ("timeout", True),
        ("timeout", 0.05),
        ("timeout", "30"),
        ("runtime", {f"k{index}": "v" for index in range(9)}),
        ("runtime", {"adapter": 1}),
        ("local_root", ""),
        ("output_dir", "x" * 256),
    ):
        bundle = {**base, "servers": [{**base["servers"][0], field: value}]}
        with pytest.raises(ValueError):
            service.plan_import(bundle, "0", _OWNER)
