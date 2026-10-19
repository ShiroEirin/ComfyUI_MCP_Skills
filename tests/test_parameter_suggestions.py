"""Tests for workflow parameter suggestions."""

from __future__ import annotations

from comfyui_mcp_skills.domain.parameter_suggestions import (
    MAX_SUGGESTIONS,
    suggest_parameters,
)


def _graph() -> dict:
    return {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "base.safetensors"},
        },
        "10": {
            "class_type": "LoraLoader",
            "inputs": {
                "lora_name": "style.safetensors",
                "strength_model": 0.8,
                "strength_clip": 0.8,
                "model": ["4", 0],
            },
        },
        "7": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 1,
                "steps": 20,
                "cfg": 7.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["10", 0],
            },
        },
        "99": {"class_type": "UnknownNode", "inputs": {"foo": 1}},
    }


def _by_target(suggestions: list[dict]) -> dict[tuple[str, str], dict]:
    return {(item["node_id"], item["field"]): item for item in suggestions}


def test_suggests_loader_and_sampler_inputs() -> None:
    suggestions = _by_target(suggest_parameters(_graph(), {}))

    assert ("4", "ckpt_name") in suggestions
    assert suggestions[("4", "ckpt_name")]["suggested_name"] == (
        "checkpointloadersimple_ckpt_name_4"
    )
    assert ("10", "lora_name") in suggestions
    assert ("10", "strength_model") in suggestions
    assert ("7", "steps") in suggestions
    assert ("7", "cfg") in suggestions


def test_skips_wired_inputs() -> None:
    suggestions = _by_target(suggest_parameters(_graph(), {}))

    # "model" is a connection, but it is also not in the suggestion table.
    assert ("10", "model") not in suggestions
    assert ("7", "model") not in suggestions


def test_skips_already_exposed_inputs() -> None:
    parameters = {"lora_name": {"node_id": "10", "field": "lora_name"}}

    suggestions = _by_target(suggest_parameters(_graph(), parameters))

    assert ("10", "lora_name") not in suggestions
    assert ("10", "strength_model") in suggestions


def test_ignores_unknown_node_classes() -> None:
    suggestions = _by_target(suggest_parameters(_graph(), {}))

    assert ("99", "foo") not in suggestions


def test_respects_the_suggestion_budget() -> None:
    graph = {
        str(index): {
            "class_type": "KSampler",
            "inputs": {
                "seed": index,
                "steps": 20,
                "cfg": 7.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
            },
        }
        for index in range(20)
    }

    suggestions = suggest_parameters(graph, {})

    assert len(suggestions) == MAX_SUGGESTIONS
