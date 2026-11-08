"""Bounded, structured facts about why one Job failed.

ComfyUI reports failure facts in ``/history`` as ``status.messages`` entries of
``["execution_error", {...}]``. The control plane keeps only the fields an agent
can act on -- node identity, the failing input, the engine message, and model
names the engine itself reported -- and never persists tracebacks, absolute
paths, or raw input snapshots. The whole document is capped so one bad workflow
cannot bloat the database.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

MAX_FAILURE_BYTES = 4096
MAX_MESSAGE_CHARS = 1024
MAX_FAILURE_ITEMS = 5
MAX_ITEM_CHARS = 128
_FAILURE_TEXT_FIELDS = ("node_id", "class_type", "input_name", "error_type", "message")
_FAILURE_LIST_FIELDS = ("requested", "missing_models", "candidates")
_CANDIDATE_LIST = re.compile(r"not in \[(?P<items>[^\]]*)\]")
_QUOTED_ITEM = re.compile(r"'([^']{1,128})'")


def _text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "").strip()[:limit]


def _items(value: object) -> list[str]:
    """Return at most ``MAX_FAILURE_ITEMS`` distinct bounded strings."""
    if isinstance(value, str):
        candidates: list[object] = [value]
    elif isinstance(value, (list, tuple)):
        candidates = list(value)
    else:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        text = _text(item, limit=MAX_ITEM_CHARS)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) == MAX_FAILURE_ITEMS:
            break
    return result


def _candidate_models(message: str) -> list[str]:
    """Read ComfyUI's own ``... not in ['a', 'b']`` option list.

    Only that canonical shape is read; an arbitrary message is never mined for
    names, so a candidate list always reflects what the engine printed.
    """
    match = _CANDIDATE_LIST.search(message)
    if match is None:
        return []
    return _items(_QUOTED_ITEM.findall(match.group("items")))


def _bounded(failure: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(failure, ensure_ascii=False, allow_nan=False, sort_keys=True)
    if len(encoded.encode("utf-8")) <= MAX_FAILURE_BYTES:
        return failure
    # Keep identity and the head of the message; drop the optional lists.
    trimmed = {**failure, "message": failure["message"][:512]}
    for field in _FAILURE_LIST_FIELDS:
        trimmed[field] = []
    return trimmed


def summarize_failure(history: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Extract bounded failure facts from one ComfyUI ``/history`` document."""
    if not isinstance(history, Mapping):
        return None
    status = history.get("status")
    messages = status.get("messages") if isinstance(status, Mapping) else None
    if not isinstance(messages, list):
        return None
    for entry in reversed(messages):
        if not isinstance(entry, list) or len(entry) < 2 or entry[0] != "execution_error":
            continue
        payload = entry[1]
        if not isinstance(payload, Mapping):
            return None

        message = _text(payload.get("exception_message"), limit=MAX_MESSAGE_CHARS)
        current_inputs = payload.get("current_inputs")
        input_name = ""
        requested: list[str] = []
        if isinstance(current_inputs, Mapping) and current_inputs:
            input_name = _text(next(iter(current_inputs)), limit=MAX_ITEM_CHARS)
            requested = _items(next(iter(current_inputs.values())))
        missing_models = _items(payload.get("missing_models", payload.get("missing", ())))

        failure: dict[str, Any] = {
            "node_id": _text(payload.get("node_id"), limit=MAX_ITEM_CHARS),
            "class_type": _text(payload.get("node_type"), limit=MAX_ITEM_CHARS),
            "input_name": input_name,
            "error_type": _text(payload.get("exception_type"), limit=MAX_ITEM_CHARS),
            "message": message,
            "requested": requested,
            "missing_models": missing_models,
            # A rejected input value is the best structured hint we have; the
            # engine's own option list is only kept when it printed one.
            "candidates": [] if missing_models else _candidate_models(message),
        }
        if not any(failure[field] for field in (*_FAILURE_TEXT_FIELDS, *_FAILURE_LIST_FIELDS)):
            return None
        return _bounded(failure)
    return None


def failure_to_json(failure: Mapping[str, Any] | None) -> str:
    """Serialize one bounded failure document, or ``""`` when there is none."""
    if not failure:
        return ""
    return json.dumps(
        dict(failure),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def failure_from_json(value: object) -> dict[str, Any] | None:
    """Read a persisted failure document, ignoring anything malformed."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


__all__ = [
    "MAX_FAILURE_BYTES",
    "failure_from_json",
    "failure_to_json",
    "summarize_failure",
]
