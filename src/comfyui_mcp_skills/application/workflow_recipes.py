"""Registered high-level workflow branch recipes (LoRA / Upscaler / Save).

Recipes are registered, explainable graph transforms applied through the
existing ``apply_recipe`` change operation. Every class and input field used
here comes from the caller-supplied ``object_info``; nothing is invented.
"""

from __future__ import annotations

import math
from typing import Any

# Recipe argument contracts: required keys must be present, optional keys may
# be omitted (defaults applied); ``exact`` recipes (set_scalar_input.v1) keep
# the legacy precise-set semantics.
RECIPE_CONTRACTS: dict[str, dict[str, Any]] = {
    "set_scalar_input.v1": {
        "required": frozenset({"node_id", "field", "value"}),
        "optional": frozenset(),
        "exact": True,
    },
    "upscale_image.v1": {
        "required": frozenset({"after_node_id", "model"}),
        "optional": frozenset(),
        "exact": False,
    },
    "save_image.v1": {
        "required": frozenset({"after_node_id"}),
        "optional": frozenset({"filename_prefix"}),
        "exact": False,
    },
    "lora_model.v1": {
        "required": frozenset({"loader_node_id", "lora_name"}),
        "optional": frozenset({"strength_model", "strength_clip"}),
        "exact": False,
    },
}

RECIPE_DEFAULTS: dict[str, dict[str, Any]] = {
    "save_image.v1": {"filename_prefix": "recipe"},
    "lora_model.v1": {"strength_model": 1.0, "strength_clip": 1.0},
}

_IMAGE = "IMAGE"
_MODEL = "MODEL"
_CLIP = "CLIP"


class RecipeError(ValueError):
    """A registered recipe rejected its arguments or the target graph."""


def apply_recipe(
    graph: dict[str, Any],
    parameter_schema: dict[str, Any],
    recipe_id: str,
    arguments: object,
    object_info: dict[str, Any] | None,
    *,
    index: int,
) -> None:
    """Validate the recipe contract and apply the registered transform."""
    contract = RECIPE_CONTRACTS.get(recipe_id)
    if contract is None:
        raise RecipeError(f"operations[{index}].recipe_id is not registered")
    normalized = _normalized_arguments(recipe_id, contract, arguments, index=index)
    if recipe_id == "set_scalar_input.v1":
        _apply_scalar(graph, parameter_schema, normalized, index=index)
        return
    if object_info is None:
        raise RecipeError(
            f"operations[{index}] recipe {recipe_id} requires object_info metadata"
        )
    if recipe_id == "upscale_image.v1":
        _apply_upscale(graph, parameter_schema, normalized, object_info, index=index)
    elif recipe_id == "save_image.v1":
        _apply_save(graph, parameter_schema, normalized, object_info, index=index)
    elif recipe_id == "lora_model.v1":
        _apply_lora(graph, parameter_schema, normalized, object_info, index=index)
    else:  # pragma: no cover - guarded by RECIPE_CONTRACTS
        raise RecipeError(f"operations[{index}].recipe_id is not registered")


