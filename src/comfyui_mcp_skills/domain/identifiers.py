"""Identifiers safe for on-disk server and workflow namespaces."""

from __future__ import annotations

import re

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def validate_identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} must be 1-128 ASCII letters, digits, underscores, or hyphens")
    return value
