"""Workflow argument schema and validation contracts."""

from __future__ import annotations

import pytest

from comfyui_mcp_skills.domain.workflow_schema import (
    WorkflowArgumentsError,
    build_input_schema,
    validate_arguments,
)

PARAMETERS = {
    "prompt": {"type": "string", "required": True, "description": "Prompt"},
    "steps": {"type": "int", "minimum": 1, "maximum": 100, "default": 20},
    "sampler": {"type": "string", "enum": ["euler", "dpmpp_2m"]},
    "image": {"type": "image", "required": False},
}


def test_workflow_schema_is_closed_json_schema_2020_12() -> None:
    schema = build_input_schema(PARAMETERS)

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["prompt"]
    assert schema["properties"]["steps"]["type"] == "integer"
    assert schema["properties"]["steps"]["minimum"] == 1
    assert schema["properties"]["sampler"]["enum"] == ["euler", "dpmpp_2m"]
    assert schema["properties"]["image"]["type"] == "string"


def test_validation_rejects_missing_unknown_and_wrong_type() -> None:
    with pytest.raises(WorkflowArgumentsError, match="prompt"):
        validate_arguments(PARAMETERS, {"steps": 20})

    with pytest.raises(WorkflowArgumentsError, match="unexpected"):
        validate_arguments(PARAMETERS, {"prompt": "cat", "unexpected": True})

    with pytest.raises(WorkflowArgumentsError, match="steps"):
        validate_arguments(PARAMETERS, {"prompt": "cat", "steps": "many"})


def test_validation_accepts_valid_arguments() -> None:
    validate_arguments(
        PARAMETERS,
        {"prompt": "cat", "steps": 30, "sampler": "euler"},
    )


def test_build_input_schema_rejects_invalid_json_schema_metadata() -> None:
    with pytest.raises(ValueError, match="Invalid JSON Schema"):
        build_input_schema({"sampler": {"type": "string", "enum": "not-an-array"}})
