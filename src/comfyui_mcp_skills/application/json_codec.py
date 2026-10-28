"""One canonical JSON encoder and digest for every application service.

The same serialization options were copied into planning, routing, retries,
and the asset library. Keeping one implementation prevents a digest from
silently diverging when only one copy is adjusted.
"""

from __future__ import annotations

import hashlib
import json


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_json_bytes(value: object) -> bytes:
    return canonical_json(value).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
