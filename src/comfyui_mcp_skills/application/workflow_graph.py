"""Semantic workflow graph projection and structural validation."""

from __future__ import annotations

import json
from typing import Any

from comfyui_mcp_skills.domain.workflow_semantics import (
    DependencyExtractorRegistry,
    ParameterRoleRegistry,
)

_MAX_NODES = 1_000
_MAX_GRAPH_BYTES = 2 * 1024 * 1024
_MAX_SEMANTIC_BYTES = 1024 * 1024
_MAX_LABEL_LENGTH = 256
_MAX_INPUT_STRING_LENGTH = 65_536


class WorkflowGraphService:
    def __init__(
        self,
        parameter_roles: ParameterRoleRegistry,
        dependencies: DependencyExtractorRegistry,
    ) -> None:
        self._parameter_roles = parameter_roles
        self._dependencies = dependencies

    def describe(
        self,
        graph: dict[str, Any],
        *,
        object_info: dict[str, Any] | None = None,
        media_type: str = "image",
    ) -> dict[str, Any]:
        nodes: list[dict[str, str]] = []
        edges: list[dict[str, Any]] = []
        outputs: list[dict[str, str]] = []
        object_info = object_info or {}
        for node_id in sorted(graph, key=_node_sort_key):
            node = graph[node_id]
            if not isinstance(node, dict):
                continue
            class_type = str(node.get("class_type", "")).strip()
            title = class_type
            if isinstance(node.get("_meta"), dict):
                title = str(node["_meta"].get("title", class_type)).strip() or class_type
            nodes.append({"node_id": str(node_id), "class_type": class_type, "title": title})
            inputs = node.get("inputs")
            if isinstance(inputs, dict):
                for field in sorted(inputs):
                    value = inputs[field]
                    if _is_connection(value):
                        edges.append(
                            {
                                "source_node_id": str(value[0]),
                                "source_output": value[1],
                                "target_node_id": str(node_id),
                                "target_input": field,
                            }
                        )
            info = object_info.get(class_type)
            if _is_output_node(class_type, info):
                outputs.append(
                    {
                        "node_id": str(node_id),
                        "class_type": class_type,
                        "media_type": _output_media_type(class_type),
                    }
                )
        edges.sort(
            key=lambda item: (
                _node_sort_key(item["source_node_id"]),
                item["source_output"],
                _node_sort_key(item["target_node_id"]),
                item["target_input"],
            )
        )
        result = {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "nodes": nodes,
            "edges": edges,
            "parameters": self._parameter_roles.extract(
                graph, media_type=media_type, object_info=object_info
            ),
            "outputs": outputs,
            "dependencies": self._dependencies.extract(graph, object_info=object_info),
        }
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > _MAX_SEMANTIC_BYTES:
            raise ValueError("Semantic workflow projection exceeds 1 MiB")
        return result


