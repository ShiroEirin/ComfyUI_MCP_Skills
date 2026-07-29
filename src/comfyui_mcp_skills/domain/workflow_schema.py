"""Translate workflow parameter metadata into strict JSON Schema."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .errors import WorkflowArgumentsError


WorkflowArgumentsError = WorkflowArgumentsError

_TYPE_MAP = {
    "int": "integer",
    "integer": "integer",
    "float": "number",
    "number": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "image": "string",
    "audio": "string",
    "video": "string",
    "string": "string",
}
_SCHEMA_FIELDS = (
    "description",
    "default",
    "enum",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "pattern",
    "format",
)


def normalize_parameters(schema: Any) -> dict[str, dict[str, Any]]:
    """Normalize current and legacy workflow parameter metadata."""
    if not isinstance(schema, dict):
        raise ValueError("Workflow schema must be an object")
    raw_parameters = schema.get("parameters", {})
    raw_ui_parameters = schema.get("ui_parameters", {})
    if not isinstance(raw_parameters, dict):
        raise ValueError("Workflow schema parameters must be an object")
    if not isinstance(raw_ui_parameters, dict):
        raise ValueError("Workflow schema ui_parameters must be an object")

    parameters: dict[str, dict[str, Any]] = {}
    for name, metadata in raw_parameters.items():
        if not isinstance(name, str) or not name or not isinstance(metadata, dict):
            raise ValueError("Workflow parameter metadata must be an object")
        if name == "_execution":
            raise ValueError('Workflow parameter name "_execution" is reserved')
        parameters[name] = dict(metadata)

    for key, metadata in raw_ui_parameters.items():
        if not isinstance(key, str) or not isinstance(metadata, dict):
            raise ValueError("Workflow UI parameter metadata must be an object")
        name = metadata.get("name", key)
        if not isinstance(name, str) or not name:
            raise ValueError("Workflow UI parameter name must be a non-empty string")
        if name == "_execution":
            raise ValueError('Workflow parameter name "_execution" is reserved')
        if name in parameters:
            for field in ("type", "required", "description", "default"):
                if field in metadata:
                    parameters[name][field] = metadata[field]
        elif metadata.get("exposed", False):
            parameters[name] = dict(metadata)

    for name, metadata in parameters.items():
        if metadata.get("exposed", True) and (
            not str(metadata.get("node_id", ""))
            or not str(metadata.get("field", ""))
        ):
            raise ValueError(f'Workflow parameter "{name}" requires node_id and field')
    return parameters


def validate_parameter_targets(
    parameters: dict[str, dict[str, Any]], graph: Any
) -> None:
    """Ensure every exposed parameter targets an existing workflow input."""
    if not isinstance(graph, dict):
        raise ValueError("Workflow graph must be an object")
    for name, metadata in parameters.items():
        if not metadata.get("exposed", True):
            continue
        node_id = str(metadata.get("node_id", ""))
        field = str(metadata.get("field", ""))
        node = graph.get(node_id)
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict) or field not in inputs:
            raise ValueError(
                f'Workflow parameter "{name}" targets missing input '
                f'"{node_id}.{field}"'
            )


def build_input_schema(parameters: dict[str, Any]) -> dict[str, Any]:
    """Build the closed JSON Schema advertised by a workflow MCP tool."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, metadata in parameters.items():
        if not isinstance(metadata, dict) or not metadata.get("exposed", True):
            continue
        property_schema: dict[str, Any] = {
            "type": _TYPE_MAP.get(str(metadata.get("type", "string")).lower(), "string")
        }
        for field in _SCHEMA_FIELDS:
            if field in metadata:
                property_schema[field] = metadata[field]
        properties[name] = property_schema
        if metadata.get("required", False):
            required.append(name)
    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"Invalid JSON Schema: {exc.message}") from exc
    return schema


def validate_arguments(parameters: dict[str, Any], arguments: Any) -> None:
    """Reject malformed, unknown, missing, and out-of-range workflow arguments."""
    schema = build_input_schema(parameters)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(arguments),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if not errors:
        return
    first = errors[0]
    location = ".".join(str(part) for part in first.absolute_path)
    message = f"{location}: {first.message}" if location else first.message
    raise WorkflowArgumentsError(
        message,
        details={"path": list(first.absolute_path), "validator": first.validator},
    )
