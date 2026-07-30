"""Read-only resolution port for canonical and legacy Resource identities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

ResourceTargetKind = Literal["asset", "job", "artifact"]


@dataclass(frozen=True, slots=True)
class ResourceTarget:
    """An owner-authorized canonical target needed by the MCP Resource adapter."""

    kind: ResourceTargetKind
    canonical_uri: str
    object_id: str
    server_id: str
    prompt_id: str = ""
    filename: str = ""
    subfolder: str = ""
    storage_type: str = ""


class ResourceAliasReader(Protocol):
    """Resolve one externally supplied URI without exposing repository internals."""

    def resolve(self, uri: str, *, owner_id: str) -> ResourceTarget | None: ...
