"""Read-only discovery of ComfyUI servers, nodes, and models."""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from comfyui_mcp_skills.application.assets import same_file_stat
from comfyui_mcp_skills.application.ports import ComfyUIGateway
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import WorkflowArgumentsError, WorkflowNotFound

GatewayFactory = Callable[[dict[str, Any]], ComfyUIGateway]

_PLUGIN_SCAN_BUDGET = 201
_MODEL_SCAN_BUDGET = 20001
_MODEL_SUFFIXES = frozenset(
    {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx"}
)
# Input types that render as widgets in the ComfyUI editor rather than as
# connectable ports; ``node_ports`` marks them (see ``_input_port``) instead of
# dropping them, so an agent sees link targets and fillable widgets alike.
_MAX_SAFETENSORS_HEADER = 4 * 1024 * 1024
# ``/object_info`` is a multi-megabyte payload on a real install and the gateway
# keeps no cache of its own, so a short TTL keeps repeated node/blueprint
# discovery off the network without serving a stale inventory for long.
_OBJECT_INFO_TTL_SECONDS = 60.0
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


def _normalized_model_name(name: str) -> str:
    """Compare model identifiers across platform separator spellings."""
    return name.replace("\\", "/")


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


def _local_root(
    connection: Mapping[str, Any],
) -> tuple[Path | None, dict[str, Any]]:
    """Resolve the configured ``local_root`` into a safe directory or a reason.

    Both local-session readers (``local.plugins`` and ``model.list`` metadata)
    share this gate so a missing, unreadable, or reparse-point root degrades with
    the same fixed reason codes instead of two slightly different checks.
    """
    local_root = connection.get("local_root")
    if not isinstance(local_root, str) or not local_root.strip():
        return None, {"available": False, "reason": "no_local_root"}
    root = Path(local_root)
    if not root.exists():
        return None, {"available": False, "reason": "root_not_found"}
    if not root.is_dir():
        return None, {"available": False, "reason": "root_not_directory"}
    if not _path_is_safe_directory(root):
        return None, {"available": False, "reason": "root_unsafe"}
    return root, {}


def _layout_candidates(root: Path, *parts: str) -> list[tuple[str, Path]]:
    """ComfyUI-owned directories for both supported install layouts.

    An aki-style bundle nests the engine one level down (``<root>/ComfyUI``)
    while a standard install keeps ``models``/``custom_nodes`` directly under
    the root. Every reader resolves its directory through this one helper, so
    the two layouts never diverge between features.
    """
    relative = Path(*parts)
    return [("nested", root / "ComfyUI" / relative), ("flat", root / relative)]


def _usable_layout_directories(root: Path, *parts: str) -> list[tuple[str, Path]]:
    """Layout candidates that exist as real, non-reparse directories."""
    return [
        (label, path)
        for label, path in _layout_candidates(root, *parts)
        if path.is_dir() and _path_is_safe_directory(path)
    ]


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


_PORT_MAX_OPTIONS = 64
_MAX_PORT_VALUE_CHARS = 128
# Custom nodes advertise runtime-populated dropdowns as a pseudo type instead
# of a plain option list; the real candidates only exist after the server
# evaluates the node, so the token is surfaced as a readable COMBO widget.
_DYNAMIC_COMBO_PREFIX = "COMFY_DYNAMICCOMBO"
_SCALAR_PORT_TYPES = frozenset({"INT", "FLOAT", "STRING", "BOOLEAN"})


def _port_options(value: object) -> list[str]:
    """Bounded COMBO candidates from a bare list or a ``{"value": ...}`` list."""
    if not isinstance(value, (list, tuple)):
        return []
    options: list[str] = []
    for option in value[:_PORT_MAX_OPTIONS]:
        candidate: object = option
        if isinstance(option, dict):
            candidate = option.get("value", option.get("name", ""))
        if isinstance(candidate, str) and candidate:
            options.append(candidate[:_MAX_PORT_VALUE_CHARS])
        elif isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            options.append(str(candidate))
    return options


def _port_value(value: object) -> object:
    """Project one advertised widget value, dropping values we cannot publish."""
    if value is None or isinstance(value, (bool, int, str)):
        return value if not isinstance(value, str) else value[:512]
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, (list, tuple)):
        projected = [_port_value(item) for item in value[:32]]
        return [item for item in projected if item is not None]
    return None


