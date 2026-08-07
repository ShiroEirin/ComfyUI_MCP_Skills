"""Read-only discovery of ComfyUI servers, nodes, and models."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from comfyui_mcp_skills.application.assets import same_file_stat
from comfyui_mcp_skills.application.ports import ComfyUIGateway
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import WorkflowArgumentsError, WorkflowNotFound

GatewayFactory = Callable[[dict[str, Any]], ComfyUIGateway]

_PLUGIN_SCAN_BUDGET = 201
_PLUGIN_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
_README_MAX_BYTES = 4 * 1024
_README_MAX_CHARS = 200
_PLUGIN_TYPE_TAGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("anima", ("anima",)),
    ("sampler", ("sampler", "flsampler", "wavespeed")),
    ("upscale", ("upscale", "ultimatesdupscale", "vsr")),
    ("controlnet", ("controlnet", "ipadapter")),
    ("llm", ("llm", "gpt", "janus")),
    ("translate", ("translate", "translation")),
    ("impact", ("impact", "segment", "detect")),
    ("essentials", ("essentials", "easy-use", "efficiency", "rgthree")),
)


def _is_reparse_or_link(path: Path) -> bool:
    """Reject Windows reparse points (junctions) and symlinks."""
    try:
        result = path.lstat()
    except OSError:
        return True
    if stat.S_ISLNK(result.st_mode):
        return True
    attributes = getattr(result, "st_file_attributes", 0)
    if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return True
    return False


def _path_is_safe_directory(path: Path) -> bool:
    """A directory whose raw path and every un-resolved component are real,
    non-reparse, non-symlink directories."""
    raw = path if path.is_absolute() else path.absolute()
    current = raw
    parts: list[Path] = []
    while current != current.parent:
        parts.append(current)
        current = current.parent
    for component in parts:
        if _is_reparse_or_link(component):
            return False
    try:
        resolved = raw.resolve(strict=True)
    except OSError:
        return False
    if not resolved.is_dir():
        return False
    return True


def _clean_readme_line(content: bytes) -> str:
    text = content.decode("utf-8", errors="replace")
    first_line = text.splitlines()[0] if text.splitlines() else ""
    cleaned = "".join(
        char for char in first_line if char.isprintable() or char in " \t"
    )
    # Remove drive, UNC, POSIX absolute, and traversal path fragments in
    # both separator directions (Windows backslash and POSIX slash).
    cleaned = re.sub(
        r"(?i)(?:[a-z]:[\\/]|\\\\[a-z0-9._-]+[\\/]|/[a-z0-9._-]+(?:/[^ \t]*)?|\.\.[\\/])+",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:_README_MAX_CHARS]


def _plugin_entry(directory: Path) -> dict[str, Any]:
    """One plugin: name, keyword type tags, and a bounded README first line."""
    name = directory.name
    lowered = name.casefold()
    tags = [
        label
        for label, keywords in _PLUGIN_TYPE_TAGS
        if any(keyword in lowered for keyword in keywords)
    ][:4]
    if not tags:
        tags = ["custom"]
    plugin: dict[str, Any] = {"name": name, "type_tags": tags}
    for readme_name in ("README.md", "README", "readme.md"):
        readme_path = directory / readme_name
        if not readme_path.is_file() or _is_reparse_or_link(readme_path):
            continue
        if not stat.S_ISREG(readme_path.lstat().st_mode):
            continue
        try:
            expected = readme_path.resolve(strict=True).stat()
            with readme_path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                # Re-check the whole chain after opening: a swapped symlink or
                # junction must fail the containment check even if the three
                # stat snapshots agree.
                if not _path_is_safe_directory(directory):
                    continue
                try:
                    resolved_readme = readme_path.resolve(strict=True)
                    resolved_dir = directory.resolve(strict=True)
                except OSError:
                    continue
                try:
                    resolved_readme.relative_to(resolved_dir)
                except ValueError:
                    continue
                if not same_file_stat(expected, opened):
                    continue
                content = handle.read(_README_MAX_BYTES)
                after = os.fstat(handle.fileno())
                current = readme_path.resolve(strict=True).stat()
                if not same_file_stat(opened, after) or not same_file_stat(
                    opened, current
                ):
                    continue
        except OSError:
            continue
        summary = _clean_readme_line(content)
        if summary:
            plugin["readme"] = summary
        break
    return plugin

_BLUEPRINT_MAX_FIELDS = 8
_BLUEPRINT_MAX_ENUM_VALUES = 8
_BLUEPRINT_MAX_OUTPUTS = 4
_BLUEPRINT_MAX_FIELD_VALUE_CHARS = 64


def _compact_type(value: object) -> str:
    """Render one object_info field type slot compactly."""
    if isinstance(value, list):
        return ", ".join(_compact_type(item) for item in value[:4])
    return str(value)


def _advertised_options(value: object) -> list[str]:
    """Extract advertised enum options from a field type slot, bounded."""
    options: list[str] = []
    raw_options: object = value
    if isinstance(value, dict):
        raw_options = value.get("options", [])
    if isinstance(raw_options, list):
        for option in raw_options:
            if len(options) >= _BLUEPRINT_MAX_ENUM_VALUES:
                break
            if isinstance(option, str) and option:
                options.append(option[:_BLUEPRINT_MAX_FIELD_VALUE_CHARS])
    return options


def _blueprint_field(name: str, spec: list[Any], *, required: bool) -> dict[str, Any]:
    """Project one input field: type plus bounded advertised options.

    object_info spells enums both as a bare list (``[["a", "b"]]``) and as
    ``["COMBO", {"options": [...]}]``; both are projected as ``options``.
    """
    raw_type = spec[0] if spec else None
    field: dict[str, Any] = {"name": name, "type": "unknown", "required": required}
    options: list[str] = []
    if isinstance(raw_type, list):
        field["type"] = "COMBO"
        options = _advertised_options(raw_type)
    elif isinstance(raw_type, str):
        field["type"] = raw_type
        if len(spec) > 1:
            options = _advertised_options(spec[1])
    else:
        field["type"] = _compact_type(raw_type)
        if len(spec) > 1:
            options = _advertised_options(spec[1])
    if options:
        field["options"] = options
    return field


def _blueprint_item(node_class: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """Compact node signature: bounded input fields with types and options."""
    fields: list[dict[str, Any]] = []
    input_spec = metadata.get("input")
    input_order = metadata.get("input_order")
    ordered_required: list[str] = []
    if isinstance(input_order, dict):
        raw_required = input_order.get("required")
        if isinstance(raw_required, list):
            ordered_required = [str(name) for name in raw_required[: _BLUEPRINT_MAX_FIELDS]]
    seen: set[str] = set()
    for name in ordered_required:
        if name in seen or len(fields) >= _BLUEPRINT_MAX_FIELDS:
            continue
        seen.add(name)
        spec = input_spec.get("required", {}).get(name) if isinstance(input_spec, dict) else None
        if not isinstance(spec, list) or not spec:
            continue
        fields.append(_blueprint_field(name, spec, required=True))
    if isinstance(input_spec, dict):
        optional = input_spec.get("optional")
        if isinstance(optional, dict):
            for name in sorted(optional, key=str):
                if name in seen or len(fields) >= _BLUEPRINT_MAX_FIELDS:
                    continue
                seen.add(name)
                spec = optional[name]
                if not isinstance(spec, list) or not spec:
                    continue
                fields.append(_blueprint_field(str(name), spec, required=False))
    outputs = metadata.get("output")
    output_types: list[str] = []
    if isinstance(outputs, list):
        for output in outputs[: _BLUEPRINT_MAX_OUTPUTS]:
            if isinstance(output, str):
                output_types.append(output)
    item: dict[str, Any] = {
        "class_type": node_class,
        "display_name": str(metadata.get("display_name", node_class)),
        "category": str(metadata.get("category", "")),
        "inputs": fields,
    }
    if output_types:
        item["outputs"] = output_types
    return item


class DiscoveryService:
    def __init__(self, servers: ServerRegistry, gateway_factory: GatewayFactory) -> None:
        self._servers = servers
        self._gateway_factory = gateway_factory

    def servers(self) -> dict[str, Any]:
        return {
            "items": [
                {
                    "server_id": server.server_id,
                    "name": server.name,
                    "enabled": server.enabled,
                }
                for server in self._servers.list()
            ]
        }

    def health(self, server_id: str) -> dict[str, Any]:
        server = self._servers.get(server_id)
        gateway = self._gateway_factory(self._servers.connection(server_id))
        return {
            "server_id": server.server_id,
            "name": server.name,
            "status": "online",
            "stats": gateway.get_system_stats(),
            "cancel_running_supported": False,
        }

    def nodes(
        self,
        server_id: str,
        *,
        query: str = "",
        limit: int = 50,
        cursor: str = "",
    ) -> dict[str, Any]:
        gateway = self._gateway_factory(self._servers.connection(server_id))
        raw = gateway.get_object_info()
        items = [
            {
                "class": node_class,
                "display_name": str(metadata.get("display_name", node_class)),
                "category": str(metadata.get("category", "")),
            }
            for node_class, metadata in raw.items()
            if isinstance(node_class, str) and isinstance(metadata, dict)
        ]
        items.sort(key=lambda item: (item["class"].casefold(), item["class"]))
        return self._page(items, query=query, limit=limit, cursor=cursor, key="class")

    def node(self, server_id: str, node_class: str) -> dict[str, Any]:
        gateway = self._gateway_factory(self._servers.connection(server_id))
        node = gateway.get_object_info_node(node_class)
        if node is None:
            raise WorkflowNotFound(f"ComfyUI node not found: {node_class}")
        return {"server_id": server_id, "node_class": node_class, "node": node}

    def blueprint(
        self,
        server_id: str,
        *,
        query: str,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Project a goal-driven compact node blueprint from object_info.

        Matches nodes by keyword overlap on class/display_name/category and
        returns bounded compact signatures: at most ``limit`` nodes, each with
        up to _BLUEPRINT_MAX_FIELDS inputs whose names, types and (truncated)
        advertised options fit a token budget an agent can actually consume.
        """
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if len(query) > 256:
            raise ValueError("query must be at most 256 characters")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
            raise ValueError("limit must be an integer between 1 and 10")
        terms = [term.casefold() for term in re.split(r"[\s,]+", query.strip()) if term]
        if not terms:
            raise ValueError("query must contain at least one keyword")
        gateway = self._gateway_factory(self._servers.connection(server_id))
        raw = gateway.get_object_info()
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for node_class, metadata in raw.items():
            if not isinstance(node_class, str) or not isinstance(metadata, dict):
                continue
            display_name = str(metadata.get("display_name", node_class)).casefold()
            category = str(metadata.get("category", "")).casefold()
            class_name = node_class.casefold()
            score = 0
            for term in terms:
                if term in display_name:
                    score += 3
                elif term in category:
                    score += 2
                elif term in class_name:
                    score += 1
            if score:
                scored.append((score, node_class, metadata))
        scored.sort(key=lambda entry: (-entry[0], entry[1].casefold(), entry[1]))
        return {
            "server_id": server_id,
            "query": query,
            "items": [
                _blueprint_item(node_class, metadata)
                for _score, node_class, metadata in scored[:limit]
            ],
            "total_matches": len(scored),
        }

    def models(
        self,
        server_id: str,
        *,
        kind: str = "",
        query: str = "",
        limit: int = 50,
        cursor: str = "",
    ) -> dict[str, Any]:
        gateway = self._gateway_factory(self._servers.connection(server_id))
        if not kind:
            values = sorted(str(value) for value in gateway.get_model_folders())
            page = self._page(values, query=query, limit=limit, cursor=cursor)
            page["kind"] = ""
            return page
        values = sorted(str(value) for value in gateway.get_models(kind))
        page = self._page(values, query=query, limit=limit, cursor=cursor)
        page["kind"] = kind
        return page

    def plugins(self, server_id: str) -> dict[str, Any]:
        """List custom_nodes plugins from a locally configured ComfyUI root.

        This is the local-session channel: the server entry must configure
        ``local_root`` (the ComfyUI installation root, e.g. an aki bundle);
        without it, or when the root is unsafe/unreadable, the tool reports
        ``available: false`` with a fixed reason code and never fabricates
        success. Cloud sessions (no local file access) get the same fallback.
        """
        connection = self._servers.connection(server_id)
        local_root = connection.get("local_root")
        if not isinstance(local_root, str) or not local_root.strip():
            return {"available": False, "reason": "no_local_root"}
        root = Path(local_root)
        if not root.exists():
            return {"available": False, "reason": "root_not_found"}
        if not root.is_dir():
            return {"available": False, "reason": "root_not_directory"}
        if not _path_is_safe_directory(root):
            return {"available": False, "reason": "root_unsafe"}
        candidates = [
            (label, path)
            for label, path in (
                ("nested", root / "ComfyUI" / "custom_nodes"),
                ("flat", root / "custom_nodes"),
            )
            if _path_is_safe_directory(path) and path.is_dir()
        ]
        if not candidates:
            return {
                "available": True,
                "layout": "none",
                "plugins": [],
                "total": 0,
                "scanned_entries": 0,
                "truncated": False,
            }
        plugins: dict[str, dict[str, Any]] = {}
        found_by_layout: dict[str, int] = {}
        scanned = 0
        truncated = False
        # True lazy iteration via os.scandir: the shared budget is consumed
        # per directory entry as it is visited and stops immediately; the
        # context manager closes the scan handle on exit.
        for label, candidate in candidates:
            try:
                with os.scandir(candidate) as iterator:
                    while True:
                        if scanned >= _PLUGIN_SCAN_BUDGET:
                            # Probe without processing: the probe consumes
                            # one entry and is counted, so scanned_entries
                            # always reflects real consumption.
                            try:
                                next(iterator)
                                scanned += 1
                                truncated = True
                            except StopIteration:
                                truncated = False
                            break
                        try:
                            entry = next(iterator)
                        except StopIteration:
                            break
                        scanned += 1
                        name = entry.name
                        if name.startswith(".") or name == "__pycache__":
                            continue
                        if not _PLUGIN_NAME.fullmatch(name):
                            continue
                        entry_path = Path(entry.path)
                        if not _path_is_safe_directory(entry_path):
                            continue
                        if not entry_path.is_dir():
                            continue
                        # Layout validity counts plugin directories before
                        # dedup: an empty dir is invalid, a dir whose plugin
                        # is later deduped is still a valid plugin dir.
                        found_by_layout[label] = found_by_layout.get(label, 0) + 1
                        key = name.casefold()
                        if key in plugins:
                            # Keep the first occurrence (nested scans first,
                            # so it wins cross-layout; same-layout duplicates
                            # keep the first-seen entry).
                            continue
                        plugins[key] = _plugin_entry(entry_path)
                    if truncated:
                        break
            except OSError:
                return {"available": False, "reason": "unreadable"}
        layout = "none"
        nested_count = found_by_layout.get("nested", 0)
        flat_count = found_by_layout.get("flat", 0)
        if nested_count and flat_count:
            layout = "merged"
        elif nested_count:
            layout = "nested"
        elif flat_count:
            layout = "flat"
        ordered = [plugins[key] for key in sorted(plugins)]
        return {
            "available": True,
            "server_id": server_id,
            "layout": layout,
            "plugins": ordered,
            "total": len(ordered),
            "scanned_entries": scanned,
            "truncated": truncated,
        }

    @staticmethod
    def _page(
        items: list[Any],
        *,
        query: str,
        limit: int,
        cursor: str,
        key: str = "",
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise WorkflowArgumentsError("limit must be an integer between 1 and 200")
        try:
            offset = int(cursor or "0")
        except ValueError as exc:
            raise WorkflowArgumentsError("cursor must be a non-negative integer") from exc
        if offset < 0:
            raise WorkflowArgumentsError("cursor must be a non-negative integer")
        needle = query.casefold()
        if needle:
            items = [
                item
                for item in items
                if needle
                in (
                    str(item.get(key, "")) if key and isinstance(item, dict) else str(item)
                ).casefold()
            ]
        page = items[offset : offset + limit]
        next_offset = offset + len(page)
        return {
            "items": page,
            "next_cursor": str(next_offset) if next_offset < len(items) else "",
            "total": len(items),
        }
