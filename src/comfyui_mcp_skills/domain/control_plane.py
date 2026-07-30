"""Stable control-plane IDs, canonical URIs, and legacy URI aliases."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Literal, TypeAlias, get_args
from urllib.parse import urlsplit

from comfyui_mcp_skills.domain.identifiers import validate_identifier

ControlPlaneKind = Literal[
    "workflow",
    "revision",
    "deployment",
    "plan",
    "job",
    "attempt",
    "asset",
    "artifact",
]
CanonicalResourceKind = Literal["workflow", "deployment", "plan", "job", "asset", "artifact"]
LegacyResourceKind = Literal["workflow", "asset", "job", "output"]
IdentityComponent: TypeAlias = str | int | bool | None

_KINDS = frozenset(get_args(ControlPlaneKind))
_PREFIXES: dict[str, str] = {kind: f"{kind}_" for kind in _KINDS}
_RESOURCE_COLLECTIONS: dict[CanonicalResourceKind, str] = {
    "workflow": "workflows",
    "deployment": "deployments",
    "plan": "plans",
    "job": "jobs",
    "asset": "assets",
    "artifact": "artifacts",
}
_OPAQUE_ID = re.compile(r"[0-9a-f]{32}(?:[0-9a-f]{32})?\Z")
_LEGACY_RESOURCE_KINDS = frozenset({"workflow", "asset", "job", "output"})
_MAX_RESOURCE_URI_LENGTH = 2048
_MAX_OUTPUT_INDEX = 2_147_483_647
_VERSIONED_NAMESPACE = re.compile(r"[A-Za-z0-9_-]+-v[1-9][0-9]*\Z")
_OUTPUT_INDEX = re.compile(r"(?:0|[1-9][0-9]{0,9})\Z")
_SHA256_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_IDENTITY_COMPONENTS = 16
_MAX_IDENTITY_STRING_LENGTH = 4096
_MAX_CANONICAL_IDENTITY_BYTES = 16_384
_MIN_IDENTITY_INTEGER = -(2**63)
_MAX_IDENTITY_INTEGER = 2**63 - 1
_LEGACY_JOB_NAMESPACE = "legacy-job-v1"
_LEGACY_ARTIFACT_NAMESPACE = "legacy-artifact-v1"
_LEGACY_WORKFLOW_NAMESPACE = "legacy-workflow-v1"
_LEGACY_REVISION_NAMESPACE = "legacy-revision-v1"
_LEGACY_UNKNOWN_JOB_NAMESPACE = "legacy-unknown-v1"


@dataclass(frozen=True, slots=True)
class LegacyResourceRef:
    """A parsed key for one previously published Resource URI."""

    kind: LegacyResourceKind
    server_id: str
    upstream_id: str
    index: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in _LEGACY_RESOURCE_KINDS:
            raise ValueError(f"unsupported legacy Resource kind: {self.kind}")
        if not _safe_legacy_component(self.server_id) or not _safe_legacy_component(
            self.upstream_id
        ):
            raise ValueError("legacy Resource components must be safe identifiers")
        if self.kind == "output":
            if (
                isinstance(self.index, bool)
                or not isinstance(self.index, int)
                or not 0 <= self.index <= _MAX_OUTPUT_INDEX
            ):
                raise ValueError("legacy output index must be a non-negative 32-bit integer")
        elif self.index is not None:
            raise ValueError("only legacy output Resources may carry an index")


def new_control_plane_id(kind: ControlPlaneKind) -> str:
    """Create a random typed ID for a newly created control-plane object."""
    prefix = _prefix(kind)
    return f"{prefix}{uuid.uuid4().hex}"


def derived_control_plane_id(
    kind: ControlPlaneKind, namespace: str, components: list[IdentityComponent]
) -> str:
    """Derive a typed migration ID from a versioned canonical tuple."""
    prefix = _prefix(kind)
    namespace = validate_identifier(namespace, field="namespace")
    if _VERSIONED_NAMESPACE.fullmatch(namespace) is None:
        raise ValueError("namespace must end with a positive unpadded -vN version")
    if not isinstance(components, list) or len(components) > _MAX_IDENTITY_COMPONENTS:
        raise ValueError(
            f"identity components must be a list of at most {_MAX_IDENTITY_COMPONENTS} values"
        )
    for component in components:
        if not isinstance(component, (str, int, bool)) and component is not None:
            raise ValueError(
                "identity components must contain only strings, integers, booleans, or null"
            )
        if isinstance(component, str) and len(component) > _MAX_IDENTITY_STRING_LENGTH:
            raise ValueError("identity string component exceeds the 4096-character limit")
        if (
            isinstance(component, int)
            and not isinstance(component, bool)
            and not _MIN_IDENTITY_INTEGER <= component <= _MAX_IDENTITY_INTEGER
        ):
            raise ValueError("identity integer component exceeds the signed 64-bit range")
    payload = json.dumps(
        [kind, namespace, *components],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > _MAX_CANONICAL_IDENTITY_BYTES:
        raise ValueError("canonical identity payload exceeds the 16384-byte limit")
    return f"{prefix}{hashlib.sha256(payload).hexdigest()}"


def derive_legacy_job_id(server_id: str, prompt_id: str) -> str:
    """Derive the canonical ID for one legacy submitted Job."""
    server_id = validate_identifier(server_id, field="server_id")
    prompt_id = validate_identifier(prompt_id, field="prompt_id")
    return derived_control_plane_id("job", _LEGACY_JOB_NAMESPACE, [server_id, prompt_id])


def derive_legacy_artifact_id(
    job_id: str,
    upstream_node_id: str,
    output_key: str,
    output_index: int,
    filename: str,
    subfolder: str,
    storage_type: str,
) -> str:
    """Derive the canonical ID for one legacy output Artifact."""
    job_id = validate_control_plane_id("job", job_id)
    upstream_node_id = validate_identifier(upstream_node_id, field="upstream_node_id")
    output_key = validate_identifier(output_key, field="output_key")
    if (
        isinstance(output_index, bool)
        or not isinstance(output_index, int)
        or not 0 <= output_index <= _MAX_OUTPUT_INDEX
    ):
        raise ValueError("output_index must be a non-negative 32-bit integer")
    filename = _require_string(filename, field="filename", allow_empty=False)
    subfolder = _require_string(subfolder, field="subfolder", allow_empty=True)
    if storage_type != "output":
        raise ValueError("storage_type must be output")
    return derived_control_plane_id(
        "artifact",
        _LEGACY_ARTIFACT_NAMESPACE,
        [
            job_id,
            upstream_node_id,
            output_key,
            output_index,
            filename,
            subfolder,
            storage_type,
        ],
    )


def derive_legacy_conflicting_workflow_id(server_id: str, workflow_id: str) -> str:
    """Derive a project ID when a legacy Workflow name has conflicting content."""
    server_id = validate_identifier(server_id, field="server_id")
    workflow_id = validate_identifier(workflow_id, field="legacy workflow_id")
    return derived_control_plane_id(
        "workflow", _LEGACY_WORKFLOW_NAMESPACE, [server_id, workflow_id]
    )


def derive_legacy_revision_id(workflow_id: str, content_digest: str) -> str:
    """Derive the initial Revision ID for migrated Workflow content."""
    workflow_id = validate_control_plane_id("workflow", workflow_id)
    content_digest = _validate_sha256_digest(content_digest, field="content_digest")
    return derived_control_plane_id(
        "revision", _LEGACY_REVISION_NAMESPACE, [workflow_id, content_digest]
    )


def derive_legacy_unknown_job_id(
    owner_id: str,
    server_id: str,
    idempotency_key: str,
    request_digest: str,
) -> str:
    """Derive a conservative Job ID when legacy submission outcome is unknown."""
    owner_id = _require_string(owner_id, field="owner_id", allow_empty=True)
    server_id = validate_identifier(server_id, field="server_id")
    idempotency_key = _require_string(idempotency_key, field="idempotency_key", allow_empty=False)
    request_digest = _validate_sha256_digest(request_digest, field="request_digest")
    return derived_control_plane_id(
        "job",
        _LEGACY_UNKNOWN_JOB_NAMESPACE,
        [owner_id, server_id, idempotency_key, request_digest],
    )


def _require_string(value: object, *, field: str, allow_empty: bool) -> str:
    if not isinstance(value, str) or (not allow_empty and not value) or "\x00" in value:
        raise ValueError(f"{field} must be a valid string")
    return value


def _validate_sha256_digest(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    normalized = value.removeprefix("sha256:")
    if _SHA256_DIGEST.fullmatch(normalized) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return normalized


def validate_control_plane_id(kind: ControlPlaneKind, identifier: object) -> str:
    """Validate that an ID belongs to the requested control-plane kind."""
    prefix = _prefix(kind)
    if kind == "workflow":
        workflow_id = validate_identifier(identifier, field="workflow_id")
        if any(
            workflow_id.startswith(other_prefix)
            and _OPAQUE_ID.fullmatch(workflow_id.removeprefix(other_prefix)) is not None
            for other_kind, other_prefix in _PREFIXES.items()
            if other_kind != "workflow"
        ):
            raise ValueError("workflow_id must not equal another control-plane typed ID")
        return workflow_id
    if not isinstance(identifier, str) or not identifier.startswith(prefix):
        raise ValueError(f"{kind}_id must start with {prefix}")
    if _OPAQUE_ID.fullmatch(identifier.removeprefix(prefix)) is None:
        raise ValueError(f"{kind}_id must contain 32 or 64 lowercase hex characters")
    return identifier


def canonical_resource_uri(kind: CanonicalResourceKind, identifier: object) -> str:
    """Return the canonical Resource URI for a top-level control-plane object."""
    try:
        collection = _RESOURCE_COLLECTIONS[kind]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"{kind} has no top-level canonical Resource URI") from exc
    value = validate_control_plane_id(kind, identifier)
    return f"comfyui://{collection}/{value}"


def workflow_revision_uri(workflow_id: object, revision_id: object) -> str:
    """Return the canonical URI for an immutable Workflow Revision."""
    workflow = validate_control_plane_id("workflow", workflow_id)
    revision = validate_control_plane_id("revision", revision_id)
    return f"comfyui://workflows/{workflow}/revisions/{revision}"


def parse_legacy_resource_uri(uri: object) -> LegacyResourceRef | None:
    """Parse a published v1.1 server-scoped URI, rejecting unsafe shapes."""
    if (
        not isinstance(uri, str)
        or len(uri) > _MAX_RESOURCE_URI_LENGTH
        or "?" in uri
        or "#" in uri
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in uri)
    ):
        return None
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return None
    if parsed.scheme != "comfyui" or parsed.query or parsed.fragment:
        return None
    collection = parsed.netloc
    raw_parts = parsed.path.split("/")[1:]
    if any(not part or "%" in part for part in raw_parts):
        return None
    parts = raw_parts

    if collection in {"workflows", "assets", "jobs"} and len(parts) == 2:
        server_id, upstream_id = parts
        if not _safe_legacy_component(server_id) or not _safe_legacy_component(upstream_id):
            return None
        kind: LegacyResourceKind = {
            "workflows": "workflow",
            "assets": "asset",
            "jobs": "job",
        }[collection]
        return LegacyResourceRef(kind, server_id, upstream_id)

    if collection == "outputs" and len(parts) == 3:
        server_id, prompt_id, raw_index = parts
        if not _safe_legacy_component(server_id) or not _safe_legacy_component(prompt_id):
            return None
        if _OUTPUT_INDEX.fullmatch(raw_index) is None:
            return None
        index = int(raw_index)
        if index > _MAX_OUTPUT_INDEX:
            return None
        return LegacyResourceRef("output", server_id, prompt_id, index)
    return None


def _prefix(kind: str) -> str:
    try:
        return _PREFIXES[kind]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"unsupported control-plane kind: {kind}") from exc


def _safe_legacy_component(value: str) -> bool:
    try:
        validate_identifier(value, field="legacy URI component")
    except ValueError:
        return False
    return True
