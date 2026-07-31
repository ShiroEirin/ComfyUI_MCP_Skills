"""Capability-aware, bounded, and redacted ComfyUI observability services."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from itertools import islice
from typing import Any

from comfyui_mcp_skills.application.ports import ComfyUIGateway
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import ExecutionFailed, WorkflowArgumentsError

GatewayFactory = Callable[[dict[str, Any]], ComfyUIGateway]

_CAPABILITY_STATES = frozenset(
    {"supported", "unsupported", "unauthorized", "temporarily_unavailable"}
)
_CAPABILITY_NAMES = (
    "jobs_api",
    "userdata_v2",
    "userdata_traditional",
    "userdata",
    "node_replacements",
    "manager_queue_status",
    "manager_install",
    "logs",
    "workflow_templates",
    "subgraphs",
)
_PUBLIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z")
_MAX_SOURCE_ITEMS = 10_000
_MAX_NORMALIZED_DEPTH = 8
_MAX_NORMALIZED_ITEMS = 1_000
_MAX_STRING_LENGTH = 4_096
_MAX_GRAPH_SUMMARY_BYTES = 1_000_000

_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)(?:[\"'])?\b(authorization|api[_ -]?key|access[_ -]?token|"
    r"refresh[_ -]?token|token|password|passwd|secret|cookie)\b(?:[\"'])?"
    r"\s*([:=])\s*[^\r\n]*"
)
_PROMPT_ASSIGNMENT = re.compile(
    r"(?i)(?:[\"'])?\b(raw[_ -]?prompt|positive[_ -]?prompt|"
    r"negative[_ -]?prompt|prompt)\b(?:[\"'])?\s*([:=])\s*[^\r\n]*"
)
_AUTHORIZATION_VALUE = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{4,}")
_STANDALONE_SECRET = re.compile(r"(?i)\b(?:sk|api|token)-[A-Za-z0-9_-]{8,}\b")
_ESCAPED_SENSITIVE_KEY = re.compile(
    r"(?i)\\[\"'](?:authorization|api[_ -]?key|access[_ -]?token|"
    r"refresh[_ -]?token|token|password|passwd|secret|cookie|raw[_ -]?prompt|"
    r"positive[_ -]?prompt|negative[_ -]?prompt|prompt)\\[\"']\s*:"
)
_STRUCTURED_SENSITIVE_MARKER = re.compile(
    r"(?i)(?:\\?[\"'])?(?:graph|nodes|links|authorization|api[_ -]?key|"
    r"access[_ -]?token|refresh[_ -]?token|token|password|passwd|secret|cookie|"
    r"raw[_ -]?prompt|positive[_ -]?prompt|negative[_ -]?prompt|prompt)"
    r"(?:\\?[\"'])?\s*[:=]"
)
_WINDOWS_PATH = re.compile(r"(?i)(?<![A-Za-z0-9])(?:(?:[A-Z]:[\\/])|(?:\\\\|//))[^\r\n\t,;\"']+")
_POSIX_PATH = re.compile(r"(?<![:\w])/(?:[^/\s]+/)+[^,\s;\"']*")
_HOME_PATH = re.compile(r"(?<!\w)~[/\\][^\r\n\t,;\"']+")
_RELATIVE_SENSITIVE_PATH = re.compile(
    r"(?i)(?<![\w./\\])(?:data|models?|input|output|uploads?|users?|home|tmp|temp|"
    r"workspace|projects?)"
    r"[\\/][^\r\n\t,;\"']+"
)


def _canonical_key(key: object) -> str:
    value = re.sub(r"(?<!^)(?=[A-Z])", "_", str(key)).lower()
    return re.sub(r"[^a-z0-9]+", "_", value).strip("_")


def _excluded_key(key: object) -> bool:
    canonical = _canonical_key(key)
    tokens = set(canonical.split("_"))
    if canonical in {"prompt_id", "upstream_prompt_id", "workflow_id"}:
        return False
    if tokens & {
        "auth",
        "authorization",
        "authentication",
        "token",
        "password",
        "passwd",
        "secret",
        "cookie",
        "cookies",
    }:
        return True
    if canonical in {"api_key", "apikey"} or {"api", "key"} <= tokens:
        return True
    if "prompt" in tokens:
        return True
    if canonical in {
        "workflow",
        "workflow_graph",
        "graph",
        "nodes",
        "links",
        "extra_data",
    }:
        return True
    if "path" in tokens or canonical in {
        "filepath",
        "directory",
        "folder",
        "subfolder",
        "root",
        "cwd",
        "working_directory",
        "url",
        "uri",
    }:
        return True
    return canonical.endswith("_url") or canonical.endswith("_uri")


def _redact_string(value: str) -> str:
    candidate = value.strip()
    if _STRUCTURED_SENSITIVE_MARKER.search(value):
        return "[REDACTED]"
    for _ in range(_MAX_NORMALIZED_DEPTH):
        if not candidate:
            break
        if len(candidate) > _MAX_STRING_LENGTH:
            return "[REDACTED]"
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError, ValueError):
            break
        if isinstance(parsed, str):
            candidate = parsed.strip()
            value = parsed
            continue
        if isinstance(parsed, (Mapping, list, tuple)):
            normalized = normalize_observation(parsed, _depth=1)
            return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        break

    else:
        return "[REDACTED]"

    if _ESCAPED_SENSITIVE_KEY.search(value):
        return "[REDACTED]"

    def credential_replacement(match: re.Match[str]) -> str:
        separator = ":" if match.group(2) == ":" else "="
        return f"{match.group(1)}{separator} [REDACTED]"

    def prompt_replacement(match: re.Match[str]) -> str:
        separator = ":" if match.group(2) == ":" else "="
        return f"{match.group(1)}{separator} [REDACTED]"

    value = _CREDENTIAL_ASSIGNMENT.sub(credential_replacement, value)
    value = _PROMPT_ASSIGNMENT.sub(prompt_replacement, value)
    value = _AUTHORIZATION_VALUE.sub("[REDACTED]", value)
    value = _STANDALONE_SECRET.sub("[REDACTED]", value)
    value = _WINDOWS_PATH.sub("[REDACTED_PATH]", value)
    value = _POSIX_PATH.sub("[REDACTED_PATH]", value)
    value = _HOME_PATH.sub("[REDACTED_PATH]", value)
    value = _RELATIVE_SENSITIVE_PATH.sub("[REDACTED_PATH]", value)
    if len(value) > _MAX_STRING_LENGTH:
        return value[: _MAX_STRING_LENGTH - 3] + "..."
    return value


def normalize_observation(value: Any, *, _depth: int = 0) -> Any:
    """Recursively convert untrusted upstream values to bounded, redacted JSON values."""
    if _depth > _MAX_NORMALIZED_DEPTH:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, bytes):
        return "[BINARY]"
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_NORMALIZED_ITEMS or _excluded_key(key):
                continue
            raw_key = str(key)
            safe_key = (
                raw_key if _PUBLIC_ID.fullmatch(raw_key) else _safe_identifier(raw_key, "field")
            )
            normalized[safe_key] = normalize_observation(item, _depth=_depth + 1)
        return normalized
    if isinstance(value, Sequence):
        return [
            normalize_observation(item, _depth=_depth + 1) for item in value[:_MAX_NORMALIZED_ITEMS]
        ]
    return _redact_string(str(value))


def _validate_limit(limit: int, maximum: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise WorkflowArgumentsError(f"limit must be an integer between 1 and {maximum}")


def _encode_cursor(kind: str, offset: int, scope_digest: str) -> str:
    payload = json.dumps(
        {"v": 1, "kind": kind, "offset": offset, "scope": scope_digest},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(kind: str, cursor: str, scope_digest: str) -> int:
    if not cursor:
        return 0
    if not isinstance(cursor, str) or len(cursor) > 512:
        raise WorkflowArgumentsError("cursor is invalid")
    try:
        encoded = cursor.encode("ascii")
        padding = b"=" * (-len(encoded) % 4)
        payload = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        decoded = json.loads(payload)
    except (UnicodeEncodeError, ValueError, json.JSONDecodeError) as exc:
        raise WorkflowArgumentsError("cursor is invalid") from exc
    if not isinstance(decoded, dict) or set(decoded) != {"v", "kind", "offset", "scope"}:
        raise WorkflowArgumentsError("cursor is invalid")
    offset = decoded.get("offset")
    if (
        decoded.get("v") != 1
        or decoded.get("kind") != kind
        or not hmac.compare_digest(str(decoded.get("scope", "")), scope_digest)
        or isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
    ):
        raise WorkflowArgumentsError("cursor is invalid")
    return offset


def _page(
    server_id: str,
    capability_state: str,
    items: list[dict[str, Any]],
    *,
    kind: str,
    limit: int,
    maximum: int,
    cursor: str,
) -> dict[str, Any]:
    _validate_limit(limit, maximum)
    snapshot = json.dumps(
        {"server_id": server_id, "state": capability_state, "items": items},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    scope_digest = hashlib.sha256(snapshot).hexdigest()
    offset = _decode_cursor(kind, cursor, scope_digest)
    selected = items[offset : offset + limit]
    next_offset = offset + len(selected)
    return {
        "server_id": server_id,
        "capability_state": capability_state,
        "items": selected,
        "next_cursor": (
            _encode_cursor(kind, next_offset, scope_digest) if next_offset < len(items) else ""
        ),
        "total": len(items),
    }


def _optional_payload(result: object) -> tuple[str, Any]:
    if not isinstance(result, dict):
        return "temporarily_unavailable", None
    state = result.get("state")
    if state not in _CAPABILITY_STATES:
        return "temporarily_unavailable", None
    return str(state), result.get("data")


def _safe_identifier(value: object, prefix: str, index: int = 0) -> str:
    candidate = str(value or "")
    if _PUBLIC_ID.fullmatch(candidate):
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8", errors="replace")).hexdigest()[:20]
    return f"{prefix}-{digest or index}"


def _safe_text(value: object) -> str:
    normalized = normalize_observation(value)
    return normalized if isinstance(normalized, str) else ""


def _unwrap_collection(raw: object, wrappers: tuple[str, ...]) -> object:
    if isinstance(raw, dict):
        for wrapper in wrappers:
            wrapped = raw.get(wrapper)
            if isinstance(wrapped, (dict, list, tuple)):
                return wrapped
    return raw


class ObservationService:
    """Expose safe runtime observations without polling or retaining upstream payloads."""

    def __init__(self, servers: ServerRegistry, gateway_factory: GatewayFactory) -> None:
        self._servers = servers
        self._gateway_factory = gateway_factory

    def queue(self, server_id: str, *, limit: int = 50, cursor: str = "") -> dict[str, Any]:
        _validate_limit(limit, 200)
        gateway = self._gateway(server_id)
        raw = gateway.get_queue()
        if not isinstance(raw, dict):
            raise ExecutionFailed("ComfyUI queue response is invalid")
        items: list[dict[str, Any]] = []
        for upstream_key, state in (("queue_running", "running"), ("queue_pending", "pending")):
            entries = raw.get(upstream_key, [])
            if not isinstance(entries, list):
                continue
            for entry in entries[:_MAX_SOURCE_ITEMS]:
                item = self._queue_item(entry, state)
                if item is not None:
                    items.append(item)
        return _page(
            server_id,
            "supported",
            items,
            kind="queue",
            limit=limit,
            maximum=200,
            cursor=cursor,
        )

    def logs(self, server_id: str, *, limit: int = 100, cursor: str = "") -> dict[str, Any]:
        _validate_limit(limit, 1_000)
        gateway = self._gateway(server_id)
        state, raw = _optional_payload(gateway.get_logs())
        items = self._log_lines(raw) if state == "supported" else []
        items.reverse()
        return _page(
            server_id,
            state,
            items,
            kind="logs",
            limit=limit,
            maximum=1_000,
            cursor=cursor,
        )

    def capabilities(self, server_id: str) -> dict[str, Any]:
        gateway = self._gateway(server_id)
        overall_state, raw = _optional_payload(gateway.get_capabilities())
        source = raw if overall_state == "supported" and isinstance(raw, dict) else {}
        capabilities: dict[str, dict[str, str]] = {}
        for name in _CAPABILITY_NAMES:
            entry = source.get(name)
            state = overall_state
            if overall_state == "supported":
                state = (
                    str(entry.get("state"))
                    if isinstance(entry, dict) and entry.get("state") in _CAPABILITY_STATES
                    else "temporarily_unavailable"
                )
            normalized = {"state": state}
            if name == "userdata":
                variant = entry.get("variant", "") if isinstance(entry, dict) else ""
                normalized["variant"] = (
                    str(variant)
                    if state == "supported" and variant in {"v2", "traditional"}
                    else ""
                )
            capabilities[name] = normalized
        return {"server_id": server_id, "capabilities": capabilities}

    def templates(self, server_id: str, *, limit: int = 50, cursor: str = "") -> dict[str, Any]:
        _validate_limit(limit, 200)
        gateway = self._gateway(server_id)
        state, raw = _optional_payload(gateway.get_workflow_templates())
        items = self._template_items(raw) if state == "supported" else []
        return _page(
            server_id,
            state,
            items,
            kind="templates",
            limit=limit,
            maximum=200,
            cursor=cursor,
        )

    def subgraphs(self, server_id: str, *, limit: int = 50, cursor: str = "") -> dict[str, Any]:
        _validate_limit(limit, 200)
        gateway = self._gateway(server_id)
        state, raw = _optional_payload(gateway.get_subgraphs())
        items = self._subgraph_items(raw) if state == "supported" else []
        return _page(
            server_id,
            state,
            items,
            kind="subgraphs",
            limit=limit,
            maximum=200,
            cursor=cursor,
        )

    def subgraph(self, server_id: str, subgraph_id: str) -> dict[str, Any]:
        if not isinstance(subgraph_id, str) or _PUBLIC_ID.fullmatch(subgraph_id) is None:
            raise WorkflowArgumentsError("subgraph_id is invalid")
        gateway = self._gateway(server_id)
        state, raw = _optional_payload(gateway.get_subgraph(subgraph_id))
        item = self._subgraph_metadata(subgraph_id, raw, include_counts=True)
        return {
            "server_id": server_id,
            "capability_state": state,
            "subgraph": item if state == "supported" else None,
        }

    def free(
        self,
        server_id: str,
        *,
        unload_models: bool,
        free_memory: bool,
    ) -> dict[str, Any]:
        if not isinstance(unload_models, bool) or not isinstance(free_memory, bool):
            raise WorkflowArgumentsError("unload_models and free_memory must be booleans")
        if not unload_models and not free_memory:
            raise WorkflowArgumentsError("at least one memory release action must be selected")
        gateway = self._gateway(server_id)
        raw = gateway.free_memory(
            unload_models=unload_models,
            free_memory=free_memory,
        )
        if not isinstance(raw, dict) or type(raw.get("success")) is not bool:
            raise ExecutionFailed("ComfyUI memory release outcome is unknown")
        success = raw["success"]
        impact = []
        if success:
            if unload_models:
                impact.append("loaded_models")
            if free_memory:
                impact.append("runtime_memory")
        return {
            "server_id": server_id,
            "success": success,
            "unload_models": unload_models,
            "free_memory": free_memory,
            "impact": impact,
            "audit_status": "not_configured",
        }

    def _gateway(self, server_id: str) -> ComfyUIGateway:
        return self._gateway_factory(self._servers.connection(server_id))

    @staticmethod
    def _queue_item(entry: object, state: str) -> dict[str, Any] | None:
        item: dict[str, Any] = {"state": state}
        if isinstance(entry, (list, tuple)):
            if (
                entry
                and isinstance(entry[0], (int, float))
                and not isinstance(entry[0], bool)
                and (not isinstance(entry[0], float) or math.isfinite(entry[0]))
            ):
                item["queue_number"] = entry[0]
            if len(entry) > 1 and isinstance(entry[1], str):
                item["prompt_id"] = _safe_text(entry[1])
        elif isinstance(entry, dict):
            queue_number = entry.get("queue_number", entry.get("number"))
            if (
                isinstance(queue_number, (int, float))
                and not isinstance(queue_number, bool)
                and (not isinstance(queue_number, float) or math.isfinite(queue_number))
            ):
                item["queue_number"] = queue_number
            for field in ("prompt_id", "job_id"):
                value = entry.get(field)
                if isinstance(value, str):
                    item[field] = _safe_text(value)
        else:
            return None
        return item if len(item) > 1 else None

    @staticmethod
    def _log_lines(raw: object) -> list[dict[str, Any]]:
        if isinstance(raw, dict):
            raw = raw.get("entries", raw.get("logs", []))
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return []
        lines: list[dict[str, Any]] = []
        for entry in raw[:_MAX_SOURCE_ITEMS]:
            timestamp = ""
            level = ""
            message: object = entry
            if isinstance(entry, dict):
                timestamp = _safe_text(
                    entry.get("timestamp", entry.get("time", entry.get("t", "")))
                )
                level = _safe_text(entry.get("level", ""))
                message = entry.get("message", entry.get("m", entry.get("text", "")))
            if isinstance(message, str):
                text = message
            else:
                normalized = normalize_observation(message)
                text = json.dumps(
                    normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            for line in text.splitlines() or [""]:
                item: dict[str, Any] = {"message": _redact_string(line)}
                if timestamp:
                    item["timestamp"] = timestamp
                if level:
                    item["level"] = level
                lines.append(item)
                if len(lines) >= _MAX_SOURCE_ITEMS:
                    return lines
        return lines

    @staticmethod
    def _template_items(raw: object) -> list[dict[str, Any]]:
        raw = _unwrap_collection(raw, ("templates", "workflow_templates", "items"))
        entries: list[tuple[object, object]]
        if isinstance(raw, dict):
            entries = list(islice(raw.items(), _MAX_SOURCE_ITEMS))
        elif isinstance(raw, (list, tuple)):
            entries = list(enumerate(raw[:_MAX_SOURCE_ITEMS]))
        else:
            return []
        items: list[dict[str, Any]] = []
        for index, (key, value) in enumerate(entries):
            metadata = value if isinstance(value, dict) else {}
            identifier = metadata.get("template_id", metadata.get("id", key))
            template_id = _safe_identifier(identifier, "template", index)
            items.append(
                {
                    "template_id": template_id,
                    "name": _safe_text(metadata.get("name", metadata.get("title", template_id))),
                    "description": _safe_text(metadata.get("description", "")),
                    "category": _safe_text(metadata.get("category", "")),
                    "source": _safe_text(metadata.get("source", "")),
                }
            )
        items.sort(key=lambda item: (item["template_id"].casefold(), item["template_id"]))
        return items

    @classmethod
    def _subgraph_items(cls, raw: object) -> list[dict[str, Any]]:
        raw = _unwrap_collection(raw, ("subgraphs", "items"))
        entries: list[tuple[object, object]]
        if isinstance(raw, dict):
            entries = list(islice(raw.items(), _MAX_SOURCE_ITEMS))
        elif isinstance(raw, (list, tuple)):
            entries = list(enumerate(raw[:_MAX_SOURCE_ITEMS]))
        else:
            return []
        items: list[dict[str, Any]] = []
        for index, (key, value) in enumerate(entries):
            item = cls._subgraph_metadata(str(key), value, include_counts=False, index=index)
            if item is not None:
                items.append(item)
        items.sort(key=lambda item: (item["subgraph_id"].casefold(), item["subgraph_id"]))
        return items

    @staticmethod
    def _subgraph_metadata(
        subgraph_id: str,
        raw: object,
        *,
        include_counts: bool,
        index: int = 0,
    ) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        identifier = raw.get("subgraph_id", raw.get("id", subgraph_id))
        safe_id = _safe_identifier(identifier, "subgraph", index)
        info = raw.get("info")
        node_pack = info.get("node_pack", "") if isinstance(info, dict) else ""
        item: dict[str, Any] = {
            "subgraph_id": safe_id,
            "name": _safe_text(raw.get("name", safe_id)),
            "source": _safe_text(raw.get("source", "")),
            "node_pack": _safe_text(node_pack),
        }
        if include_counts:
            graph = raw.get("data", raw)
            if isinstance(graph, str) and len(graph.encode("utf-8")) <= _MAX_GRAPH_SUMMARY_BYTES:
                try:
                    graph = json.loads(graph)
                except (TypeError, ValueError, json.JSONDecodeError):
                    graph = {}
            if isinstance(graph, dict):
                nodes = graph.get("nodes")
                links = graph.get("links")
                if isinstance(nodes, (dict, list, tuple)):
                    item["node_count"] = len(nodes)
                if isinstance(links, (dict, list, tuple)):
                    item["link_count"] = len(links)
        return item