def _input_port(name: str, spec: list[Any], *, required: bool) -> dict[str, Any]:
    """Project one object_info input spec into a usable port description.

    object_info spells a field as ``[type, options]`` where ``type`` is either a
    widget token (``INT``/``FLOAT``/``STRING``/``BOOLEAN``), a combo option list,
    a dynamic-combo pseudo type, or a link type. Every port keeps the original
    ``name``/``type``/``required`` keys and adds the widget facts an agent needs
    to fill or connect it; unknown facts are omitted rather than invented.
    """
    raw_type = spec[0] if spec else None
    options: dict[str, Any] = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
    port: dict[str, Any] = {"name": name, "type": "unknown", "required": required}
    widget = False
    connectable = True
    force_input = bool(options.get("forceInput", False))
    if isinstance(raw_type, list):
        port["type"] = "COMBO"
        widget = True
        connectable = force_input
        values = _port_options(raw_type)
        if values:
            port["options"] = values
            port["enum"] = list(values)
    elif isinstance(raw_type, str):
        token = raw_type
        if token in _SCALAR_PORT_TYPES:
            port["type"] = token
            widget = True
            connectable = force_input
        elif token == "COMBO" or token.startswith(_DYNAMIC_COMBO_PREFIX):
            port["type"] = "COMBO"
            port["raw_type"] = token
            widget = True
            connectable = force_input
            if token.startswith(_DYNAMIC_COMBO_PREFIX):
                port["dynamic"] = True
            values = _port_options(options.get("options"))
            if values:
                port["options"] = values
                port["enum"] = list(values)
            elif token.startswith(_DYNAMIC_COMBO_PREFIX):
                # Never leave a caller with a type name it cannot resolve.
                port["options_missing"] = "dynamic_combo_options_unavailable"
        else:
            port["type"] = token
    else:
        port["type"] = _compact_type(raw_type)
    port["widget"] = widget
    port["connectable"] = connectable
    default = _port_value(options.get("default"))
    if default is not None:
        port["default"] = default
    current = default
    if "value" in options:
        advertised = _port_value(options["value"])
        current = advertised if advertised is not None else default
    if current is not None:
        # object_info is a schema, not live canvas state: ``current_value`` is
        # only the value this node definition itself advertises.
        port["current_value"] = current
    for key in ("min", "max", "step"):
        numeric = options.get(key)
        if isinstance(numeric, (int, float)) and not isinstance(numeric, bool):
            port[key] = numeric
    return port


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
        self._object_info_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._object_info_lock = threading.Lock()

    def _object_info(self, server_id: str) -> dict[str, Any]:
        """Return the server's node inventory, reusing a short-lived cached copy.

        ``/object_info`` is several megabytes on a real install and the gateway
        has no cache, so every node listing used to re-download and re-parse the
        whole inventory. The returned mapping is treated as read-only by callers.
        """
        now = time.monotonic()
        with self._object_info_lock:
            cached = self._object_info_cache.get(server_id)
            if cached is not None and now - cached[0] < _OBJECT_INFO_TTL_SECONDS:
                return cached[1]
        gateway = self._gateway_factory(self._servers.connection(server_id))
        raw = gateway.get_object_info()
        with self._object_info_lock:
            self._object_info_cache[server_id] = (time.monotonic(), raw)
        return raw

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
        raw = self._object_info(server_id)
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

    def node_ports(self, server_id: str, node_class: str) -> dict[str, Any]:
        """Project one node's inputs and outputs for graph editing.

        Every input keeps the original ``name``/``type``/``required`` keys and
        adds the widget facts an agent needs: ``widget``/``connectable`` tell a
        canvas widget apart from a link target, ``default``/``current_value``
        carry the value the node definition advertises, ``options``/``enum``
        carry combo candidates, and ``min``/``max``/``step`` bound numeric
        widgets. A dynamic combo pseudo type (``COMFY_DYNAMICCOMBO_*``) is
        reported as a readable ``COMBO`` with ``dynamic: true`` instead of an
        unresolvable type name. Facts the definition does not advertise are
        omitted rather than invented.
        """
        payload = self.node(server_id, node_class)
        node = payload["node"]
        inputs = node.get("input") if isinstance(node.get("input"), dict) else {}
        ports: list[dict[str, Any]] = []
        for section, required in (("required", True), ("optional", False)):
            group = inputs.get(section)
            if not isinstance(group, dict):
                continue
            for name, spec in group.items():
                if not isinstance(name, str):
                    continue
                if not isinstance(spec, list) or not spec:
                    continue
                ports.append(_input_port(name, spec, required=required))
        ports.sort(key=lambda port: str(port["name"]))
        raw_outputs = node.get("output")
        raw_names = node.get("output_name")
        outputs: list[dict[str, Any]] = []
        if isinstance(raw_outputs, list):
            for index, output_type in enumerate(raw_outputs):
                name = ""
                if isinstance(raw_names, list) and index < len(raw_names):
                    name = str(raw_names[index])
                outputs.append(
                    {
                        "index": index,
                        "name": name,
                        "type": str(output_type),
                        "connectable": True,
                    }
                )
        return {
            "server_id": server_id,
            "node_class": node_class,
            "inputs": ports,
            "outputs": outputs,
            "widget_count": sum(1 for port in ports if port["widget"]),
        }

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
        raw = self._object_info(server_id)
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
        include_metadata: bool = False,
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
        if include_metadata:
            page["metadata"] = self._model_metadata(server_id, kind, page["items"])
        return page

    def _model_metadata(
        self, server_id: str, kind: str, names: list[Any]
    ) -> dict[str, Any]:
        """Read on-disk facts for one page of model files (needs ``local_root``)."""
        root, unavailable = _local_root(self._servers.connection(server_id))
        if root is None:
            return unavailable
        wanted = [str(name) for name in names]
        # ComfyUI reports Windows sub-folder entries with a backslash while
        # ``Path.as_posix()`` yields forward slashes, so match on one
        # normalized form and report the caller's original spelling back.
        wanted_names = {_normalized_model_name(name): name for name in wanted}
        # The same nested/flat resolution ``local.plugins`` uses: an aki bundle
        # keeps models under ``<root>/ComfyUI/models``, a standard install under
        # ``<root>/models``. Nested wins when both exist.
        existing = [
            candidate
            for _label, candidate in _layout_candidates(root, "models", kind)
            if candidate.is_dir()
        ]
        if not existing:
            return {
                "available": True,
                "layout": "none",
                "files": [],
                "missing": wanted,
            }
        usable = _usable_layout_directories(root, "models", kind)
        if not usable:
            return {"available": False, "reason": "models_dir_unsafe"}
        layout, models_dir = usable[0]
        found: dict[str, Path] = {}
        scanned = 0
        for candidate in models_dir.rglob("*"):
            scanned += 1
            if scanned > _MODEL_SCAN_BUDGET:
                break
            if len(found) >= len(wanted):
                break
            if not candidate.is_file() or candidate.suffix.lower() not in _MODEL_SUFFIXES:
                continue
            if _is_reparse_or_link(candidate):
                continue
            relative = candidate.relative_to(models_dir).as_posix()
            original = wanted_names.get(relative)
            if original is not None:
                found[original] = candidate
        files: list[dict[str, Any]] = []
        missing: list[str] = []
        for name in wanted:
            matched = found.get(name)
            if matched is None:
                missing.append(name)
                continue
            try:
                info = matched.stat()
            except OSError:
                missing.append(name)
                continue
            files.append(
                {
                    "name": name,
                    "size_bytes": info.st_size,
                    "mtime": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(info.st_mtime)
                    ),
                    "metadata": _safetensors_metadata(matched),
                }
            )
        return {
            "available": True,
            "layout": layout,
            "files": files,
            "missing": missing,
        }

    def plugins(self, server_id: str) -> dict[str, Any]:
        """List custom_nodes plugins from a locally configured ComfyUI root.

        This is the local-session channel: the server entry must configure
        ``local_root`` (the ComfyUI installation root, e.g. an aki bundle);
        without it, or when the root is unsafe/unreadable, the tool reports
        ``available: false`` with a fixed reason code and never fabricates
        success. Cloud sessions (no local file access) get the same fallback.
        """
        connection = self._servers.connection(server_id)
        root, unavailable = _local_root(connection)
        if root is None:
            return unavailable
        candidates = _usable_layout_directories(root, "custom_nodes")
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


def _safetensors_metadata(path: Path) -> dict[str, str]:
    """Read the bounded ``__metadata__`` block of a safetensors file.

    Training metadata such as ``ss_tag_frequency`` (trigger words) lives there,
    so a caller can pick a LoRA without opening a multi-gigabyte file.
    """
    if path.suffix.lower() != ".safetensors":
        return {}
    try:
        with path.open("rb") as handle:
            length_raw = handle.read(8)
            if len(length_raw) != 8:
                return {}
            length = int.from_bytes(length_raw, "little")
            if length <= 0 or length > _MAX_SAFETENSORS_HEADER:
                return {}
            header = json.loads(handle.read(length).decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    if not isinstance(header, dict):
        return {}
    raw = header.get("__metadata__")
    if not isinstance(raw, dict):
        return {}
    return {
        str(key)[:128]: str(value)[:512]
        for key, value in raw.items()
        if isinstance(value, str)
    }
