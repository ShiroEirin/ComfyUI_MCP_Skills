"""Static community-consensus guidance for model families.

These are experience-based starting points (sampler/scheduler/steps/CFG/
resolution) compiled from common community practice; they are NOT engine
guarantees and never override server-side validation. Evidence-driven
suggestions from local run history live in ``SuggestionService``.
"""

from __future__ import annotations

from typing import Any

_MODEL_GUIDANCE: dict[str, dict[str, Any]] = {
    "sd1.5": {
        "family": "Stable Diffusion 1.5",
        "sampler": "euler_a",
        "scheduler": "normal",
        "steps": 25,
        "cfg": 7.0,
        "resolution": 512,
    },
    "sdxl": {
        "family": "Stable Diffusion XL",
        "sampler": "dpmpp_2m",
        "scheduler": "karras",
        "steps": 30,
        "cfg": 7.0,
        "resolution": 1024,
    },
    "sd3": {
        "family": "Stable Diffusion 3 / 3.5",
        "sampler": "euler",
        "scheduler": "simple",
        "steps": 28,
        "cfg": 5.0,
        "resolution": 1024,
    },
    "flux": {
        "family": "FLUX.1",
        "sampler": "euler",
        "scheduler": "simple",
        "steps": 28,
        "cfg": 1.0,
        "resolution": 1024,
    },
    "pony": {
        "family": "Pony Diffusion",
        "sampler": "dpmpp_2m",
        "scheduler": "karras",
        "steps": 30,
        "cfg": 7.0,
        "resolution": 1024,
    },
    "illustrious": {
        "family": "Illustrious",
        "sampler": "euler_a",
        "scheduler": "normal",
        "steps": 28,
        "cfg": 6.0,
        "resolution": 1024,
    },
    "noobai": {
        "family": "NoobAI",
        "sampler": "euler_a",
        "scheduler": "normal",
        "steps": 30,
        "cfg": 6.0,
        "resolution": 1024,
    },
    "animagine": {
        "family": "Animagine XL",
        "sampler": "euler_a",
        "scheduler": "normal",
        "steps": 28,
        "cfg": 6.0,
        "resolution": 1024,
    },
    "realistic": {
        "family": "Realistic photo models",
        "sampler": "dpmpp_2m",
        "scheduler": "karras",
        "steps": 30,
        "cfg": 7.0,
        "resolution": 1024,
    },
}

_MAX_QUERY_LENGTH = 64


def _family_keys() -> list[str]:
    return sorted(_MODEL_GUIDANCE)


def list_families() -> list[str]:
    """All guidance keys (stable order)."""
    return _family_keys()


def guidance(query: str = "") -> dict[str, Any]:
    """Return guidance for the family best matching a free-text query.

    Matching is a casefolded substring scan over family keys and display
    names; an empty query returns the whole catalog. Unknown queries yield an
    empty match set instead of guessing.
    """
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    if len(query) > _MAX_QUERY_LENGTH:
        raise ValueError(f"query must be at most {_MAX_QUERY_LENGTH} characters")
    needle = query.strip().casefold()
    if not needle:
        return {"query": query, "items": [dict(_MODEL_GUIDANCE[key]) for key in _family_keys()]}
    matched: list[dict[str, Any]] = []
    for key in _family_keys():
        entry = _MODEL_GUIDANCE[key]
        haystack = f"{key} {entry.get('family', '')}".casefold()
        if needle in haystack:
            matched.append(dict(entry))
    return {"query": query, "items": matched}
