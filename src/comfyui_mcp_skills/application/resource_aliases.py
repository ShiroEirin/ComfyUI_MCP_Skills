"""Read-only resolution port for canonical and legacy Resource identities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

ResourceTargetKind = Literal["workflow", "revision", "deployment", "asset", "job", "artifact"]


@dataclass(frozen=True, slots=True)
class ResourceTarget:
    """A resolved canonical target containing only adapter-safe lookup data."""

    kind: ResourceTargetKind
    canonical_uri: str
    object_id: str
    server_id: str
    prompt_id: str = ""
    filename: str = ""
    subfolder: str = ""
    storage_type: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)


class ResourceAliasReader(Protocol):
    """Resolve canonical or legacy identities with owner checks where required."""

    def resolve(self, uri: str, *, owner_id: str) -> ResourceTarget | None: ...
