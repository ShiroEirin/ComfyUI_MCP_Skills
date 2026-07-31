"""Deterministic ComfyUI Editor workflow conversion."""

from __future__ import annotations

import json
from typing import Any


def detect_workflow_format(source: object) -> str:
    if (
        isinstance(source, dict)
        and isinstance(source.get("nodes"), list)
        and isinstance(source.get("links"), list)
    ):
        return "editor"
    if (
        isinstance(source, dict)
        and source
        and all(
            isinstance(node, dict)
            and isinstance(node.get("class_type"), str)
            and isinstance(node.get("inputs"), dict)
            for node in source.values()
        )
    ):
        return "api"
    raise ValueError("Unrecognized workflow format")


def convert_editor_workflow(
    source: object, object_info: dict[str, Any]
) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...]]:
    editor = _copy_json_object(source, field="editor workflow")
    dropped: set[str] = set()
    raw_nodes = editor.get("nodes", [])
    raw_links = editor.get("links", [])
    nodes: list[dict[str, Any]] = []
    if not isinstance(raw_nodes, list):
        dropped.add("editor.nodes")
    else:
        for index, node in enumerate(raw_nodes):
            if isinstance(node, dict):
                nodes.append(node)
            else:
                dropped.add(f"nodes[{index}]")
    links = raw_links if isinstance(raw_links, list) else []
    if not isinstance(raw_links, list):
        dropped.add("editor.links")

    by_id: dict[int, dict[str, Any]] = {}
    for index, node in enumerate(nodes):
        node_id = node.get("id")
        if not isinstance(node_id, int) or isinstance(node_id, bool):
            dropped.add(f"nodes[{index}].id")
            continue
        if node_id in by_id:
            dropped.add(f"nodes[{index}].duplicate_id")
            continue
        by_id[node_id] = node

    unsupported = sorted(
        {
            str(node.get("type", "")).strip()
            for node in nodes
            if str(node.get("type", "")).strip()
            and str(node.get("type", "")).strip() not in {"Reroute", "Note"}
            and not isinstance(object_info.get(str(node.get("type", "")).strip()), dict)
        }
    )
    link_map: dict[tuple[int, int], tuple[str, int]] = {}
    for index, link in enumerate(links):
        if not isinstance(link, list) or len(link) < 5:
            dropped.add(f"links[{index}]")
            continue
        try:
            source_ref = _resolve_reroute(int(link[1]), int(link[2]), by_id, links, set())
            target = (int(link[3]), int(link[4]))
            target_node = by_id.get(target[0])
            target_slots = target_node.get("inputs") if isinstance(target_node, dict) else None
            target_type = str(target_node.get("type", "")).strip() if target_node else ""
            target_slot = (
                target_slots[target[1]]
                if isinstance(target_slots, list) and 0 <= target[1] < len(target_slots)
                else None
            )
            valid_target_slot = isinstance(target_slot, dict)
            if (
                target_node is None
                or not valid_target_slot
                or (
                    target_type != "Reroute"
                    and isinstance(target_slot, dict)
                    and not str(target_slot.get("name", "")).strip()
                )
            ):
                dropped.add(f"links[{index}].target")
                continue
        except (TypeError, ValueError):
            dropped.add(f"links[{index}]")
            continue
        if source_ref is None:
            dropped.add(f"links[{index}].source")
        elif target in link_map:
            dropped.add(f"links[{index}].target")
        else:
            link_map[target] = source_ref

    graph: dict[str, Any] = {}
    for index, node in enumerate(nodes):
        node_id = node.get("id")
        class_type = str(node.get("type", "")).strip()
        if not isinstance(node_id, int) or isinstance(node_id, bool):
            continue
        if by_id.get(node_id) is not node:
            continue
        if not class_type:
            dropped.add(f"nodes[{index}].type")
            continue
        if class_type in {"Reroute", "Note"}:
            continue
        info = object_info.get(class_type)
        if not isinstance(info, dict):
            continue
        inputs, node_dropped = convert_editor_node_inputs(node, info, link_map)
        dropped.update(f"{node_id}.{field}" for field in node_dropped)
        title = str(node.get("title", class_type)).strip() or class_type
        graph[str(node_id)] = {
            "inputs": inputs,
            "class_type": class_type,
            "_meta": {"title": title},
        }
    return graph, tuple(unsupported), tuple(sorted(dropped))


