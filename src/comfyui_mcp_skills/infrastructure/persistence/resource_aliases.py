"""SQLite reader for canonical Resources and owner-authorized legacy aliases."""

from __future__ import annotations

import sqlite3
from typing import Literal
from urllib.parse import urlsplit

from comfyui_mcp_skills.application.resource_aliases import ResourceTarget
from comfyui_mcp_skills.domain.control_plane import (
    canonical_resource_uri,
    parse_legacy_resource_uri,
    validate_control_plane_id,
)
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore

_CanonicalKind = Literal["workflow", "revision", "deployment", "asset", "job", "artifact"]
_MAX_RESOURCE_URI_LENGTH = 2048
_CANONICAL_COLLECTIONS: dict[str, _CanonicalKind] = {
    "workflows": "workflow",
    "revisions": "revision",
    "deployments": "deployment",
    "assets": "asset",
    "jobs": "job",
    "artifacts": "artifact",
}


class SQLiteLegacyResourceAliasReader:
    """Resolve canonical Resources and legacy aliases against one SQLite fact store."""

    def __init__(self, store: SQLiteControlPlaneStore) -> None:
        self._store = store

    def resolve(self, uri: str, *, owner_id: str) -> ResourceTarget | None:
        if not isinstance(owner_id, str):
            return None
        legacy = parse_legacy_resource_uri(uri)
        if legacy is not None and legacy.kind in {"asset", "job", "output"}:
            return self._resolve_legacy(
                uri,
                legacy.kind,
                owner_id,
                server_id=legacy.server_id,
                prompt_id=legacy.upstream_id if legacy.kind in {"job", "output"} else "",
            )
        canonical = _parse_canonical_uri(uri)
        if canonical is None:
            return None
        kind, object_id = canonical
        if kind == "workflow":
            return self._read_workflow(object_id)
        if kind == "revision":
            return self._read_revision(object_id)
        if kind == "deployment":
            return self._read_deployment(object_id)
        if kind == "asset":
            return self._read_asset(object_id, owner_id)
        if kind == "job":
            return self._read_job(object_id, owner_id)
        return self._read_artifact(object_id, owner_id)

    def _resolve_legacy(
        self,
        uri: str,
        object_kind: str,
        owner_id: str,
        *,
        server_id: str,
        prompt_id: str,
    ) -> ResourceTarget | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT canonical_uri, asset_id, job_id, artifact_id
                FROM legacy_resource_aliases
                WHERE alias_uri = ? AND object_kind = ?
                """,
                (uri, object_kind),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        canonical_uri = str(row[0])
        if object_kind == "asset" and row[1] is not None:
            target = self._read_asset(str(row[1]), owner_id)
        elif object_kind == "job" and row[2] is not None:
            target = self._read_job(
                str(row[2]),
                owner_id,
                server_id=server_id,
                prompt_id=prompt_id,
            )
        elif object_kind == "output" and row[3] is not None:
            target = self._read_artifact(str(row[3]), owner_id)
        else:
            return None
        if target is None or target.canonical_uri != canonical_uri:
            return None
        return target

    def _read_workflow(self, workflow_id: str) -> ResourceTarget | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT workflow_id, created_at
                FROM workflows
                WHERE workflow_id = ?
                """,
                (workflow_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        identifier = str(row[0])
        return ResourceTarget(
            kind="workflow",
            canonical_uri=_canonical_top_level_uri("workflow", identifier),
            object_id=identifier,
            server_id="",
            metadata={
                "workflow_id": identifier,
                "created_at": str(row[1]),
            },
        )

    def _read_revision(self, revision_id: str) -> ResourceTarget | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT revision_id, workflow_id, content_digest, created_at
                FROM workflow_revisions
                WHERE revision_id = ?
                """,
                (revision_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        identifier = str(row[0])
        return ResourceTarget(
            kind="revision",
            canonical_uri=_canonical_top_level_uri("revision", identifier),
            object_id=identifier,
            server_id="",
            metadata={
                "revision_id": identifier,
                "workflow_id": str(row[1]),
                "content_digest": str(row[2]),
                "created_at": str(row[3]),
            },
        )

    def _read_deployment(self, deployment_id: str) -> ResourceTarget | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT d.deployment_id, d.workflow_id, d.revision_id,
                       d.server_id, d.enabled, d.validation_status,
                       d.published, d.created_at, r.content_digest
                FROM workflow_deployments AS d
                JOIN workflow_revisions AS r
                  ON r.workflow_id = d.workflow_id AND r.revision_id = d.revision_id
                WHERE d.deployment_id = ?
                """,
                (deployment_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        identifier = str(row[0])
        server_id = str(row[3])
        return ResourceTarget(
            kind="deployment",
            canonical_uri=_canonical_top_level_uri("deployment", identifier),
            object_id=identifier,
            server_id=server_id,
            metadata={
                "deployment_id": identifier,
                "workflow_id": str(row[1]),
                "revision_id": str(row[2]),
                "server_id": server_id,
                "enabled": bool(row[4]),
                "validation_status": str(row[5]),
                "published": bool(row[6]),
                "created_at": str(row[7]),
                "content_digest": str(row[8]),
            },
        )

    def _read_asset(self, asset_id: str, owner_id: str) -> ResourceTarget | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT asset_id, server_id
                FROM assets
                WHERE asset_id = ? AND owner_id = ?
                """,
                (asset_id, owner_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        identifier = str(row[0])
        return ResourceTarget(
            kind="asset",
            canonical_uri=canonical_resource_uri("asset", identifier),
            object_id=identifier,
            server_id=str(row[1]),
        )

    def _read_job(
        self,
        job_id: str,
        owner_id: str,
        *,
        server_id: str = "",
        prompt_id: str = "",
    ) -> ResourceTarget | None:
        connection = self._connect()
        try:
            predicates = ["jobs.job_id = ?", "jobs.owner_id = ?"]
            parameters: list[str] = [job_id, owner_id]
            if server_id or prompt_id:
                predicates.extend(
                    [
                        "execution_attempts.server_id = ?",
                        "execution_attempts.upstream_prompt_id = ?",
                    ]
                )
                parameters.extend([server_id, prompt_id])
            row = connection.execute(
                f"""
                SELECT jobs.job_id, execution_attempts.server_id,
                       execution_attempts.upstream_prompt_id
                FROM jobs
                JOIN execution_attempts ON execution_attempts.job_id = jobs.job_id
                WHERE {" AND ".join(predicates)}
                  AND execution_attempts.upstream_prompt_id IS NOT NULL
                ORDER BY execution_attempts.attempt DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        identifier = str(row[0])
        return ResourceTarget(
            kind="job",
            canonical_uri=canonical_resource_uri("job", identifier),
            object_id=identifier,
            server_id=str(row[1]),
            prompt_id=str(row[2]),
        )

    def _read_artifact(self, artifact_id: str, owner_id: str) -> ResourceTarget | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT artifacts.artifact_id, artifacts.server_id,
                       artifacts.filename, artifacts.subfolder,
                       artifacts.storage_type
                FROM artifacts
                JOIN jobs ON jobs.job_id = artifacts.job_id
                WHERE artifacts.artifact_id = ? AND jobs.owner_id = ?
                """,
                (artifact_id, owner_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        identifier = str(row[0])
        return ResourceTarget(
            kind="artifact",
            canonical_uri=canonical_resource_uri("artifact", identifier),
            object_id=identifier,
            server_id=str(row[1]),
            filename=str(row[2]),
            subfolder=str(row[3]),
            storage_type=str(row[4]),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._store.path, isolation_level=None, timeout=5.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA trusted_schema = OFF")
        return connection


def _parse_canonical_uri(uri: object) -> tuple[_CanonicalKind, str] | None:
    if (
        not isinstance(uri, str)
        or len(uri) > _MAX_RESOURCE_URI_LENGTH
        or "?" in uri
        or "#" in uri
        or "%" in uri
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in uri)
    ):
        return None
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return None
    if parsed.scheme != "comfyui" or parsed.query or parsed.fragment:
        return None
    kind = _CANONICAL_COLLECTIONS.get(parsed.netloc)
    parts = parsed.path.split("/")[1:]
    if kind is None or len(parts) != 1 or not parts[0]:
        return None
    try:
        identifier = validate_control_plane_id(kind, parts[0])
        canonical = _canonical_top_level_uri(kind, identifier)
    except ValueError:
        return None
    if uri != canonical:
        return None
    return kind, identifier


def _canonical_top_level_uri(kind: _CanonicalKind, identifier: str) -> str:
    if kind in {"revision", "deployment", "workflow"}:
        collection = {
            "workflow": "workflows",
            "revision": "revisions",
            "deployment": "deployments",
        }[kind]
        return f"comfyui://{collection}/{identifier}"
    if kind == "asset":
        return canonical_resource_uri("asset", identifier)
    if kind == "job":
        return canonical_resource_uri("job", identifier)
    return canonical_resource_uri("artifact", identifier)
