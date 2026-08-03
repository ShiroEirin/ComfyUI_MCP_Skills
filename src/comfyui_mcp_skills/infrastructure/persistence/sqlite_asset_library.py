"""SQLite facade for Phase L Asset and Artifact lifecycle data."""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import secrets
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, cast

from comfyui_mcp_skills.domain.control_plane import (
    derive_legacy_artifact_id,
    parse_legacy_resource_uri,
)
from comfyui_mcp_skills.domain.errors import (
    ArtifactNotFound,
    ArtifactTransferConflict,
    ArtifactTransferNotFound,
    AssetDeletePlanNotFound,
    AssetLibraryConflict,
    AssetNotFound,
)
from comfyui_mcp_skills.domain.media import validate_media_locator
from comfyui_mcp_skills.domain.models import Artifact, Asset, Job
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.sqlite_runs import (
    serialize_job_outputs,
    terminalize_job_snapshot,
)

_COLLECTION = re.compile(r"[A-Za-z0-9_. -]{1,128}\Z")
_MEDIA = frozenset({"image", "audio", "video"})
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_TRANSFER_LEASE = timedelta(minutes=5)


class SQLiteAssetLibraryRepository:
    def __init__(self, store: SQLiteControlPlaneStore) -> None:
        self._store = store

    def save(self, asset: Asset) -> None:
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            self._insert_asset(c, asset, "runtime_upload")
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def get(self, asset_id: str) -> Asset | None:
        c = self._connect()
        try:
            r = c.execute(
                "SELECT asset_id,server_id,comfyui_ref,name,subfolder,media_type,mime_type,size_bytes,sha256,owner_id,created_at FROM assets WHERE asset_id=? AND deleted_at IS NULL",
                (asset_id,),
            ).fetchone()
        finally:
            c.close()
        return self._asset(r) if r else None

    def get_asset_record(
        self, asset_id: str, owner_id: str, *, include_deleted: bool = False
    ) -> dict[str, Any] | None:
        c = self._connect()
        try:
            return self._asset_record(c, asset_id, owner_id, include_deleted)
        finally:
            c.close()

    def list_asset_records(
        self,
        owner_id: str,
        *,
        limit: int,
        after_created_at: str | None = None,
        after_asset_id: str | None = None,
        media_type: str = "",
        collection: str = "",
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 101 or bool(after_created_at) != bool(after_asset_id):
            raise ValueError("invalid Asset keyset")
        where = ["assets.owner_id=?", "assets.deleted_at IS NULL"]
        vals: list[Any] = [owner_id]
        join = ""
        if media_type:
            if media_type not in _MEDIA:
                raise ValueError("invalid media type")
            where.append("assets.media_type=?")
            vals.append(media_type)
        if collection:
            self._collection(collection)
            join = " JOIN asset_collection_members m ON m.owner_id=assets.owner_id AND m.asset_id=assets.asset_id"
            where.append("m.collection=?")
            vals.append(collection)
        if after_created_at and after_asset_id:
            where.append("(assets.created_at<? OR (assets.created_at=? AND assets.asset_id<?))")
            vals.extend((after_created_at, after_created_at, after_asset_id))
        vals.append(limit)
        c = self._connect()
        try:
            rows = c.execute(
                f"SELECT assets.asset_id,assets.server_id,assets.name,assets.subfolder,assets.media_type,assets.mime_type,assets.size_bytes,assets.sha256,assets.source_type,assets.created_at,assets.expires_at,assets.deleted_at FROM assets{join} WHERE {' AND '.join(where)} ORDER BY assets.created_at DESC,assets.asset_id DESC LIMIT ?",
                vals,
            ).fetchall()
        finally:
            c.close()
        return [self._summary(r) for r in rows]

    def collection_update(
        self, owner_id: str, collection: str, asset_ids: Sequence[str], action: str
    ) -> int:
        self._collection(collection)
        ids = tuple(dict.fromkeys(asset_ids))
        if not ids or action not in {"add", "remove"}:
            raise ValueError("invalid collection update")
        c = self._connect()
        now = _now()
        try:
            c.execute("BEGIN IMMEDIATE")
            marks = ",".join("?" for _ in ids)
            found = {
                str(r[0])
                for r in c.execute(
                    f"SELECT asset_id FROM assets WHERE owner_id=? AND deleted_at IS NULL AND asset_id IN ({marks})",
                    (owner_id, *ids),
                ).fetchall()
            }
            missing = next((x for x in ids if x not in found), None)
            if missing:
                raise AssetNotFound("Asset was not found", details={"asset_id": missing})
            c.execute(
                "INSERT INTO asset_collections(owner_id,collection,created_at,updated_at) VALUES(?,?,?,?) ON CONFLICT(owner_id,collection) DO UPDATE SET updated_at=excluded.updated_at",
                (owner_id, collection, now, now),
            )
            if action == "add":
                c.executemany(
                    "INSERT OR IGNORE INTO asset_collection_members(owner_id,collection,asset_id,created_at) VALUES(?,?,?,?)",
                    ((owner_id, collection, x, now) for x in ids),
                )
            else:
                c.executemany(
                    "DELETE FROM asset_collection_members WHERE owner_id=? AND collection=? AND asset_id=?",
                    ((owner_id, collection, x) for x in ids),
                )
            count = int(
                c.execute(
                    "SELECT count(*) FROM asset_collection_members WHERE owner_id=? AND collection=?",
                    (owner_id, collection),
                ).fetchone()[0]
            )
            c.commit()
            return count
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def metadata_projection(self, asset_id: str, owner_id: str) -> dict[str, Any] | None:
        c = self._connect()
        try:
            r = c.execute(
                "SELECT projection_json FROM asset_metadata_extractions e JOIN assets USING(asset_id) WHERE e.asset_id=? AND e.owner_id=? AND assets.deleted_at IS NULL AND e.source_sha256=assets.sha256",
                (asset_id, owner_id),
            ).fetchone()
        finally:
            c.close()
        return json.loads(str(r[0])) if r else None

    def save_metadata_projection(
        self, asset_id: str, owner_id: str, source_sha256: str, projection: Mapping[str, Any]
    ) -> None:
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute(
                "SELECT 1 FROM assets WHERE asset_id=? AND owner_id=? AND sha256=? AND deleted_at IS NULL",
                (asset_id, owner_id, source_sha256),
            ).fetchone():
                raise AssetNotFound("Asset was not found")
            revision_id = projection.get("revision_id")
            if revision_id is not None and not isinstance(revision_id, str):
                raise ValueError("invalid metadata Revision identity")
            c.execute(
                """INSERT INTO asset_metadata_extractions(
                    asset_id,owner_id,source_sha256,format,projection_json,revision_id,extracted_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(asset_id) DO UPDATE SET
                    source_sha256=excluded.source_sha256,format=excluded.format,
                    projection_json=excluded.projection_json,revision_id=excluded.revision_id,
                    extracted_at=excluded.extracted_at""",
                (
                    asset_id,
                    owner_id,
                    source_sha256,
                    projection["format"],
                    _json(projection),
                    revision_id,
                    _now(),
                ),
            )
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def terminalize(
        self,
        job: Job,
        observations: Sequence[Mapping[str, Any]],
        *,
        failure_injector: Callable[[str], None] | None = None,
    ) -> tuple[Artifact, ...]:
        outputs_json = serialize_job_outputs(observations)
        if outputs_json != serialize_job_outputs(job.outputs):
            raise RuntimeError("completed Job output snapshot does not match observations")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            terminalize_job_snapshot(connection, job, outputs_json)
            items = tuple(self._prepare_artifact(job, item) for item in observations)
            artifact_ids = {artifact.artifact_id for artifact, _legacy in items}
            if len(artifact_ids) != len(items):
                raise AssetLibraryConflict("Duplicate Artifact observation")
            _inject(failure_injector, "after_job")
            for artifact, legacy_uri in items:
                self._persist_terminal_artifact(
                    connection, artifact, legacy_uri, owner_id=job.owner_id
                )
            stored_ids = {
                str(row[0])
                for row in connection.execute(
                    "SELECT artifact_id FROM artifacts WHERE job_id=?", (job.job_id,)
                ).fetchall()
            }
            if stored_ids != artifact_ids:
                raise AssetLibraryConflict("Completed Job Artifact set conflicts with snapshot")
            persisted = tuple(
                self._artifact_owned(connection, artifact.artifact_id, job.owner_id)
                for artifact, _legacy_uri in items
            )
            if any(artifact is None for artifact in persisted):
                raise RuntimeError("completed Job Artifact could not be reloaded")
            _inject(failure_injector, "after_artifacts")
            snapshot_digest = hashlib.sha256(outputs_json.encode("utf-8")).hexdigest()
            updated = connection.execute(
                """UPDATE job_artifact_collections
                   SET status='complete',artifact_count=?,output_snapshot_digest=?,
                       error_code=NULL,updated_at=? WHERE job_id=?""",
                (len(items), snapshot_digest, _now(), job.job_id),
            ).rowcount
            if updated != 1:
                raise RuntimeError("completed Job collection fact was not found")
            self._refresh_artifact_backfill(connection)
            _inject(failure_injector, "after_completeness")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return cast(tuple[Artifact, ...], persisted)

    def record_artifacts(
        self, job: Job, observations: Sequence[Mapping[str, Any]]
    ) -> tuple[Artifact, ...]:
        """Backfill through the same verified atomic terminalization boundary."""
        return self.terminalize(job, observations)

    def _persist_terminal_artifact(
        self,
        connection: sqlite3.Connection,
        artifact: Artifact,
        legacy_uri: str,
        *,
        owner_id: str,
    ) -> None:
        facts = (
            artifact.job_id,
            artifact.server_id,
            artifact.upstream_node_id,
            artifact.output_key,
            artifact.upstream_output_index,
            artifact.filename,
            artifact.subfolder,
            artifact.storage_type,
            artifact.media_type,
            artifact.digest,
            artifact.mime_type,
        )
        old = connection.execute(
            """SELECT job_id,server_id,upstream_node_id,output_key,
                      upstream_output_index,filename,subfolder,storage_type,
                      media_type,digest,mime_type
               FROM artifacts WHERE artifact_id=?""",
            (artifact.artifact_id,),
        ).fetchone()
        if old is None:
            connection.execute(
                """INSERT INTO artifacts(
                       artifact_id,job_id,server_id,upstream_node_id,output_key,
                       upstream_output_index,filename,subfolder,storage_type,
                       media_type,digest,mime_type,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (artifact.artifact_id, *facts, artifact.created_at),
            )
        elif tuple(old) != facts:
            raise AssetLibraryConflict("Artifact identity conflict")
        self._persist_artifact_completeness(connection, artifact, legacy_uri)
        self._persist_artifact_location(connection, artifact, owner_id)
        self._persist_artifact_alias(connection, artifact, legacy_uri)

    @staticmethod
    def _persist_artifact_completeness(
        connection: sqlite3.Connection, artifact: Artifact, legacy_uri: str
    ) -> None:
        legacy_index = _legacy_index(legacy_uri)
        connection.execute(
            """INSERT OR IGNORE INTO artifact_completeness(
                   artifact_id,completeness,mime_type,size_bytes,sha256,legacy_index,observed_at
               ) VALUES(?,'locator_only',?,NULL,NULL,?,?)""",
            (artifact.artifact_id, artifact.mime_type, legacy_index, artifact.created_at),
        )
        row = connection.execute(
            "SELECT mime_type,legacy_index FROM artifact_completeness WHERE artifact_id=?",
            (artifact.artifact_id,),
        ).fetchone()
        if row is None or (str(row[0]), row[1]) != (artifact.mime_type, legacy_index):
            raise AssetLibraryConflict("Artifact completeness identity conflict")

    @staticmethod
    def _persist_artifact_location(
        connection: sqlite3.Connection, artifact: Artifact, owner_id: str
    ) -> None:
        location_id = f"artifact:{artifact.artifact_id}"
        connection.execute(
            """INSERT OR IGNORE INTO media_locations(
                   location_id,owner_id,asset_id,artifact_id,source_job_id,server_id,
                   filename,subfolder,storage_type,state,size_bytes,sha256,mime_type,
                   created_at,updated_at,archived_at,deleted_at
               ) VALUES(?,?,NULL,?,?,?,?,?,?,'available',NULL,NULL,?,?,?,NULL,NULL)""",
            (
                location_id,
                owner_id,
                artifact.artifact_id,
                artifact.job_id,
                artifact.server_id,
                artifact.filename,
                artifact.subfolder,
                artifact.storage_type,
                artifact.mime_type,
                artifact.created_at,
                artifact.created_at,
            ),
        )
        row = connection.execute(
            """SELECT owner_id,artifact_id,source_job_id,server_id,filename,subfolder,
                      storage_type,mime_type FROM media_locations WHERE location_id=?""",
            (location_id,),
        ).fetchone()
        expected = (
            owner_id,
            artifact.artifact_id,
            artifact.job_id,
            artifact.server_id,
            artifact.filename,
            artifact.subfolder,
            artifact.storage_type,
            artifact.mime_type,
        )
        if row is None or tuple(row) != expected:
            raise AssetLibraryConflict("Artifact location identity conflict")

    @staticmethod
    def _persist_artifact_alias(
        connection: sqlite3.Connection, artifact: Artifact, legacy_uri: str
    ) -> None:
        if not legacy_uri:
            return
        alias = connection.execute(
            "SELECT canonical_uri,artifact_id FROM legacy_resource_aliases WHERE alias_uri=?",
            (legacy_uri,),
        ).fetchone()
        if alias is None:
            connection.execute(
                """INSERT INTO legacy_resource_aliases(
                       alias_uri,canonical_uri,object_kind,workflow_id,asset_id,
                       job_id,artifact_id,created_at
                   ) VALUES(?,?,'output',NULL,NULL,NULL,?,?)""",
                (legacy_uri, artifact.resource_uri, artifact.artifact_id, artifact.created_at),
            )
        elif (str(alias[0]), str(alias[1])) != (
            artifact.resource_uri,
            artifact.artifact_id,
        ):
            raise AssetLibraryConflict("Legacy Artifact alias identity conflict")

    @staticmethod
    def _refresh_artifact_backfill(connection: sqlite3.Connection) -> None:
        incomplete = int(
            connection.execute(
                "SELECT count(*) FROM job_artifact_collections WHERE status!='complete'"
            ).fetchone()[0]
        )
        if incomplete == 0:
            connection.execute(
                """UPDATE phase_l_backfill_state
                   SET status='complete',incomplete_count=0,completed_at=?,failure_code=NULL
                   WHERE backfill_name='artifact_outputs' AND status!='failed'""",
                (_now(),),
            )
        else:
            connection.execute(
                """UPDATE phase_l_backfill_state
                   SET status='pending',incomplete_count=?,completed_at=NULL,failure_code=NULL
                   WHERE backfill_name='artifact_outputs' AND status!='failed'""",
                (incomplete,),
            )

    def get_artifact(self, artifact_id: str, owner_id: str) -> Artifact | None:
        c = self._connect()
        try:
            self._require_backfill_complete(c)
            r = c.execute(
                self._artifact_sql()
                + " WHERE artifacts.artifact_id=? AND jobs.owner_id=?"
                + self._artifact_available_sql(),
                (artifact_id, owner_id),
            ).fetchone()
        finally:
            c.close()
        return self._artifact(r) if r else None

    def resolve_artifact_alias(self, uri: str, owner_id: str) -> Artifact | None:
        identity = parse_legacy_resource_uri(uri)
        if identity is None or identity.kind != "output" or identity.index is None:
            return None
        c = self._connect()
        try:
            self._require_backfill_complete(c)
            r = c.execute(
                self._artifact_sql()
                + """ WHERE jobs.owner_id=? AND f.legacy_index=?
                    AND EXISTS (
                        SELECT 1 FROM legacy_resource_aliases AS aliases
                        WHERE aliases.alias_uri=? AND aliases.object_kind='output'
                          AND aliases.artifact_id=artifacts.artifact_id
                          AND aliases.canonical_uri=
                              'comfyui://artifacts/'||artifacts.artifact_id
                    )
                    AND EXISTS (
                        SELECT 1 FROM execution_attempts AS attempts
                        WHERE attempts.job_id=artifacts.job_id
                          AND attempts.server_id=?
                          AND attempts.upstream_prompt_id=?
                    )"""
                + self._artifact_available_sql(),
                (
                    owner_id,
                    identity.index,
                    uri,
                    identity.server_id,
                    identity.upstream_id,
                ),
            ).fetchone()
        finally:
            c.close()
        return self._artifact(r) if r else None

    def verify_artifact_facts(
        self,
        artifact_id: str,
        owner_id: str,
        *,
        size_bytes: int,
        sha256: str,
        mime_type: str,
        observed_at: datetime,
    ) -> Artifact:
        if size_bytes < 0 or not _SHA.fullmatch(sha256) or not mime_type:
            raise ValueError("invalid verified Artifact facts")
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            self._require_backfill_complete(c)
            artifact = self._artifact_owned(c, artifact_id, owner_id)
            if artifact is None:
                raise ArtifactNotFound("Artifact was not found")
            existing = c.execute(
                "SELECT completeness,size_bytes,sha256,mime_type FROM artifact_completeness WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            facts = (size_bytes, sha256, mime_type)
            if existing is not None and str(existing[0]) == "verified":
                if (int(existing[1]), str(existing[2]), str(existing[3])) != facts:
                    raise ArtifactTransferConflict(
                        "Artifact content changed", details={"reason": "content_changed"}
                    )
            else:
                c.execute(
                    """INSERT INTO artifact_completeness(
                        artifact_id,completeness,mime_type,size_bytes,sha256,observed_at
                    ) VALUES(?,'verified',?,?,?,?)
                    ON CONFLICT(artifact_id) DO UPDATE SET
                        completeness='verified',mime_type=excluded.mime_type,
                        size_bytes=excluded.size_bytes,sha256=excluded.sha256,
                        observed_at=excluded.observed_at""",
                    (artifact_id, mime_type, size_bytes, sha256, observed_at.isoformat()),
                )
                c.execute(
                    """UPDATE media_locations SET size_bytes=?,sha256=?,mime_type=?,updated_at=?
                    WHERE artifact_id=? AND owner_id=? AND state='available'""",
                    (size_bytes, sha256, mime_type, observed_at.isoformat(), artifact_id, owner_id),
                )
            row = c.execute(
                self._artifact_sql() + " WHERE artifacts.artifact_id=? AND jobs.owner_id=?",
                (artifact_id, owner_id),
            ).fetchone()
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()
        if row is None:
            raise ArtifactNotFound("Artifact was not found")
        return self._artifact(row)

    def published_consumer_class(
        self, workflow_id: str, server_id: str, parameter_name: str
    ) -> str | None:
        binding = self.published_parameter_binding(workflow_id, server_id, parameter_name)
        return str(binding["consumer_class"]) if binding is not None else None

    def published_parameter_binding(
        self, workflow_id: str, server_id: str, parameter_name: str
    ) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT r.graph_json,r.parameter_schema_json,r.revision_id,
                          r.content_digest,d.deployment_id
                   FROM workflow_deployments AS d
                   JOIN workflow_revisions AS r
                     ON r.workflow_id=d.workflow_id AND r.revision_id=d.revision_id
                   WHERE d.workflow_id=? AND d.server_id=? AND d.published=1
                     AND d.enabled=1 AND d.validation_status='valid'""",
                (workflow_id, server_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        try:
            graph = json.loads(str(row[0]))
            schema = json.loads(str(row[1]))
            metadata = schema["parameters"][parameter_name]
            node = graph[str(metadata["node_id"])]
            inputs = node["inputs"]
            field = str(metadata["field"])
            consumer_class = str(node["class_type"])
            media_type = str(metadata["type"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            not consumer_class
            or not media_type
            or not isinstance(inputs, dict)
            or field not in inputs
        ):
            return None
        storage_type = str(
            metadata.get("storage_type", "output" if consumer_class.endswith("Output") else "input")
        )
        return {
            "consumer_class": consumer_class,
            "parameter_media_type": media_type,
            "parameter_field": field,
            "parameter_storage_type": storage_type,
            "revision_id": str(row[2]),
            "revision_content_digest": str(row[3]),
            "deployment_id": str(row[4]),
        }

    def match_revision_graph(self, graph: dict[str, Any]) -> dict[str, Any] | None:
        canonical = _json(graph)
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT workflow_id,revision_id,content_digest,graph_json FROM workflow_revisions ORDER BY revision_id"
            ).fetchall()
        finally:
            connection.close()
        matches = [row for row in rows if _canonical_stored_json(str(row[3])) == canonical]
        if len(matches) != 1:
            return None
        row = matches[0]
        return {
            "workflow_id": str(row[0]),
            "revision_id": str(row[1]),
            "content_digest": str(row[2]),
        }

    def list_artifacts(
        self,
        owner_id: str,
        *,
        limit: int = 100,
        after_created_at: str | None = None,
        after_artifact_id: str | None = None,
    ) -> tuple[Artifact, ...]:
        if not 1 <= limit <= 100 or bool(after_created_at) != bool(after_artifact_id):
            raise ValueError("invalid Artifact keyset")
        w = " WHERE jobs.owner_id=?" + self._artifact_available_sql()
        v: list[Any] = [owner_id]
        if after_created_at and after_artifact_id:
            w += " AND (artifacts.created_at<? OR (artifacts.created_at=? AND artifacts.artifact_id<?))"
            v.extend((after_created_at, after_created_at, after_artifact_id))
        v.append(limit)
        c = self._connect()
        try:
            self._require_backfill_complete(c)
            rows = c.execute(
                self._artifact_sql()
                + w
                + " ORDER BY artifacts.created_at DESC,artifacts.artifact_id DESC LIMIT ?",
                v,
            ).fetchall()
        finally:
            c.close()
        return tuple(self._artifact(r) for r in rows)

    def list_artifacts_for_job(self, job_id: str, owner_id: str) -> tuple[Artifact, ...]:
        c = self._connect()
        try:
            self._require_backfill_complete(c)
            rows = c.execute(
                self._artifact_sql()
                + " WHERE artifacts.job_id=? AND jobs.owner_id=?"
                + self._artifact_available_sql()
                + " ORDER BY COALESCE(f.legacy_index,2147483647),artifacts.upstream_node_id,artifacts.output_key,artifacts.upstream_output_index",
                (job_id, owner_id),
            ).fetchall()
        finally:
            c.close()
        return tuple(self._artifact(r) for r in rows)

    def artifact_lineage(self, artifact_id: str, owner_id: str) -> dict[str, Any] | None:
        c = self._connect()
        try:
            c.execute("BEGIN")
            self._require_backfill_complete(c)
            artifact_row = c.execute(
                self._artifact_sql() + " WHERE artifacts.artifact_id=? AND jobs.owner_id=?",
                (artifact_id, owner_id),
            ).fetchone()
            if artifact_row is None:
                c.commit()
                return None
            a = self._artifact(artifact_row)
            job = c.execute(
                """SELECT jobs.job_id,jobs.workflow_id,jobs.plan_id,jobs.revision_id,
                          jobs.deployment_id,collections.status,plans.plan_digest,
                          revisions.content_digest
                   FROM jobs
                   LEFT JOIN job_artifact_collections AS collections USING(job_id)
                   LEFT JOIN execution_plans AS plans ON plans.plan_id=jobs.plan_id
                   LEFT JOIN workflow_revisions AS revisions ON revisions.revision_id=jobs.revision_id
                   WHERE jobs.job_id=? AND jobs.owner_id=?""",
                (a.job_id, owner_id),
            ).fetchone()
            derived = c.execute(
                "SELECT asset_id,relationship,created_at FROM asset_artifact_lineage WHERE owner_id=? AND source_artifact_id=? ORDER BY created_at,asset_id",
                (owner_id, artifact_id),
            ).fetchall()
            inputs = []
            if job is not None and job[2] is not None:
                inputs = c.execute(
                    """SELECT parameter_name,consumer_node_id,consumer_input_name,
                              consumer_class,source_kind,asset_id,artifact_id,
                              reuse_strategy,source_digest
                       FROM execution_plan_inputs
                       WHERE plan_id=? AND owner_id=? ORDER BY parameter_name""",
                    (str(job[2]), owner_id),
                ).fetchall()
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()
        out = a.to_public_dict()
        if job is not None:
            out["chain"] = {
                "revision": {
                    "revision_id": str(job[3]) if job[3] is not None else None,
                    "content_digest": str(job[7]) if job[7] is not None else None,
                },
                "plan": {
                    "plan_id": str(job[2]) if job[2] is not None else None,
                    "plan_digest": str(job[6]) if job[6] is not None else None,
                    "deployment_id": str(job[4]) if job[4] is not None else None,
                    "inputs": [
                        {
                            "parameter_name": str(row[0]),
                            "consumer_node_id": str(row[1]),
                            "consumer_input_name": str(row[2]),
                            "consumer_class": str(row[3]),
                            "source_kind": str(row[4]),
                            "asset_id": str(row[5]) if row[5] is not None else None,
                            "artifact_id": str(row[6]) if row[6] is not None else None,
                            "reuse_strategy": str(row[7]),
                            "source_digest": str(row[8]),
                        }
                        for row in inputs
                    ],
                },
                "job": {
                    "job_id": str(job[0]),
                    "workflow_id": str(job[1]),
                    "artifact_collection_status": str(job[5])
                    if job[5] is not None
                    else "incomplete",
                },
            }
        out["derived_assets"] = [
            {
                "asset_id": str(row[0]),
                "resource_uri": f"comfyui://assets/{row[0]}",
                "relationship": str(row[1]),
                "created_at": str(row[2]),
            }
            for row in derived
        ]
        return out

    def delete_snapshot(self, asset_id: str, owner_id: str) -> dict[str, Any] | None:
        c = self._connect()
        try:
            c.execute("BEGIN")
            self._require_backfill_complete(c)
            snapshot = self._snapshot(c, asset_id, owner_id)
            c.commit()
            return snapshot
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def save_delete_plan(self, p: Mapping[str, Any]) -> None:
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            self._require_backfill_complete(c)
            c.execute(
                "INSERT INTO asset_delete_plans(plan_id,owner_id,asset_id,plan_digest,asset_identity_digest,impact_digest,impact_json,created_at,expires_at,committed_at) VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                (
                    p["plan_id"],
                    p["owner_id"],
                    p["asset_id"],
                    p["plan_digest"],
                    p["asset_identity_digest"],
                    p["impact_digest"],
                    _json(p["impact"]),
                    p["created_at"],
                    p["expires_at"],
                ),
            )
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def commit_delete_plan(
        self, plan_id: str, plan_digest: str, owner_id: str, *, now: datetime
    ) -> dict[str, Any]:
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            self._require_backfill_complete(c)
            r = c.execute(
                "SELECT asset_id,plan_digest,asset_identity_digest,impact_digest,expires_at,committed_at FROM asset_delete_plans WHERE plan_id=? AND owner_id=?",
                (plan_id, owner_id),
            ).fetchone()
            if not r:
                raise AssetDeletePlanNotFound("Asset delete plan was not found")
            aid = str(r[0])
            if str(r[1]) != plan_digest:
                raise AssetLibraryConflict(
                    "Delete digest mismatch", details={"reason": "digest_mismatch"}
                )
            if r[5] is not None:
                c.commit()
                return {
                    "asset_id": aid,
                    "resource_uri": f"comfyui://assets/{aid}",
                    "deleted": True,
                    "deleted_at": str(r[5]),
                }
            if _date(str(r[4])) <= now:
                raise AssetLibraryConflict("Delete plan expired", details={"reason": "expired"})
            snap = self._snapshot(c, aid, owner_id)
            if not snap:
                raise AssetNotFound("Asset was not found")
            if snap["asset_identity_digest"] != str(r[2]):
                raise AssetLibraryConflict(
                    "Asset identity changed", details={"reason": "identity_changed"}
                )
            if snap["impact_digest"] != str(r[3]):
                raise AssetLibraryConflict(
                    "Asset impact changed", details={"reason": "impact_changed"}
                )
            if any(
                bool(hold["legal_hold"])
                or (hold["retain_until"] is not None and _date(str(hold["retain_until"])) > now)
                for hold in snap["impact"]["holds"]
            ):
                raise AssetLibraryConflict(
                    "Asset is retained", details={"reason": "retention_hold"}
                )
            stamp = now.isoformat()
            c.execute(
                "UPDATE assets SET deleted_at=? WHERE asset_id=? AND owner_id=? AND deleted_at IS NULL",
                (stamp, aid, owner_id),
            )
            c.execute(
                """UPDATE media_locations
                   SET state='deleted',updated_at=?,deleted_at=?,
                       archived_at=CASE WHEN state='archived' THEN archived_at ELSE NULL END
                   WHERE owner_id=? AND asset_id=? AND state!='deleted'""",
                (stamp, stamp, owner_id, aid),
            )
            c.execute(
                "UPDATE asset_delete_plans SET committed_at=? WHERE plan_id=?", (stamp, plan_id)
            )
            c.commit()
            return {
                "asset_id": aid,
                "resource_uri": f"comfyui://assets/{aid}",
                "deleted": True,
                "deleted_at": stamp,
            }
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def save_transfer_plan(self, p: Mapping[str, Any]) -> dict[str, Any]:
        transfer_id = str(p["transfer_id"])
        owner_id = str(p["owner_id"])
        artifact_id = str(p["artifact_id"])
        target_asset_id = _target_asset_id(str(p["plan_digest"]))
        network_policy = p.get("network_policy", {})
        if not isinstance(network_policy, Mapping):
            raise ValueError("invalid transfer network policy")
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            self._require_backfill_complete(c)
            artifact = self._artifact_owned(c, artifact_id, owner_id)
            if artifact is None:
                raise ArtifactNotFound("Artifact was not found")
            c.execute(
                """INSERT INTO artifact_transfers(
                    transfer_id,owner_id,artifact_id,source_job_id,target_server_id,
                    target_asset_id,operation,strategy,state,plan_digest,
                    artifact_identity_digest,planned_size_bytes,planned_sha256,
                    planned_mime_type,network_policy_json,temporary_policy,
                    created_at,expires_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,'planned',?,?,?,?,?,?,?,?,?,?)""",
                (
                    transfer_id,
                    owner_id,
                    artifact_id,
                    artifact.job_id,
                    p["target_server_id"],
                    target_asset_id,
                    p["operation"],
                    p["strategy"],
                    p["plan_digest"],
                    p["artifact_identity_digest"],
                    p["planned_size_bytes"],
                    p["planned_sha256"],
                    p["planned_mime_type"],
                    _json(network_policy),
                    p["temporary_policy"],
                    p["created_at"],
                    p["expires_at"],
                    p["created_at"],
                ),
            )
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()
        return self.get_transfer(transfer_id, owner_id) or {}

    def get_transfer(self, transfer_id: str, owner_id: str) -> dict[str, Any] | None:
        c = self._connect()
        try:
            self._require_backfill_complete(c)
            r = c.execute(
                self._transfer_sql() + " WHERE transfer_id=? AND owner_id=?",
                (transfer_id, owner_id),
            ).fetchone()
        finally:
            c.close()
        return self._transfer(r) if r else None

    def claim_transfer(
        self, transfer_id: str, plan_digest: str, owner_id: str, *, now: datetime
    ) -> dict[str, Any]:
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            self._require_backfill_complete(c)
            r = c.execute(
                """SELECT artifact_id,source_job_id,target_server_id,target_asset_id,
                          operation,strategy,state,plan_digest,artifact_identity_digest,
                          planned_size_bytes,planned_sha256,planned_mime_type,
                          network_policy_json,temporary_policy,expires_at,
                          lease_expires_at,lease_fence
                   FROM artifact_transfers WHERE transfer_id=? AND owner_id=?""",
                (transfer_id, owner_id),
            ).fetchone()
            if r is None:
                raise ArtifactTransferNotFound("Artifact transfer was not found")
            if str(r[7]) != plan_digest:
                raise ArtifactTransferConflict(
                    "Transfer digest mismatch", details={"reason": "digest_mismatch"}
                )
            state = str(r[6])
            if state == "completed":
                c.commit()
                return {"completed": True}
            if _date(str(r[14])) <= now:
                raise ArtifactTransferConflict("Transfer expired", details={"reason": "expired"})
            if state == "transferring" and r[15] is not None and _date(str(r[15])) > now:
                raise ArtifactTransferConflict(
                    "Transfer in progress", details={"reason": "in_progress"}
                )
            artifact = self._artifact_owned(c, str(r[0]), owner_id)
            if artifact is None or artifact.job_id != str(r[1]):
                raise ArtifactNotFound("Artifact was not found")
            if self.artifact_identity_digest(artifact) != str(r[8]):
                raise ArtifactTransferConflict(
                    "Artifact identity changed", details={"reason": "identity_changed"}
                )
            token = secrets.token_urlsafe(32)
            fence = int(r[16]) + 1
            lease_expires_at = min(_date(str(r[14])), now + _TRANSFER_LEASE).isoformat()
            c.execute(
                """UPDATE artifact_transfers SET state='transferring',failure_code=NULL,
                          updated_at=?,lease_token=?,lease_expires_at=?,lease_fence=?
                   WHERE transfer_id=? AND owner_id=?""",
                (now.isoformat(), token, lease_expires_at, fence, transfer_id, owner_id),
            )
            c.commit()
            return {
                "artifact": artifact,
                "target_server_id": str(r[2]),
                "target_asset_id": str(r[3]),
                "operation": str(r[4]),
                "strategy": str(r[5]),
                "planned_size_bytes": int(r[9]),
                "planned_sha256": str(r[10]),
                "planned_mime_type": str(r[11]),
                "network_policy": json.loads(str(r[12])),
                "temporary_policy": str(r[13]),
                "lease_token": token,
                "lease_fence": fence,
                "lease_expires_at": lease_expires_at,
            }
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def complete_direct_transfer(
        self,
        transfer_id: str,
        owner_id: str,
        *,
        lease_token: str,
        lease_fence: int,
        now: datetime,
    ) -> None:
        del transfer_id, owner_id, lease_token, lease_fence, now
        raise ArtifactTransferConflict(
            "Direct reuse does not materialize a transfer", details={"reason": "direct_forbidden"}
        )

    def complete_uploaded_transfer(
        self,
        transfer_id: str,
        owner_id: str,
        asset: Asset,
        *,
        relationship: str,
        size_bytes: int,
        sha256: str,
        mime_type: str,
        lease_token: str,
        lease_fence: int,
        now: datetime,
    ) -> None:
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            self._require_backfill_complete(c)
            r = c.execute(
                """SELECT artifact_id,source_job_id,operation,strategy,target_server_id,
                          target_asset_id,planned_size_bytes,planned_sha256,planned_mime_type
                   FROM artifact_transfers
                   WHERE transfer_id=? AND owner_id=? AND state='transferring'
                     AND lease_token=? AND lease_fence=? AND lease_expires_at>?""",
                (transfer_id, owner_id, lease_token, lease_fence, now.isoformat()),
            ).fetchone()
            if r is None or str(r[2]) != relationship:
                raise ArtifactTransferConflict(
                    "Transfer lease changed", details={"reason": "stale_lease"}
                )
            planned = (int(r[6]), str(r[7]), str(r[8]))
            if (size_bytes, sha256, mime_type) != planned:
                raise ArtifactTransferConflict(
                    "Transfer bytes changed", details={"reason": "content_changed"}
                )
            artifact = self._artifact_owned(c, str(r[0]), owner_id)
            if artifact is None or artifact.job_id != str(r[1]):
                raise ArtifactNotFound("Artifact was not found")
            verified = c.execute(
                "SELECT completeness,size_bytes,sha256,mime_type FROM artifact_completeness WHERE artifact_id=?",
                (artifact.artifact_id,),
            ).fetchone()
            if verified is None or (
                str(verified[0]),
                int(verified[1]),
                str(verified[2]),
                str(verified[3]),
            ) != ("verified", size_bytes, sha256, mime_type):
                raise ArtifactTransferConflict(
                    "Artifact verification changed", details={"reason": "content_changed"}
                )
            target_asset = replace(
                asset,
                asset_id=str(r[5]),
                owner_id=owner_id,
                server_id=str(r[4]),
                size_bytes=size_bytes,
                sha256=sha256,
                mime_type=mime_type,
            )
            self._insert_asset(
                c,
                target_asset,
                "artifact_import" if relationship == "import" else "artifact_transfer",
            )
            c.execute(
                """INSERT INTO asset_artifact_lineage(
                    asset_id,owner_id,source_artifact_id,source_job_id,relationship,created_at
                ) VALUES(?,?,?,?,?,?)""",
                (
                    target_asset.asset_id,
                    owner_id,
                    artifact.artifact_id,
                    artifact.job_id,
                    relationship,
                    now.isoformat(),
                ),
            )
            changed = c.execute(
                """UPDATE artifact_transfers SET state='completed',completed_at=?,updated_at=?,
                          result_asset_id=?,result_size_bytes=?,result_sha256=?
                   WHERE transfer_id=? AND owner_id=? AND state='transferring'
                     AND lease_token=? AND lease_fence=? AND lease_expires_at>?""",
                (
                    now.isoformat(),
                    now.isoformat(),
                    target_asset.asset_id,
                    size_bytes,
                    sha256,
                    transfer_id,
                    owner_id,
                    lease_token,
                    lease_fence,
                    now.isoformat(),
                ),
            ).rowcount
            if changed != 1:
                raise ArtifactTransferConflict(
                    "Transfer lease changed", details={"reason": "stale_lease"}
                )
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def fail_transfer(
        self,
        transfer_id: str,
        owner_id: str,
        failure_code: str,
        *,
        lease_token: str,
        lease_fence: int,
    ) -> None:
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            self._require_backfill_complete(c)
            changed = c.execute(
                """UPDATE artifact_transfers SET state='failed',failure_code=?,updated_at=?
                   WHERE transfer_id=? AND owner_id=? AND state='transferring'
                     AND lease_token=? AND lease_fence=?""",
                (
                    failure_code[:64],
                    _now(),
                    transfer_id,
                    owner_id,
                    lease_token,
                    lease_fence,
                ),
            ).rowcount
            if changed != 1:
                raise ArtifactTransferConflict(
                    "Transfer lease changed", details={"reason": "stale_lease"}
                )
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def apply_retention(self, *, now: datetime) -> dict[str, int]:
        """Archive or dispose due locations only after a complete reference snapshot."""
        stamp = now.isoformat()
        archived = 0
        deleted = 0
        tombstoned = 0
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            self._require_backfill_complete(c)
            bindings = c.execute(
                """SELECT binding_id,owner_id,asset_id,artifact_id,source_job_id,
                          archive_at,delete_at,retain_until,legal_hold
                   FROM media_retention_bindings
                   WHERE legal_hold=0 AND (
                       (archive_at IS NOT NULL AND archive_at<=?) OR
                       (delete_at IS NOT NULL AND delete_at<=?)
                   ) ORDER BY binding_id""",
                (stamp, stamp),
            ).fetchall()
            for binding in bindings:
                retain_until = _optional_date(binding[7])
                if retain_until is not None and retain_until > now:
                    continue
                archive_at = _optional_date(binding[5])
                delete_at = _optional_date(binding[6])
                delete_due = delete_at is not None and delete_at <= now
                archive_due = archive_at is not None and archive_at <= now
                if not delete_due and not archive_due:
                    continue
                owner_id = str(binding[1])
                if binding[2] is not None:
                    asset_id = str(binding[2])
                    snapshot = self._snapshot(c, asset_id, owner_id)
                    if snapshot is None:
                        continue
                    impact = snapshot["impact"]
                    if impact["plan_references"] or any(
                        transfer["state"] in {"planned", "transferring"}
                        for transfer in impact["transfers"]
                    ):
                        continue
                    if delete_due:
                        deleted += c.execute(
                            """UPDATE media_locations SET state='deleted',updated_at=?,deleted_at=?
                               WHERE owner_id=? AND asset_id=? AND state!='deleted'""",
                            (stamp, stamp, owner_id, asset_id),
                        ).rowcount
                        tombstoned += c.execute(
                            "UPDATE assets SET deleted_at=? WHERE owner_id=? AND asset_id=? AND deleted_at IS NULL",
                            (stamp, owner_id, asset_id),
                        ).rowcount
                    else:
                        archived += c.execute(
                            """UPDATE media_locations SET state='archived',updated_at=?,archived_at=?
                               WHERE owner_id=? AND asset_id=? AND state='available'""",
                            (stamp, stamp, owner_id, asset_id),
                        ).rowcount
                    continue
                artifact_id = str(binding[3])
                source_job_id = str(binding[4])
                collection = c.execute(
                    """SELECT collections.status
                       FROM artifacts
                       JOIN jobs ON jobs.job_id=artifacts.job_id
                       LEFT JOIN job_artifact_collections AS collections
                         ON collections.job_id=artifacts.job_id
                       WHERE artifacts.artifact_id=? AND artifacts.job_id=?
                         AND jobs.owner_id=?""",
                    (artifact_id, source_job_id, owner_id),
                ).fetchone()
                if collection is None or str(collection[0]) != "complete":
                    raise AssetLibraryConflict(
                        "Artifact evidence is incomplete", details={"reason": "backfill_pending"}
                    )
                referenced = bool(
                    c.execute(
                        "SELECT 1 FROM execution_plan_inputs WHERE owner_id=? AND artifact_id=? LIMIT 1",
                        (owner_id, artifact_id),
                    ).fetchone()
                    or c.execute(
                        """SELECT 1 FROM asset_artifact_lineage AS lineage
                           JOIN assets ON assets.asset_id=lineage.asset_id
                           WHERE lineage.owner_id=? AND lineage.source_artifact_id=?
                             AND assets.deleted_at IS NULL LIMIT 1""",
                        (owner_id, artifact_id),
                    ).fetchone()
                    or c.execute(
                        """SELECT 1 FROM artifact_transfers
                           WHERE owner_id=? AND artifact_id=?
                             AND state IN ('planned','transferring') LIMIT 1""",
                        (owner_id, artifact_id),
                    ).fetchone()
                )
                if referenced:
                    continue
                if delete_due:
                    deleted += c.execute(
                        """UPDATE media_locations SET state='deleted',updated_at=?,deleted_at=?
                           WHERE owner_id=? AND artifact_id=? AND state!='deleted'""",
                        (stamp, stamp, owner_id, artifact_id),
                    ).rowcount
                else:
                    archived += c.execute(
                        """UPDATE media_locations SET state='archived',updated_at=?,archived_at=?
                           WHERE owner_id=? AND artifact_id=? AND state='available'""",
                        (stamp, stamp, owner_id, artifact_id),
                    ).rowcount
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()
        return {
            "locations_archived": archived,
            "locations_deleted": deleted,
            "assets_tombstoned": tombstoned,
        }

    @staticmethod
    def artifact_identity_digest(a: Artifact) -> str:
        return _digest(
            {
                "artifact_id": a.artifact_id,
                "job_id": a.job_id,
                "server_id": a.server_id,
                "node": a.upstream_node_id,
                "key": a.output_key,
                "index": a.upstream_output_index,
                "filename": a.filename,
                "subfolder": a.subfolder,
                "storage": a.storage_type,
                "media": a.media_type,
                "digest": a.digest,
            }
        )

    def _snapshot(self, c: sqlite3.Connection, aid: str, owner: str) -> dict[str, Any] | None:
        asset = self._asset_record(c, aid, owner, False)
        if asset is None:
            return None
        collections = [
            str(row[0])
            for row in c.execute(
                "SELECT collection FROM asset_collection_members WHERE owner_id=? AND asset_id=? ORDER BY collection",
                (owner, aid),
            ).fetchall()
        ]
        lineage_row = c.execute(
            """SELECT source_artifact_id,source_job_id,relationship
               FROM asset_artifact_lineage WHERE owner_id=? AND asset_id=?""",
            (owner, aid),
        ).fetchone()
        plan_rows = c.execute(
            """SELECT inputs.plan_id,inputs.parameter_name,inputs.consumer_node_id,
                      inputs.consumer_input_name,inputs.consumer_class,inputs.reuse_strategy,
                      plans.workflow_id,inputs.revision_id,inputs.deployment_id,
                      jobs.job_id,collections.status
               FROM execution_plan_inputs AS inputs
               JOIN execution_plans AS plans ON plans.plan_id=inputs.plan_id
               LEFT JOIN jobs ON jobs.plan_id=inputs.plan_id AND jobs.owner_id=inputs.owner_id
               LEFT JOIN job_artifact_collections AS collections ON collections.job_id=jobs.job_id
               WHERE inputs.owner_id=? AND inputs.asset_id=?
               ORDER BY inputs.plan_id,inputs.parameter_name,jobs.job_id""",
            (owner, aid),
        ).fetchall()
        transfers = [
            {"transfer_id": str(row[0]), "state": str(row[1])}
            for row in c.execute(
                """SELECT transfer_id,state FROM artifact_transfers
                   WHERE owner_id=? AND (result_asset_id=? OR target_asset_id=?)
                   ORDER BY transfer_id""",
                (owner, aid, aid),
            ).fetchall()
        ]
        locations = [
            {
                "location_id": str(row[0]),
                "server_id": str(row[1]),
                "storage_type": str(row[2]),
                "state": str(row[3]),
                "size_bytes": int(row[4]) if row[4] is not None else None,
                "sha256": str(row[5]) if row[5] is not None else None,
                "mime_type": str(row[6]),
                "archived_at": str(row[7]) if row[7] is not None else None,
                "deleted_at": str(row[8]) if row[8] is not None else None,
            }
            for row in c.execute(
                """SELECT location_id,server_id,storage_type,state,size_bytes,sha256,
                          mime_type,archived_at,deleted_at
                   FROM media_locations WHERE owner_id=? AND asset_id=? ORDER BY location_id""",
                (owner, aid),
            ).fetchall()
        ]
        holds = [
            {
                "binding_id": str(row[0]),
                "legal_hold": bool(row[1]),
                "retain_until": str(row[2]) if row[2] is not None else None,
                "archive_at": str(row[3]) if row[3] is not None else None,
                "delete_at": str(row[4]) if row[4] is not None else None,
            }
            for row in c.execute(
                """SELECT binding_id,legal_hold,retain_until,archive_at,delete_at
                   FROM media_retention_bindings WHERE owner_id=? AND asset_id=?
                   ORDER BY binding_id""",
                (owner, aid),
            ).fetchall()
        ]
        impact = {
            "collections": collections,
            "lineage": (
                {
                    "source_artifact_id": str(lineage_row[0]),
                    "source_job_id": str(lineage_row[1]),
                    "relationship": str(lineage_row[2]),
                }
                if lineage_row is not None
                else None
            ),
            "plan_references": [
                {
                    "plan_id": str(row[0]),
                    "parameter_name": str(row[1]),
                    "consumer_node_id": str(row[2]),
                    "consumer_input_name": str(row[3]),
                    "consumer_class": str(row[4]),
                    "reuse_strategy": str(row[5]),
                    "workflow_id": str(row[6]),
                    "revision_id": str(row[7]),
                    "deployment_id": str(row[8]),
                    "job_id": str(row[9]) if row[9] is not None else None,
                    "artifact_collection_status": str(row[10]) if row[10] is not None else None,
                }
                for row in plan_rows
            ],
            "transfers": transfers,
            "locations": locations,
            "holds": holds,
            "metadata_present": bool(
                c.execute(
                    "SELECT 1 FROM asset_metadata_extractions WHERE asset_id=? AND owner_id=?",
                    (aid, owner),
                ).fetchone()
            ),
            "legacy_alias_count": int(
                c.execute(
                    "SELECT count(*) FROM legacy_resource_aliases WHERE asset_id=?", (aid,)
                ).fetchone()[0]
            ),
        }
        identity = {
            key: asset[key]
            for key in (
                "asset_id",
                "server_id",
                "media_type",
                "mime_type",
                "size_bytes",
                "sha256",
                "source_type",
                "created_at",
                "expires_at",
            )
        }
        return {
            "asset": asset,
            "asset_identity_digest": _digest(identity),
            "impact": impact,
            "impact_digest": _digest(impact),
        }

    def _asset_record(
        self, c: sqlite3.Connection, aid: str, owner: str, deleted: bool
    ) -> dict[str, Any] | None:
        r = c.execute(
            "SELECT asset_id,server_id,name,subfolder,media_type,mime_type,size_bytes,sha256,source_type,created_at,expires_at,deleted_at FROM assets WHERE asset_id=? AND owner_id=?"
            + ("" if deleted else " AND deleted_at IS NULL"),
            (aid, owner),
        ).fetchone()
        if not r:
            return None
        out = self._summary(r)
        out["collections"] = [
            str(x[0])
            for x in c.execute(
                "SELECT collection FROM asset_collection_members WHERE owner_id=? AND asset_id=? ORDER BY collection",
                (owner, aid),
            ).fetchall()
        ]
        out["locations"] = [
            {
                "location_id": str(row[0]),
                "server_id": str(row[1]),
                "storage_type": str(row[2]),
                "state": str(row[3]),
                "size_bytes": int(row[4]) if row[4] is not None else None,
                "sha256": str(row[5]) if row[5] is not None else None,
                "mime_type": str(row[6]),
            }
            for row in c.execute(
                """SELECT location_id,server_id,storage_type,state,size_bytes,sha256,mime_type
                   FROM media_locations WHERE owner_id=? AND asset_id=? ORDER BY location_id""",
                (owner, aid),
            ).fetchall()
        ]
        lin = c.execute(
            "SELECT source_artifact_id,relationship,created_at FROM asset_artifact_lineage WHERE owner_id=? AND asset_id=?",
            (owner, aid),
        ).fetchone()
        if lin:
            out["lineage"] = {
                "source_artifact_id": str(lin[0]),
                "source_resource_uri": f"comfyui://artifacts/{lin[0]}",
                "lineage_uri": f"comfyui://lineage/{lin[0]}",
                "relationship": str(lin[1]),
                "created_at": str(lin[2]),
            }
        return out

    def _prepare_artifact(self, job: Job, x: Mapping[str, Any]) -> tuple[Artifact, str]:
        node, key = str(x.get("upstream_node_id", "")), str(x.get("output_key", ""))
        idx = x.get("upstream_output_index", x.get("output_index", -1))
        name, sub = validate_media_locator(x.get("filename"), x.get("subfolder", ""))
        storage, media = (
            str(x.get("storage_type", x.get("type", "output"))),
            str(x.get("media_type", "")),
        )
        if isinstance(idx, bool) or not isinstance(idx, int) or media not in _MEDIA:
            raise ValueError("invalid Artifact observation")
        if "output_index" in x and x.get("output_index") != idx:
            raise AssetLibraryConflict("Artifact output index conflict")
        if "type" in x and str(x.get("type")) != storage:
            raise AssetLibraryConflict("Artifact storage type conflict")
        aid = derive_legacy_artifact_id(job.job_id, node, key, idx, name, sub, storage)
        claimed_id = x.get("artifact_id")
        if claimed_id not in (None, "", aid):
            raise AssetLibraryConflict("Artifact observation identity conflict")
        claimed_uri = x.get("resource_uri")
        if claimed_uri not in (None, "", f"comfyui://artifacts/{aid}"):
            raise AssetLibraryConflict("Artifact observation URI conflict")
        legacy_value = x.get("legacy_uri", "")
        if not isinstance(legacy_value, str):
            raise ValueError("invalid Artifact legacy alias")
        if legacy_value:
            legacy_index = _legacy_index(legacy_value)
            expected_legacy = f"comfyui://outputs/{job.server_id}/{job.prompt_id}/{legacy_index}"
            if legacy_index is None or legacy_value != expected_legacy:
                raise AssetLibraryConflict("Artifact observation alias conflict")
        if "legacy_index" in x:
            claimed_legacy_index = x.get("legacy_index")
            if (
                isinstance(claimed_legacy_index, bool)
                or not isinstance(claimed_legacy_index, int)
                or not legacy_value
                or claimed_legacy_index != _legacy_index(legacy_value)
            ):
                raise AssetLibraryConflict("Artifact legacy index conflict")
        loc = {
            "job": job.job_id,
            "server": job.server_id,
            "node": node,
            "key": key,
            "index": idx,
            "name": name,
            "sub": sub,
            "storage": storage,
            "media": media,
        }
        a = Artifact(
            aid,
            job.job_id,
            job.server_id,
            node,
            key,
            idx,
            name,
            sub,
            storage,
            cast(Literal["image", "audio", "video"], media),
            _digest(loc),
            str(x.get("created_at") or _now()),
            str(x.get("mime_type") or mimetypes.guess_type(name)[0] or "application/octet-stream"),
        )
        return a, legacy_value

    def _artifact_owned(self, c: sqlite3.Connection, aid: str, owner: str) -> Artifact | None:
        r = c.execute(
            self._artifact_sql() + " WHERE artifacts.artifact_id=? AND jobs.owner_id=?",
            (aid, owner),
        ).fetchone()
        return self._artifact(r) if r else None

    @staticmethod
    def _artifact_sql() -> str:
        return "SELECT artifacts.artifact_id,artifacts.job_id,artifacts.server_id,artifacts.upstream_node_id,artifacts.output_key,artifacts.upstream_output_index,artifacts.filename,artifacts.subfolder,artifacts.storage_type,artifacts.media_type,artifacts.digest,artifacts.created_at,COALESCE(f.mime_type,artifacts.mime_type,''),f.size_bytes,COALESCE(f.sha256,''),COALESCE(f.completeness,'locator_only') FROM artifacts JOIN jobs ON jobs.job_id=artifacts.job_id LEFT JOIN artifact_completeness f ON f.artifact_id=artifacts.artifact_id"

    @staticmethod
    def _artifact_available_sql() -> str:
        return """ AND f.artifact_id=artifacts.artifact_id
            AND EXISTS (
                SELECT 1 FROM job_artifact_collections AS collections
                WHERE collections.job_id=artifacts.job_id
                  AND collections.status='complete'
                  AND collections.output_snapshot_digest IS NOT NULL
                  AND collections.artifact_count=(
                      SELECT count(*) FROM artifacts AS collection_artifacts
                      WHERE collection_artifacts.job_id=artifacts.job_id
                  )
            )
            AND EXISTS (
                SELECT 1 FROM media_locations AS locations
                WHERE locations.owner_id=jobs.owner_id
                  AND locations.artifact_id=artifacts.artifact_id
                  AND locations.source_job_id=artifacts.job_id
                  AND locations.server_id=artifacts.server_id
                  AND locations.filename=artifacts.filename
                  AND locations.subfolder=artifacts.subfolder
                  AND locations.storage_type=artifacts.storage_type
                  AND locations.state='available'
            )"""

    @staticmethod
    def _artifact(r: sqlite3.Row) -> Artifact:
        return Artifact(
            str(r[0]),
            str(r[1]),
            str(r[2]),
            str(r[3]),
            str(r[4]),
            int(r[5]),
            str(r[6]),
            str(r[7]),
            str(r[8]),
            cast(Literal["image", "audio", "video"], str(r[9])),
            str(r[10]),
            str(r[11]),
            str(r[12]),
            int(r[13]) if r[13] is not None else None,
            str(r[14]),
            cast(Literal["locator_only", "verified"], str(r[15])),
        )

    @staticmethod
    def _asset(r: sqlite3.Row) -> Asset:
        return Asset(
            str(r[0]),
            str(r[1]),
            str(r[2]),
            str(r[3]),
            str(r[4]),
            cast(Literal["image", "audio", "video"], str(r[5])),
            str(r[6]),
            int(r[7]),
            str(r[8]),
            str(r[9]),
            str(r[10]),
        )

    @staticmethod
    def _summary(r: sqlite3.Row) -> dict[str, Any]:
        deleted = str(r[11]) if r[11] is not None else ""
        expiry = str(r[10]) if r[10] is not None else None
        return {
            "asset_id": str(r[0]),
            "resource_uri": f"comfyui://assets/{r[0]}",
            "server_id": str(r[1]),
            "media_type": str(r[4]),
            "mime_type": str(r[5]),
            "size_bytes": int(r[6]),
            "sha256": str(r[7]),
            "source_type": str(r[8]),
            "created_at": str(r[9]),
            "expires_at": expiry,
            "retention": {
                "state": "deleted" if deleted else "active",
                "expires_at": expiry,
                "deleted_at": deleted or None,
            },
        }

    @staticmethod
    def _insert_asset(c: sqlite3.Connection, a: Asset, source: str) -> None:
        name, sub = validate_media_locator(a.name, a.subfolder)
        if a.media_type not in _MEDIA or not _SHA.fullmatch(a.sha256):
            raise ValueError("invalid Asset facts")
        ref = f"{sub}/{name}" if sub else name
        c.execute(
            "INSERT INTO assets(asset_id,owner_id,server_id,name,subfolder,media_type,mime_type,size_bytes,sha256,source_type,comfyui_ref,created_at,expires_at,deleted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
            (
                a.asset_id,
                a.owner_id,
                a.server_id,
                name,
                sub,
                a.media_type,
                a.mime_type,
                a.size_bytes,
                a.sha256,
                source,
                ref,
                a.created_at,
            ),
        )

    @staticmethod
    def _transfer_sql() -> str:
        return (
            "SELECT transfer_id,artifact_id,target_server_id,target_asset_id,operation,"
            "strategy,state,plan_digest,planned_size_bytes,planned_sha256,"
            "planned_mime_type,network_policy_json,temporary_policy,created_at,"
            "expires_at,updated_at,completed_at,result_asset_id,result_size_bytes,"
            "result_sha256,failure_code FROM artifact_transfers"
        )

    @staticmethod
    def _transfer(r: sqlite3.Row) -> dict[str, Any]:
        out: dict[str, Any] = {
            "transfer_id": str(r[0]),
            "artifact_id": str(r[1]),
            "resource_uri": f"comfyui://artifacts/{r[1]}",
            "lineage_uri": f"comfyui://lineage/{r[1]}",
            "target_server_id": str(r[2]),
            "target_asset_id": str(r[3]),
            "operation": str(r[4]),
            "strategy": str(r[5]),
            "state": str(r[6]),
            "plan_digest": str(r[7]),
            "planned_size_bytes": int(r[8]),
            "planned_sha256": str(r[9]),
            "planned_mime_type": str(r[10]),
            "network_policy": json.loads(str(r[11])),
            "temporary_policy": str(r[12]),
            "created_at": str(r[13]),
            "expires_at": str(r[14]),
            "updated_at": str(r[15]),
        }
        if r[16] is not None:
            out["completed_at"] = str(r[16])
        if r[17] is not None:
            out.update(asset_id=str(r[17]), resource_uri=f"comfyui://assets/{r[17]}")
        if r[18] is not None:
            out["size_bytes"] = int(r[18])
            out["mime_type"] = str(r[10])
        if r[19] is not None:
            out["sha256"] = str(r[19])
        if r[20] is not None:
            out["failure_code"] = str(r[20])
        return out

    @staticmethod
    def _require_backfill_complete(
        connection: sqlite3.Connection, backfill_name: str | None = None
    ) -> None:
        if backfill_name is None:
            rows = connection.execute(
                "SELECT backfill_name,status,incomplete_count FROM phase_l_backfill_state ORDER BY backfill_name"
            ).fetchall()
            incomplete = len(rows) != 2 or any(
                str(row[1]) != "complete" or int(row[2]) != 0 for row in rows
            )
        else:
            row = connection.execute(
                "SELECT status,incomplete_count FROM phase_l_backfill_state WHERE backfill_name=?",
                (backfill_name,),
            ).fetchone()
            incomplete = row is None or str(row[0]) != "complete" or int(row[1]) != 0
        if incomplete:
            raise AssetLibraryConflict(
                "Phase L evidence is incomplete", details={"reason": "backfill_pending"}
            )

    @staticmethod
    def _collection(x: str) -> None:
        if not _COLLECTION.fullmatch(x):
            raise ValueError("invalid collection")

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._store.path, isolation_level=None, timeout=5.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("PRAGMA synchronous=FULL")
        c.execute("PRAGMA trusted_schema=OFF")
        return c


def _inject(injector: Callable[[str], None] | None, phase: str) -> None:
    if injector is not None:
        injector(phase)


def _legacy_index(uri: str) -> int | None:
    try:
        value = int(uri.rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return None
    return value if 0 <= value <= 2147483647 else None


def _json(x: object) -> str:
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(x: object) -> str:
    return hashlib.sha256(_json(x).encode()).hexdigest()


def _date(x: str) -> datetime:
    value = datetime.fromisoformat(x)
    if value.tzinfo is None:
        raise ValueError("timezone required")
    return value.astimezone(timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _optional_date(value: object) -> datetime | None:
    return None if value is None else _date(str(value))


def _target_asset_id(plan_digest: str) -> str:
    digest = hashlib.sha256(f"phase-l-transfer-target\0{plan_digest}".encode()).hexdigest()
    return f"asset_{digest}"


def _canonical_stored_json(value: str) -> str:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    return _json(decoded)
