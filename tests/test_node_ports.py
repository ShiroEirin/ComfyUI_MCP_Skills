"""``comfyui.node.ports`` must expose usable widget facts, not just link types."""

from __future__ import annotations

from typing import Any

from comfyui_mcp_skills.application.discovery import DiscoveryService

_KSAMPLER = {
    "display_name": "KSampler",
    "input": {
        "required": {
            "model": ["MODEL"],
            "seed": ["INT", {"default": 0, "min": 0, "max": 18446744073709551615}],
            "steps": ["INT", {"default": 20, "min": 1, "max": 10000, "step": 1}],
            "cfg": ["FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0, "step": 0.1}],
            "sampler_name": [
                ["euler", "euler_ancestral", "dpmpp_2m"],
                {"default": "euler"},
            ],
            "scheduler": ["COMBO", {"options": ["normal", "karras"], "default": "normal"}],
        },
        "optional": {
            "denoise": ["FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "forceInput": True}],
        },
    },
    "output": ["LATENT"],
    "output_name": ["LATENT"],
}

_VSR = {
    "display_name": "RTXVideoSuperResolution",
    "input": {
        "required": {
            "images": ["IMAGE"],
            "quality": [
                "COMFY_DYNAMICCOMBO_V3",
                {"options": ["LOW", "MEDIUM", "HIGH", "ULTRA"], "default": "HIGH"},
            ],
        },
        "optional": {
            "engine": ["COMFY_DYNAMICCOMBO_V3", {"default": "auto"}],
        },
    },
    "output": ["IMAGE"],
    "output_name": ["IMAGE"],
}


class _Servers:
    def connection(self, _server_id: str) -> dict[str, Any]:
        return {"url": "http://127.0.0.1:8188"}


class _Gateway:
    def __init__(self, nodes: dict[str, dict[str, Any]]) -> None:
        self._nodes = nodes

    def get_object_info_node(self, node_class: str) -> dict[str, Any] | None:
        return self._nodes.get(node_class)


def _ports(node_class: str) -> dict[str, Any]:
    nodes = {"KSampler": _KSAMPLER, "RTXVideoSuperResolution": _VSR}
    service = DiscoveryService(_Servers(), lambda _config: _Gateway(nodes))
    return service.node_ports("local", node_class)


def _by_name(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {port["name"]: port for port in result["inputs"]}


def test_ksampler_widgets_carry_defaults_and_numeric_bounds() -> None:
    result = _ports("KSampler")
    ports = _by_name(result)

    # Link ports keep the original contract and stay connectable.
    assert ports["model"] == {
        "name": "model",
        "type": "MODEL",
        "required": True,
        "widget": False,
        "connectable": True,
    }
    # Numeric widgets expose default + bounds instead of being dropped.
    steps = ports["steps"]
    assert steps["type"] == "INT"
    assert steps["widget"] is True
    assert steps["connectable"] is False
    assert steps["default"] == 20
    assert steps["current_value"] == 20
    assert (steps["min"], steps["max"], steps["step"]) == (1, 10000, 1)
    cfg = ports["cfg"]
    assert cfg["default"] == 8.0
    assert (cfg["min"], cfg["max"], cfg["step"]) == (0.0, 100.0, 0.1)
    assert result["widget_count"] == 6  # seed/steps/cfg/sampler_name/scheduler/denoise
    assert [port["name"] for port in result["inputs"]] == sorted(ports)


def test_combo_lists_expose_options_and_force_input_becomes_connectable() -> None:
    ports = _by_name(_ports("KSampler"))

    sampler = ports["sampler_name"]
    assert sampler["type"] == "COMBO"
    assert sampler["options"] == ["euler", "euler_ancestral", "dpmpp_2m"]
    assert sampler["enum"] == sampler["options"]
    assert sampler["default"] == "euler"
    assert sampler["widget"] is True
    # forceInput turns a widget slot into a link target.
    assert ports["denoise"]["connectable"] is True
    assert ports["denoise"]["default"] == 1.0


def test_string_combo_token_reports_readable_combo() -> None:
    ports = _by_name(_ports("KSampler"))

    scheduler = ports["scheduler"]
    assert scheduler["type"] == "COMBO"
    assert scheduler["raw_type"] == "COMBO"
    assert scheduler["options"] == ["normal", "karras"]
    assert "dynamic" not in scheduler


def test_dynamic_combo_is_readable_and_never_silently_unusable() -> None:
    result = _ports("RTXVideoSuperResolution")
    ports = _by_name(result)

    quality = ports["quality"]
    assert quality["type"] == "COMBO"  # not the raw pseudo type
    assert quality["raw_type"] == "COMFY_DYNAMICCOMBO_V3"
    assert quality["dynamic"] is True
    assert quality["widget"] is True
    assert quality["connectable"] is False
    assert quality["options"] == ["LOW", "MEDIUM", "HIGH", "ULTRA"]
    assert quality["default"] == "HIGH"

    engine = ports["engine"]
    assert engine["dynamic"] is True
    assert engine["type"] == "COMBO"
    assert "options" not in engine
    assert engine["options_missing"] == "dynamic_combo_options_unavailable"


def test_outputs_report_index_name_type_and_stay_backward_compatible() -> None:
    result = _ports("KSampler")

    assert result["server_id"] == "local"
    assert result["node_class"] == "KSampler"
    assert result["outputs"] == [
        {"index": 0, "name": "LATENT", "type": "LATENT", "connectable": True}
    ]


def test_unknown_shapes_do_not_fabricate_facts() -> None:
    node = {
        "display_name": "Odd",
        "input": {
            "required": {
                "mystery": [None, {"unknown": object()}],
                "empty": [],
            }
        },
        "output": [],
    }
    service = DiscoveryService(_Servers(), lambda _config: _Gateway({"Odd": node}))

    ports = {port["name"]: port for port in service.node_ports("local", "Odd")["inputs"]}

    assert ports["mystery"]["type"] == "None"
    assert ports["mystery"]["widget"] is False
    assert ports["mystery"]["connectable"] is True
    assert "default" not in ports["mystery"]
    assert "current_value" not in ports["mystery"]
    assert "empty" not in ports  # empty specs stay unprojected
