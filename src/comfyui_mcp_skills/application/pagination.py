"""Opaque filter-bound keyset cursors for bounded application listings."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Mapping

_CURSOR_VERSION = 1
_MAX_CURSOR_LENGTH = 2048


def encode_keyset_cursor(
    created_at: str,
    item_id: str,
    *,
    filters: Mapping[str, str],
) -> str:
    """Encode a keyset position without exposing filter values."""
    payload = [_CURSOR_VERSION, _filter_digest(filters), created_at, item_id]
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def decode_keyset_cursor(
    cursor: str,
    *,
    filters: Mapping[str, str],
) -> tuple[str, str]:
    """Decode a cursor and reject malformed or differently filtered positions."""
    if (
        not isinstance(cursor, str)
        or not cursor
        or len(cursor) > _MAX_CURSOR_LENGTH
        or not cursor.isascii()
        or "=" in cursor
    ):
        raise ValueError("cursor is malformed")
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("ascii"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("cursor is malformed") from exc
    if (
        not isinstance(payload, list)
        or len(payload) != 4
        or payload[0] != _CURSOR_VERSION
        or not all(isinstance(value, str) for value in payload[1:])
        or not payload[2]
        or not payload[3]
    ):
        raise ValueError("cursor is malformed")
    if not hmac.compare_digest(payload[1], _filter_digest(filters)):
        raise ValueError("cursor does not match the requested filters")
    return payload[2], payload[3]


def _filter_digest(filters: Mapping[str, str]) -> str:
    canonical = json.dumps(
        dict(filters),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()