class WorkflowValidationService:
    def validate_api(
        self, graph: object, object_info: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        issues: list[dict[str, str]] = []
        unsupported: set[str] = set()
        if not isinstance(graph, dict) or not graph:
            return _validation_result(
                [_issue("invalid_graph", "Workflow graph must be a non-empty object")],
                set(),
            )
        try:
            graph_size = len(json.dumps(graph, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError):
            return _validation_result(
                [_issue("invalid_graph", "Workflow graph must contain finite JSON values")],
                set(),
            )
        if graph_size > _MAX_GRAPH_BYTES:
            return _validation_result(
                [_issue("graph_too_large", "Workflow graph exceeds 2 MiB")], set()
            )
        if len(graph) > _MAX_NODES:
            return _validation_result(
                [_issue("graph_too_large", "Workflow graph exceeds 1000 nodes")],
                set(),
            )
        node_ids = {str(node_id) for node_id in graph}
        for raw_node_id in sorted(graph, key=_node_sort_key):
            node_id = str(raw_node_id)
            if len(node_id) > _MAX_LABEL_LENGTH:
                issues.append(_issue("identifier_too_long", "Node ID exceeds 256 characters"))
                continue
            node = graph[raw_node_id]
            if not isinstance(node, dict):
                issues.append(_issue("invalid_node", "Node must be an object", node_id))
                continue
            raw_class_type = node.get("class_type")
            class_type = raw_class_type.strip() if isinstance(raw_class_type, str) else ""
            inputs = node.get("inputs")
            if not class_type or not isinstance(inputs, dict):
                issues.append(
                    _issue(
                        "invalid_node", "Node requires string class_type and object inputs", node_id
                    )
                )
                continue
            if len(class_type) > _MAX_LABEL_LENGTH:
                issues.append(
                    _issue("identifier_too_long", "Node class_type exceeds 256 characters", node_id)
                )
            meta = node.get("_meta")
            if isinstance(meta, dict) and len(str(meta.get("title", ""))) > _MAX_LABEL_LENGTH:
                issues.append(
                    _issue("title_too_long", "Node title exceeds 256 characters", node_id)
                )
            info = object_info.get(class_type) if object_info is not None else None
            if object_info is not None and not isinstance(info, dict):
                unsupported.add(class_type)
                issues.append(
                    _issue("unknown_node_type", f"Unknown node type: {class_type}", node_id)
                )
            known_inputs = _known_inputs(info)
            required_inputs = _required_inputs(info)
            for field in sorted(required_inputs - {str(key) for key in inputs}):
                issues.append(
                    _issue(
                        "missing_required_input",
                        f"Required input is missing: {field}",
                        node_id,
                        field,
                    )
                )
            for raw_field in sorted(inputs, key=str):
                field = str(raw_field)
                value = inputs[raw_field]
                if not isinstance(raw_field, str) or len(field) > _MAX_LABEL_LENGTH:
                    issues.append(
                        _issue(
                            "invalid_input_name",
                            "Input name must be at most 256 characters",
                            node_id,
                            field[:256],
                        )
                    )
                    continue
                if isinstance(value, str) and len(value) > _MAX_INPUT_STRING_LENGTH:
                    issues.append(
                        _issue(
                            "input_too_large",
                            "String input exceeds 65536 characters",
                            node_id,
                            field,
                        )
                    )
                    continue
                if (
                    _is_media_path_input(class_type, field)
                    and isinstance(value, str)
                    and _unsafe_media_path(value)
                ):
                    issues.append(
                        _issue(
                            "unsafe_media_path",
                            "Media input must use a bounded ComfyUI server reference",
                            node_id,
                            field,
                        )
                    )
                if known_inputs is not None and field not in known_inputs:
                    issues.append(
                        _issue(
                            "unknown_input_port",
                            f"Unknown input port: {field}",
                            node_id,
                            field,
                        )
                    )
                if not isinstance(value, list):
                    scalar_issue = _validate_scalar_input(value, _input_definition(info, field))
                    if scalar_issue is not None:
                        code, message = scalar_issue
                        issues.append(_issue(code, message, node_id, field))
                if not isinstance(value, list):
                    continue
                if not _is_connection(value):
                    issues.append(
                        _issue(
                            "invalid_connection",
                            f"Invalid connection for input: {field}",
                            node_id,
                            field,
                        )
                    )
                    continue
                source_id = str(value[0])
                if source_id not in node_ids:
                    issues.append(
                        _issue(
                            "missing_source_node",
                            f"Connection source does not exist: {source_id}",
                            node_id,
                            field,
                        )
                    )
                    continue
                source = graph.get(source_id)
                if source is None and value[0] in graph:
                    source = graph[value[0]]
                source_type = str(source.get("class_type", "")) if isinstance(source, dict) else ""
                source_info = object_info.get(source_type) if object_info is not None else None
                outputs = source_info.get("output") if isinstance(source_info, dict) else None
                if isinstance(outputs, list) and value[1] >= len(outputs):
                    issues.append(
                        _issue(
                            "output_port_out_of_range",
                            f"Output port does not exist: {value[1]}",
                            node_id,
                            field,
                        )
                    )
                elif isinstance(outputs, list):
                    source_port = outputs[value[1]]
                    target_port = _input_type(info, field)
                    if (
                        isinstance(source_port, str)
                        and target_port is not None
                        and source_port != target_port
                        and target_port != "*"
                    ):
                        issues.append(
                            _issue(
                                "port_type_mismatch",
                                f"Connection type {source_port} is incompatible with {target_port}",
                                node_id,
                                field,
                            )
                        )
        return _validation_result(issues, unsupported)


def _required_inputs(info: object) -> set[str]:
    if not isinstance(info, dict):
        return set()
    inputs = info.get("input")
    if not isinstance(inputs, dict):
        return set()
    required = inputs.get("required")
    return {str(field) for field in required} if isinstance(required, dict) else set()


def _input_definition(info: object, field: str) -> list[Any] | None:
    if not isinstance(info, dict):
        return None
    inputs = info.get("input")
    if not isinstance(inputs, dict):
        return None
    for section in ("required", "optional"):
        definitions = inputs.get(section)
        if isinstance(definitions, dict):
            definition = definitions.get(field)
            if isinstance(definition, list) and definition:
                return definition
    return None


def _validate_scalar_input(value: object, definition: list[Any] | None) -> tuple[str, str] | None:
    if definition is None:
        return None
    declared = definition[0]
    options = definition[1] if len(definition) > 1 else None
    if isinstance(declared, list):
        choices = declared
    elif declared == "COMBO" and isinstance(options, dict):
        raw_choices = options.get("options")
        choices = raw_choices if isinstance(raw_choices, list) else []
    else:
        choices = []
    if choices:
        if value not in choices:
            return "invalid_enum_value", "Input value is not an advertised option"
        return None
    valid_type = {
        "INT": isinstance(value, int) and not isinstance(value, bool),
        "FLOAT": isinstance(value, (int, float)) and not isinstance(value, bool),
        "STRING": isinstance(value, str),
        "BOOLEAN": isinstance(value, bool),
    }.get(str(declared))
    if valid_type is None:
        return "invalid_input_type", f"Input {declared} requires a node connection"
    if not valid_type:
        return "invalid_input_type", f"Input value is incompatible with {declared}"
    if isinstance(options, dict) and isinstance(value, (int, float)):
        minimum = options.get("min")
        maximum = options.get("max")
        if isinstance(minimum, (int, float)) and value < minimum:
            return "input_out_of_range", f"Input value is below minimum {minimum}"
        if isinstance(maximum, (int, float)) and value > maximum:
            return "input_out_of_range", f"Input value exceeds maximum {maximum}"
    return None


def _known_inputs(info: object) -> set[str] | None:
    if not isinstance(info, dict):
        return None
    inputs = info.get("input")
    if not isinstance(inputs, dict):
        return None
    result: set[str] = set()
    for section in ("required", "optional"):
        values = inputs.get(section)
        if isinstance(values, dict):
            result.update(str(value) for value in values)
    return result


def _input_type(info: object, field: str) -> str | None:
    if not isinstance(info, dict):
        return None
    inputs = info.get("input")
    if not isinstance(inputs, dict):
        return None
    for section in ("required", "optional"):
        definitions = inputs.get(section)
        if not isinstance(definitions, dict):
            continue
        definition = definitions.get(field)
        if isinstance(definition, list) and definition and isinstance(definition[0], str):
            return definition[0]
    return None


def _is_media_path_input(class_type: str, field: str) -> bool:
    class_name = class_type.casefold()
    field_name = field.casefold()
    field_is_path = any(
        token in field_name
        for token in ("image", "audio", "video", "media", "file", "path", "filename")
    )
    return field_is_path and (
        "load" in class_name
        or any(token in class_name for token in ("image", "audio", "video", "media", "gif"))
    )


def _issue(code: str, message: str, node_id: str = "", field: str = "") -> dict[str, str]:
    result = {"code": code, "message": message}
    if node_id:
        result["node_id"] = node_id
    if field:
        result["field"] = field
    return result


def _validation_result(issues: list[dict[str, str]], unsupported: set[str]) -> dict[str, Any]:
    issues.sort(
        key=lambda item: (
            item.get("node_id", ""),
            item.get("field", ""),
            item["code"],
        )
    )
    return {"valid": not issues, "issues": issues, "unsupported_nodes": sorted(unsupported)}


def _is_connection(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], (str, int))
        and not isinstance(value[0], bool)
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
        and value[1] >= 0
    )


def _is_output_node(class_type: str, info: object) -> bool:
    return (isinstance(info, dict) and info.get("output_node") is True) or class_type.startswith(
        ("Save", "Preview")
    )


def _output_media_type(class_type: str) -> str:
    lowered = class_type.lower()
    if "audio" in lowered:
        return "audio"
    if "video" in lowered:
        return "video"
    return "image"


def _unsafe_media_path(value: str) -> bool:
    if any(ord(character) < 32 for character in value):
        return True
    normalized = value.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    return (
        not normalized
        or normalized.startswith("/")
        or ":" in normalized
        or any(part == ".." for part in parts)
    )


def _node_sort_key(value: object) -> tuple[int, int | str]:
    text = str(value)
    return (0, int(text)) if text.isdigit() else (1, text)