def convert_editor_node_inputs(
    node: dict[str, Any],
    node_info: dict[str, Any],
    link_map: dict[tuple[int, int], tuple[str, int]],
) -> tuple[dict[str, Any], set[str]]:
    node_id = int(node["id"])
    raw_slots = node.get("inputs")
    slots: list[Any] = raw_slots if isinstance(raw_slots, list) else []
    raw_widget_values = node.get("widgets_values")
    widget_values: list[Any] = raw_widget_values if isinstance(raw_widget_values, list) else []
    dropped: set[str] = set()
    if raw_slots is not None and not isinstance(raw_slots, list):
        dropped.add("inputs")
    if raw_widget_values is not None and not isinstance(raw_widget_values, list):
        dropped.add("widgets_values")
    converted: dict[str, Any] = {}
    connected: set[str] = set()
    for slot_index, slot in enumerate(slots):
        if not isinstance(slot, dict):
            dropped.add(f"inputs[{slot_index}]")
            continue
        name = str(slot.get("name", "")).strip()
        link = link_map.get((node_id, slot_index))
        if name and link is not None:
            converted[name] = [link[0], link[1]]
            connected.add(name)
    widget_names = _widget_names(node_info)
    control_fields = _control_fields(node_info)
    index = 0
    for field in widget_names:
        if field in connected:
            index += 1
            continue
        if index >= len(widget_values):
            break
        converted[field] = widget_values[index]
        index += 1
        if field in control_fields and index < len(widget_values):
            marker = widget_values[index]
            if isinstance(marker, str) and marker.lower() in {
                "fixed",
                "increment",
                "decrement",
                "randomize",
            }:
                index += 1
    dropped.update(f"widgets_values[{extra}]" for extra in range(index, len(widget_values)))
    return converted, dropped | (set(widget_names) - set(converted))


def _widget_names(node_info: dict[str, Any]) -> list[str]:
    raw_inputs = node_info.get("input")
    inputs: dict[str, Any] = raw_inputs if isinstance(raw_inputs, dict) else {}
    raw_ordered = node_info.get("input_order")
    ordered: dict[str, Any] = raw_ordered if isinstance(raw_ordered, dict) else {}
    names: list[str] = []
    for section in ("required", "optional"):
        candidates = ordered.get(section)
        if not isinstance(candidates, list):
            candidates = (
                list(inputs.get(section, {})) if isinstance(inputs.get(section), dict) else []
            )
        raw_definitions = inputs.get(section)
        definitions: dict[str, Any] = raw_definitions if isinstance(raw_definitions, dict) else {}
        for name in candidates:
            if _is_widget_definition(definitions.get(name)):
                names.append(str(name))
    return names


def _is_widget_definition(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    return isinstance(value[0], list) or value[0] in {
        "INT",
        "FLOAT",
        "STRING",
        "BOOLEAN",
        "COMBO",
    }


def _control_fields(node_info: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    raw_inputs = node_info.get("input")
    inputs: dict[str, Any] = raw_inputs if isinstance(raw_inputs, dict) else {}
    for section in ("required", "optional"):
        definitions = inputs.get(section)
        if not isinstance(definitions, dict):
            continue
        for field, definition in definitions.items():
            if (
                isinstance(definition, list)
                and len(definition) > 1
                and isinstance(definition[1], dict)
                and definition[1].get("control_after_generate")
            ):
                result.add(str(field))
    return result


def _resolve_reroute(
    node_id: int,
    output: int,
    nodes: dict[int, dict[str, Any]],
    links: list[Any],
    visited: set[int],
) -> tuple[str, int] | None:
    if node_id in visited:
        return None
    node = nodes.get(node_id)
    if not isinstance(node, dict):
        return None
    if str(node.get("type", "")).strip() != "Reroute":
        return str(node_id), output
    visited.add(node_id)
    slots = node.get("inputs")
    if not isinstance(slots, list) or not slots or not isinstance(slots[0], dict):
        return None
    incoming = slots[0].get("link")
    for link in links:
        if isinstance(link, list) and len(link) >= 5 and link[0] == incoming:
            return _resolve_reroute(int(link[1]), int(link[2]), nodes, links, visited)
    return None


def _copy_json_object(value: object, *, field: str) -> dict[str, Any]:
    try:
        copied = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be finite JSON") from exc
    if not isinstance(copied, dict):
        raise ValueError(f"{field} must be an object")
    return copied


def _node_sort_key(value: object) -> tuple[int, int | str]:
    text = str(value)
    return (0, int(text)) if text.isdigit() else (1, text)
