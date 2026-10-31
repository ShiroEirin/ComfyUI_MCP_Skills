"""COMFY_DYNAMICCOMBO_V3 inputs must validate like ComfyUI, not like links.

ComfyUI keeps a dynamic combo's selection in the input itself and carries the
selected option's inputs as ``<field>.<sub>`` keys. Treating that widget as a
link type made every graph containing an RTXVideoSuperResolution node
uneditable (WORKFLOW_CHANGE_INVALID / requires_manual_review).
"""

from __future__ import annotations

from typing import Any

from comfyui_mcp_skills.application.workflow_graph import WorkflowValidationService

OBJECT_INFO: dict[str, Any] = {
    "LoadImage": {
        "input": {"required": {"image": [["a.png", "b.png"], {}]}},
        "output": ["IMAGE", "MASK"],
    },
    "RTXVideoSuperResolution": {
        "input": {
            "required": {
                "images": ["IMAGE", {}],
                "resize_type": [
                    "COMFY_DYNAMICCOMBO_V3",
                    {
                        "tooltip": "scale by multiplier or target dimensions",
                        "options": [
                            {
                                "key": "scale by multiplier",
                                "inputs": {"required": {"scale": ["FLOAT", {"default": 2.0}]}},
                            },
                            {
                                "key": "target dimensions",
                                "inputs": {
                                    "required": {
                                        "width": ["INT", {"default": 1920}],
                                        "height": ["INT", {"default": 1080}],
                                    }
                                },
                            },
                        ],
                    },
                ],
                "quality": [
                    "COMBO",
                    {"options": ["LOW", "ULTRA"], "default": "ULTRA"},
                ],
                "strength": ["FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0}],
                "image_encoding": [
                    "COMBO",
                    {"options": ["8-bit RGB", "10-bit RGB (RGB10A2)"], "default": "8-bit RGB"},
                ],
            }
        },
        "output": ["IMAGE"],
    },
    "SaveImage": {
        "input": {"required": {"images": ["IMAGE", {}], "filename_prefix": ["STRING", {}]}},
        "output": [],
    },
}


def _graph(**overrides: Any) -> dict[str, dict[str, Any]]:
    node_inputs: dict[str, Any] = {
        "images": ["1", 0],
        "resize_type": "scale by multiplier",
        "resize_type.scale": 2.0,
        "quality": "ULTRA",
        "strength": 1.0,
        "image_encoding": "8-bit RGB",
    }
    node_inputs.update(overrides)
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
        "10": {"class_type": "RTXVideoSuperResolution", "inputs": node_inputs},
        "20": {
            "class_type": "SaveImage",
            "inputs": {"images": ["10", 0], "filename_prefix": "unit"},
        },
    }


def _validate(graph: dict[str, Any]) -> dict[str, Any]:
    return WorkflowValidationService().validate_api(graph, OBJECT_INFO)


def _codes(result: dict[str, Any]) -> set[str]:
    return {str(issue["code"]) for issue in result["issues"]}


def test_dynamic_combo_selection_is_not_treated_as_a_link() -> None:
    result = _validate(_graph())

    assert result["valid"] is True, result["issues"]
    assert result["issues"] == []


def test_dotted_sub_input_is_a_known_port() -> None:
    result = _validate(_graph())

    assert "unknown_input_port" not in _codes(result)


def test_unknown_dynamic_option_is_rejected() -> None:
    result = _validate(_graph(resize_type="pixel soup"))

    assert "invalid_enum_value" in _codes(result)
    assert result["valid"] is False


def test_dotted_sub_input_type_is_still_checked() -> None:
    result = _validate(_graph(**{"resize_type.scale": "two"}))

    assert "invalid_input_type" in _codes(result)
    issues = [issue for issue in result["issues"] if issue.get("field") == "resize_type.scale"]
    assert issues, result["issues"]


def test_target_dimension_sub_inputs_of_the_selected_option_are_accepted() -> None:
    graph = _graph(**{"resize_type": "target dimensions"})
    graph["10"]["inputs"].pop("resize_type.scale")
    graph["10"]["inputs"]["resize_type.width"] = 1280
    graph["10"]["inputs"]["resize_type.height"] = 720

    result = _validate(graph)

    assert result["valid"] is True, result["issues"]


def test_missing_required_input_is_still_reported() -> None:
    graph = _graph()
    graph["10"]["inputs"].pop("strength")

    result = _validate(graph)

    assert "missing_required_input" in _codes(result)
    assert any(issue.get("field") == "strength" for issue in result["issues"])