def _normalized_arguments(
    recipe_id: str,
    contract: dict[str, Any],
    arguments: object,
    *,
    index: int,
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise RecipeError(f"operations[{index}].arguments is invalid for {recipe_id}")
    keys = set(arguments)
    required = contract["required"]
    optional = contract["optional"]
    if contract.get("exact"):
        if keys != required:
            raise RecipeError(f"operations[{index}].arguments is invalid for {recipe_id}")
        return dict(arguments)
    if not required <= keys <= required | optional:
        missing = sorted(required - keys)
        extra = sorted(keys - required - optional)
        raise RecipeError(
            f"operations[{index}].arguments is invalid for {recipe_id}"
            f" (missing={missing} extra={extra})"
        )
    normalized = dict(arguments)
    for key, default in RECIPE_DEFAULTS.get(recipe_id, {}).items():
        normalized.setdefault(key, default)
    for key in ("after_node_id", "loader_node_id", "node_id", "field"):
        if key in normalized:
            value = normalized[key]
            if not isinstance(value, str) or not value or len(value) > 256:
                raise RecipeError(f"operations[{index}].arguments.{key} must be a bounded string")
    for key in ("model", "lora_name", "filename_prefix"):
        if key in normalized:
            value = normalized[key]
            if not isinstance(value, str) or len(value) > 1024:
                raise RecipeError(f"operations[{index}].arguments.{key} must be a bounded string")
    for key in ("strength_model", "strength_clip"):
        if key in normalized:
            value = normalized[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise RecipeError(
                    f"operations[{index}].arguments.{key} must be a finite number"
                )
    return normalized


def _apply_scalar(
    graph: dict[str, Any],
    parameter_schema: dict[str, Any],
    arguments: dict[str, Any],
    *,
    index: int,
) -> None:
    node_id = arguments["node_id"]
    field = arguments["field"]
    if not isinstance(node_id, str) or not node_id or len(node_id) > 256:
        raise RecipeError(f"operations[{index}].arguments.node_id must be a bounded string")
    if not isinstance(field, str) or not field or len(field) > 256:
        raise RecipeError(f"operations[{index}].arguments.field must be a bounded string")
    value = arguments["value"]
    if _is_connection(value):
        raise RecipeError(f"operations[{index}].arguments.value must be scalar")
    inputs = _node_inputs(graph, node_id, index)
    # The target field may be an optional input the graph currently omits;
    # object_info validation runs after the recipe and decides its legality.
    # Existing public parameters for this target stay intact (legacy behavior).
    inputs[field] = value


def _apply_upscale(
    graph: dict[str, Any],
    parameter_schema: dict[str, Any],
    arguments: dict[str, Any],
    object_info: dict[str, Any],
    *,
    index: int,
) -> None:
    after = str(arguments["after_node_id"])
    _require_output_type(graph, after, 0, _IMAGE, object_info, index)
    # Snapshot consumers BEFORE inserting so the new chain is never rewired.
    consumers = _snapshot_consumers(graph, after, 0)
    loader_id = _next_node_id(graph, "upscale_loader")
    _insert_node(
        graph,
        parameter_schema,
        loader_id,
        "UpscaleModelLoader",
        {"model_name": arguments["model"]},
        object_info,
        exposed={"model_name": True},
        index=index,
    )
    upscaler_id = _next_node_id(graph, "upscaler")
    _insert_node(
        graph,
        parameter_schema,
        upscaler_id,
        "ImageUpscaleWithModel",
        {"upscale_model": [loader_id, 0], "image": [after, 0]},
        object_info,
        exposed={},
        index=index,
    )
    _rewire_consumers(graph, consumers, after, 0, upscaler_id, 0)


def _apply_save(
    graph: dict[str, Any],
    parameter_schema: dict[str, Any],
    arguments: dict[str, Any],
    object_info: dict[str, Any],
    *,
    index: int,
) -> None:
    after = str(arguments["after_node_id"])
    _require_output_type(graph, after, 0, _IMAGE, object_info, index)
    save_id = _next_node_id(graph, "save")
    _insert_node(
        graph,
        parameter_schema,
        save_id,
        "SaveImage",
        {
            "images": [after, 0],
            "filename_prefix": str(arguments["filename_prefix"]),
        },
        object_info,
        exposed={"filename_prefix": False},
        index=index,
    )


def _apply_lora(
    graph: dict[str, Any],
    parameter_schema: dict[str, Any],
    arguments: dict[str, Any],
    object_info: dict[str, Any],
    *,
    index: int,
) -> None:
    loader = str(arguments["loader_node_id"])
    _require_output_type(graph, loader, 0, _MODEL, object_info, index)
    _require_output_type(graph, loader, 1, _CLIP, object_info, index)
    model_consumers = _snapshot_consumers(graph, loader, 0)
    clip_consumers = _snapshot_consumers(graph, loader, 1)
    lora_id = _next_node_id(graph, "lora")
    _insert_node(
        graph,
        parameter_schema,
        lora_id,
        "LoraLoader",
        {
            "model": [loader, 0],
            "clip": [loader, 1],
            "lora_name": str(arguments["lora_name"]),
            "strength_model": arguments["strength_model"],
            "strength_clip": arguments["strength_clip"],
        },
        object_info,
        exposed={"lora_name": True, "strength_model": False, "strength_clip": False},
        index=index,
    )
    _rewire_consumers(graph, model_consumers, loader, 0, lora_id, 0)
    _rewire_consumers(graph, clip_consumers, loader, 1, lora_id, 1)


# -- shared helpers ---------------------------------------------------------


def _output_types(
    graph: dict[str, Any], node_id: str, object_info: dict[str, Any]
) -> list[str]:
    node = graph.get(node_id)
    if not isinstance(node, dict):
        return []
    info = object_info.get(str(node.get("class_type", "")))
    if not isinstance(info, dict):
        return []
    outputs = info.get("output")
    if not isinstance(outputs, list):
        return []
    return [str(item).upper() for item in outputs]


def _require_output_type(
    graph: dict[str, Any],
    node_id: str,
    output_index: int,
    expected: str,
    object_info: dict[str, Any],
    index: int,
) -> None:
    outputs = _output_types(graph, node_id, object_info)
    if output_index >= len(outputs) or outputs[output_index] != expected:
        raise RecipeError(
            f"operations[{index}] node {node_id} output {output_index} must be {expected}"
        )


def _next_node_id(graph: dict[str, Any], prefix: str) -> str:
    suffix = 1
    while f"{prefix}_{suffix}" in graph:
        suffix += 1
    return f"{prefix}_{suffix}"


def _insert_node(
    graph: dict[str, Any],
    parameter_schema: dict[str, Any],
    node_id: str,
    class_type: str,
    inputs: dict[str, Any],
    object_info: dict[str, Any],
    *,
    exposed: dict[str, bool],
    index: int,
) -> None:
    info = object_info.get(class_type)
    if not isinstance(info, dict):
        raise RecipeError(
            f"operations[{index}] recipe class {class_type} is not in object_info"
        )
    input_sections = info.get("input")
    declared: dict[str, Any] = {}
    if isinstance(input_sections, dict):
        for section in ("required", "optional"):
            values = input_sections.get(section)
            if isinstance(values, dict):
                declared.update(values)
    for field in inputs:
        if field not in declared:
            raise RecipeError(
                f"operations[{index}] class {class_type} has no input field {field}"
            )
    graph[node_id] = {"class_type": class_type, "inputs": dict(inputs)}
    parameters = parameter_schema.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {}
        parameter_schema["parameters"] = parameters
    for field, required in exposed.items():
        if field not in inputs:
            continue
        name = f"{node_id}.{field}"
        if name in parameters:
            raise RecipeError(
                f"operations[{index}] parameter {name} already exists"
            )
        metadata = declared_parameter(info, field, inputs[field])
        metadata.update({"node_id": node_id, "field": field, "required": required})
        parameters[name] = metadata


def _snapshot_consumers(
    graph: dict[str, Any], source_id: str, source_output: int
) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for target_id, node in graph.items():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for field, value in inputs.items():
            if _is_connection(value) and str(value[0]) == source_id and value[1] == source_output:
                result.append((str(target_id), str(field)))
    return result


def _rewire_consumers(
    graph: dict[str, Any],
    consumers: list[tuple[str, str]],
    source_id: str,
    source_output: int,
    target_id: str,
    target_output: int,
) -> None:
    for target_id_name, field in consumers:
        node = graph.get(target_id_name)
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        value = inputs.get(field)
        if (
            _is_connection(value)
            and isinstance(value, list)
            and str(value[0]) == source_id
            and value[1] == source_output
        ):
            inputs[field] = [target_id, target_output]


def declared_parameter(info: object, field: str, current: object) -> dict[str, Any]:
    """Build public parameter metadata from a ComfyUI object_info entry."""
    if not isinstance(info, dict):
        raise ValueError(f'Input "{field}" has no ComfyUI object_info metadata')
    inputs = info.get("input")
    definition: object = None
    if isinstance(inputs, dict):
        for section in ("required", "optional"):
            values = inputs.get(section)
            if isinstance(values, dict) and field in values:
                definition = values[field]
                break
    if not isinstance(definition, list) or not definition:
        raise ValueError(f'Input "{field}" has no ComfyUI object_info metadata')
    declared = definition[0]
    settings = definition[1] if len(definition) > 1 and isinstance(definition[1], dict) else {}
    type_map = {
        "INT": "int",
        "FLOAT": "float",
        "BOOLEAN": "boolean",
        "STRING": "string",
        "IMAGE": "image",
        "AUDIO": "audio",
        "VIDEO": "video",
    }
    if isinstance(declared, str):
        parameter_type = type_map.get(declared.upper(), "string")
    elif isinstance(declared, list):
        parameter_type = _type_guess(declared[0]) if declared else "string"
    else:
        raise ValueError(f'Input "{field}" has unsupported ComfyUI object_info metadata')
    metadata: dict[str, Any] = {"type": parameter_type, "default": current}
    for source, target in (("min", "minimum"), ("max", "maximum")):
        value = settings.get(source)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metadata[target] = value
    options = declared if isinstance(declared, list) else settings.get("options")
    if isinstance(options, list) and len(options) <= 200:
        metadata["enum"] = list(options)
    description = settings.get("tooltip", settings.get("description"))
    if isinstance(description, str) and description:
        metadata["description"] = description
    return metadata


def _type_guess(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    return "string"


def _is_connection(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], (str, int))
        and isinstance(value[1], int)
    )


def _node_inputs(graph: dict[str, Any], node_id: str, index: int) -> dict[str, Any]:
    node = graph.get(node_id)
    if not isinstance(node, dict):
        raise RecipeError(f"operations[{index}] references missing node {node_id}")
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        raise RecipeError(f"operations[{index}] node {node_id} has no inputs")
    return inputs


__all__ = ["RecipeError", "apply_recipe", "declared_parameter"]
