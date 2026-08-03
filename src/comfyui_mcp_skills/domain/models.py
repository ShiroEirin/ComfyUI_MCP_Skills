"""Immutable domain models shared by CLI and MCP adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from comfyui_mcp_skills.domain.media import validate_media_locator


@dataclass(frozen=True, slots=True)
class Asset:
    asset_id: str
    server_id: str
    comfyui_ref: str
    name: str
    subfolder: str
    media_type: Literal["image", "audio", "video"]
    mime_type: str
    size_bytes: int
    sha256: str
    owner_id: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def resource_uri(self) -> str:
        return f"comfyui://assets/{self.asset_id}"

    def to_public_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["resource_uri"] = self.resource_uri
        result.pop("owner_id", None)
        try:
            name, subfolder = validate_media_locator(self.name, self.subfolder)
        except ValueError:
            result.pop("name", None)
            result.pop("subfolder", None)
            result.pop("comfyui_ref", None)
        else:
            result["name"] = name
            result["subfolder"] = subfolder
            result["comfyui_ref"] = f"{subfolder}/{name}" if subfolder else name
        return result


@dataclass(frozen=True, slots=True)
class Artifact:
    """Immutable identity and progressively verified facts for one Job output."""

    artifact_id: str
    job_id: str
    server_id: str
    upstream_node_id: str
    output_key: str
    upstream_output_index: int
    filename: str
    subfolder: str
    storage_type: str
    media_type: Literal["image", "audio", "video"]
    digest: str
    created_at: str
    mime_type: str = ""
    size_bytes: int | None = None
    sha256: str = ""
    completeness: Literal["locator_only", "verified"] = "locator_only"

    @property
    def resource_uri(self) -> str:
        return f"comfyui://artifacts/{self.artifact_id}"

    @property
    def lineage_uri(self) -> str:
        return f"comfyui://lineage/{self.artifact_id}"

    def to_public_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "artifact_id": self.artifact_id,
            "job_id": self.job_id,
            "media_type": self.media_type,
            "completeness": self.completeness,
            "created_at": self.created_at,
            "resource_uri": self.resource_uri,
            "lineage_uri": self.lineage_uri,
        }
        if self.mime_type:
            result["mime_type"] = self.mime_type
        if self.completeness == "verified":
            result["size_bytes"] = self.size_bytes
            result["sha256"] = self.sha256
        return result


@dataclass(frozen=True, slots=True)
class Workflow:
    server_id: str
    workflow_id: str
    description: str
    parameters: dict[str, Any]
    graph: dict[str, Any]
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class Server:
    server_id: str
    url: str
    name: str
    enabled: bool = True
    output_dir: str = "./outputs"


@dataclass(frozen=True, slots=True)
class Job:
    prompt_id: str
    server_id: str
    workflow_id: str
    status: str
    outputs: tuple[dict[str, Any], ...] = ()
    error: str = ""
    idempotency_key: str = ""
    client_id: str = ""
    request_digest: str = ""
    owner_id: str = ""
    job_id: str = ""
    plan_id: str = ""
    revision_id: str = ""
    deployment_id: str = ""
    plan_digest: str = ""
