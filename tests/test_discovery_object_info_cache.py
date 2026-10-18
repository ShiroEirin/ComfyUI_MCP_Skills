"""DiscoveryService object_info caching: one inventory fetch per server per TTL.

``/object_info`` is several megabytes on a real install, so node and blueprint
discovery must not re-download it on every call.
"""

from __future__ import annotations

from typing import Any

import pytest

from comfyui_mcp_skills.application import discovery as discovery_module
from comfyui_mcp_skills.application.discovery import DiscoveryService

INVENTORY: dict[str, Any] = {
    "KSampler": {"display_name": "KSampler", "category": "sampling"},
    "CLIPTextEncode": {"display_name": "CLIP Text Encode", "category": "conditioning"},
}


class _Registry:
    def __init__(self, server_ids: list[str]) -> None:
        self._server_ids = server_ids

    def connection(self, server_id: str) -> dict[str, Any]:
        if server_id not in self._server_ids:
            raise LookupError(server_id)
        return {"id": server_id, "url": "http://127.0.0.1:8188"}


class _Gateway:
    def __init__(self, inventory: dict[str, Any]) -> None:
        self.inventory = inventory
        self.object_info_calls = 0

    def get_object_info(self) -> dict[str, Any]:
        self.object_info_calls += 1
        return self.inventory


def _service(gateway: _Gateway, server_ids: list[str] | None = None) -> DiscoveryService:
    return DiscoveryService(_Registry(server_ids or ["local"]), lambda _config: gateway)


def test_nodes_reuses_object_info_within_ttl() -> None:
    gateway = _Gateway(INVENTORY)
    service = _service(gateway)

    first = service.nodes("local")
    second = service.nodes("local")

    assert gateway.object_info_calls == 1
    assert first == second


def test_blueprint_reuses_object_info_fetched_by_nodes() -> None:
    gateway = _Gateway(INVENTORY)
    service = _service(gateway)

    service.nodes("local")
    result = service.blueprint("local", query="sampler")

    assert gateway.object_info_calls == 1
    assert result["total_matches"] == 1


def test_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _Gateway(INVENTORY)
    service = _service(gateway)

    service.nodes("local")
    monkeypatch.setattr(discovery_module, "_OBJECT_INFO_TTL_SECONDS", -1.0)
    service.nodes("local")

    assert gateway.object_info_calls == 2


def test_each_server_keeps_its_own_entry() -> None:
    gateways = {"local": _Gateway(INVENTORY), "second": _Gateway({"OnlyNode": {}})}
    service = DiscoveryService(
        _Registry(["local", "second"]), lambda config: gateways[str(config["id"])]
    )

    service.nodes("local")
    service.nodes("second")
    service.nodes("local")

    assert gateways["local"].object_info_calls == 1
    assert gateways["second"].object_info_calls == 1


def test_cached_inventory_is_not_shared_between_services() -> None:
    first_gateway = _Gateway(INVENTORY)
    second_gateway = _Gateway(INVENTORY)

    _service(first_gateway).nodes("local")
    _service(second_gateway).nodes("local")

    assert first_gateway.object_info_calls == 1
    assert second_gateway.object_info_calls == 1
