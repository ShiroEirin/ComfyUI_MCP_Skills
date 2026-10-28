"""One revision identity payload shared by application and persistence.

The same ``identity_version=2`` document was built in four places. Only one of
them may define it, otherwise a future field change would make an application
digest and a stored digest disagree without any test noticing.
"""

from __future__ import annotations

from typing import Any

from comfyui_mcp_skills.application.json_codec import digest

IDENTITY_VERSION = 2


def revision_identity(
    graph: dict[str, Any],
    parameters: dict[str, Any],
    dependencies: dict[str, Any],
    output_contract: object,
    revision_metadata: object = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "identity_version": IDENTITY_VERSION,
        "graph": graph,
        "parameters": parameters,
        "dependencies": dependencies,
        "output_contract": output_contract,
    }
    if isinstance(revision_metadata, dict) and revision_metadata:
        value["revision_metadata"] = revision_metadata
    return value


def revision_digest(
    graph: dict[str, Any],
    parameters: dict[str, Any],
    dependencies: dict[str, Any],
    output_contract: object,
    revision_metadata: object = None,
) -> str:
    return digest(
        revision_identity(
            graph,
            parameters,
            dependencies,
            output_contract,
            revision_metadata,
        )
    )


def revision_digest_from_schema(
    graph: dict[str, Any],
    schema: dict[str, Any],
    dependencies: dict[str, Any],
) -> str:
    return revision_digest(
        graph,
        schema.get("parameters", {}),
        dependencies,
        schema.get("_output_contract"),
        schema.get("_revision"),
    )
