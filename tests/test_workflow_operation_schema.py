"""The workflow change ``operations`` array is a closed, model-checkable schema."""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from comfyui_mcp_skills.adapters.mcp.tooling import fixed_tools, workflow_operations_schema

# Exactly the operation kinds accepted by workflow_change._apply_operation.
SUPPORTED_OPERATIONS = {
    "add_node",
    "remove_node",
    "replace_node",
    "set_input",
    "connect",
    "disconnect",
    "expose_parameter",
    "insert_subgraph",
    "extract_subgraph",
    "apply_recipe",
}

CHANGE_TOOLS = (
    "comfyui.admin.workflow.change.plan",
    "comfyui.admin.workflow.change.commit",
)


def _branches() -> list[dict[str, Any]]:
    return workflow_operations_schema()["items"]["oneOf"]


def _item_validator() -> Draft202012Validator:
    return Draft202012Validator(workflow_operations_schema()["items"])


def test_schema_is_valid_and_covers_every_implemented_operation() -> None:
    schema = workflow_operations_schema()
    Draft202012Validator.check_schema(schema)

    branches = schema["items"]["oneOf"]
    operations = {branch["properties"]["op"]["enum"][0] for branch in branches}
    assert operations == SUPPORTED_OPERATIONS
    # insert_subgraph carries one branch per source form (nodes | subgraph).
    assert len(branches) == len(SUPPORTED_OPERATIONS) + 1
    for branch in branches:
        assert branch["additionalProperties"] is False
        assert "op" in branch["required"]


def test_every_operation_ships_a_valid_example() -> None:
    item_validator = _item_validator()
    for branch in _branches():
        examples = branch.get("examples")
        assert isinstance(examples, list) and examples, branch["description"]
        for example in examples:
            Draft202012Validator(branch).validate(example)
            item_validator.validate(example)


def test_array_level_examples_validate() -> None:
    schema = workflow_operations_schema()
    validator = Draft202012Validator(schema)
    examples = schema["examples"]
    assert examples
    for example in examples:
        validator.validate(example)


def test_unknown_operation_is_rejected_by_the_schema() -> None:
    validator = _item_validator()
    assert not validator.is_valid({"op": "explode_graph"})
    assert not validator.is_valid({"op": "set_inputs", "node_id": "1"})


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "add_node", "node_id": "9"},  # missing class_type/inputs
        {"op": "remove_node"},  # missing node_id
        {"op": "set_input", "node_id": "1", "field": "text"},  # missing value
        {"op": "connect", "source_node_id": "1", "target_node_id": "2"},  # missing parts
        {"op": "expose_parameter", "node_id": "1", "field": "text"},  # missing name
        {"op": "extract_subgraph", "name": "part", "node_ids": []},  # empty selection
        {"op": "apply_recipe", "recipe_id": "change_checkpoint.v1"},  # missing arguments
    ],
)
def test_missing_required_fields_are_rejected(operation: dict[str, Any]) -> None:
    assert not _item_validator().is_valid(operation)


def test_unexpected_fields_are_rejected() -> None:
    validator = _item_validator()
    # _apply_operation refuses unknown fields per operation.
    assert not validator.is_valid({"op": "remove_node", "node_id": "1", "inputs": {}})
    assert not validator.is_valid(
        {"op": "set_input", "node_id": "1", "field": "text", "value": "x", "class_type": "Text"}
    )


def test_insert_subgraph_requires_exactly_one_source() -> None:
    validator = _item_validator()
    assert validator.is_valid(
        {"op": "insert_subgraph", "id_prefix": "p", "nodes": {"a": {}}}
    )
    assert validator.is_valid(
        {"op": "insert_subgraph", "id_prefix": "p", "subgraph": "upscale_chain"}
    )
    # Neither source, or both at once, is rejected.
    assert not validator.is_valid({"op": "insert_subgraph", "id_prefix": "p"})
    assert not validator.is_valid(
        {
            "op": "insert_subgraph",
            "id_prefix": "p",
            "nodes": {"a": {}},
            "subgraph": "upscale_chain",
        }
    )


def test_set_input_rejects_connection_shaped_values() -> None:
    validator = _item_validator()
    assert not validator.is_valid(
        {"op": "set_input", "node_id": "1", "field": "text", "value": ["4", 0]}
    )
    assert validator.is_valid(
        {"op": "set_input", "node_id": "1", "field": "text", "value": "plain"}
    )


def test_apply_recipe_lists_registered_recipes_and_pinning_arguments() -> None:
    from comfyui_mcp_skills.application.workflow_recipes import RECIPE_CONTRACTS

    branch = next(
        item
        for item in _branches()
        if item["properties"]["op"]["enum"][0] == "apply_recipe"
    )
    assert branch["properties"]["recipe_id"]["enum"] == sorted(RECIPE_CONTRACTS)
    assert not _item_validator().is_valid(
        {"op": "apply_recipe", "recipe_id": "not_a_recipe.v1", "arguments": {}}
    )
    arguments_examples = branch["properties"]["arguments"]["examples"]
    assert any("node_id" in example for example in arguments_examples)
    assert any("node_ids" in example for example in arguments_examples)


def test_change_tools_publish_the_operation_schema() -> None:
    tools = {tool.name: tool for tool in fixed_tools()}
    for name in CHANGE_TOOLS:
        assert tools[name].input_schema["properties"]["operations"] == (
            workflow_operations_schema()
        )


def test_implementation_agrees_on_the_operation_names() -> None:
    """Cross-check the schema against the real dispatcher, both directions."""
    from comfyui_mcp_skills.application.workflow_change import _apply_operation

    with pytest.raises(ValueError, match="op is unsupported"):
        _apply_operation({}, {}, {"op": "explode_graph"}, index=0)

    for operation in sorted(SUPPORTED_OPERATIONS):
        with pytest.raises((ValueError, TypeError)) as raised:
            _apply_operation({}, {}, {"op": operation}, index=0)
        assert "op is unsupported" not in str(raised.value), operation
