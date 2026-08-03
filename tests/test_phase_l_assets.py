"""Phase L asset-library behavior contracts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import zlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from comfyui_mcp_skills.application.asset_library import AssetLibraryService
from comfyui_mcp_skills.application.planning import ExecutionPlanningService
from comfyui_mcp_skills.domain.errors import (
    ArtifactTransferConflict,
    AssetLibraryConflict,
    AssetLibraryInvalidRequest,
    AssetMetadataUnavailable,
    AssetNotFound,
    UnsupportedMediaType,
    UploadFailed,
)
from comfyui_mcp_skills.domain.models import Asset, Job
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.sqlite_asset_library import (
    SQLiteAssetLibraryRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_workflows import (
    SQLiteWorkflowRepository,
)

_OWNER = "owner-a"
_OTHER_OWNER = "owner-b"
_CREATED = "2026-07-31T00:00:00+00:00"


class _Servers:
    def connection(self, server_id: str) -> dict[str, Any]:
        return {"id": server_id}


class _Gateway:
    def __init__(
        self,
        payload: bytes,
        *,
        uploaded_name: str = "",
        readback_payload: bytes | None = None,
        history: dict[str, Any] | None = None,
    ) -> None:
        self.payload = payload
        self.uploaded_name = uploaded_name
        self.history = history
        self.readback_payload = payload if readback_payload is None else readback_payload
        self.destinations: list[Path] = []
        self.uploads: list[Path] = []
        self.receipt_name = ""

    def get_history(
        self, prompt_id: str, *, timeout_seconds: float | None = None
    ) -> dict[str, Any] | None:
        return self.history

    def download_output_to(
        self,
        filename: str,
        destination: str | Path,
        subfolder: str = "",
        storage_type: str = "output",
        *,
        max_bytes: int,
    ) -> dict[str, Any]:
        payload = (
            self.readback_payload
            if storage_type == "input" and filename == self.receipt_name and subfolder == "phase-l"
            else self.payload
        )
        assert len(payload) <= max_bytes
        path = Path(destination)
        self.destinations.append(path)
        path.write_bytes(payload)
        return {
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    def upload_file(self, path: str, *, purpose: str, original_ref: str) -> dict[str, Any]:
        uploaded = Path(path)
        assert uploaded.read_bytes() == self.payload
        assert purpose in {"image", "audio", "video"}
        assert original_ref == ""
        self.uploads.append(uploaded)
        self.receipt_name = self.uploaded_name or uploaded.name
        return {"name": self.receipt_name, "subfolder": "phase-l"}


@pytest.fixture
def store(tmp_path: Path) -> SQLiteControlPlaneStore:
    value = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    value.initialize()
    return value


@pytest.fixture
def repository(store: SQLiteControlPlaneStore) -> SQLiteAssetLibraryRepository:
    return SQLiteAssetLibraryRepository(store)


def _asset(index: int, *, owner_id: str = _OWNER, sha256: str | None = None) -> Asset:
    digest = sha256 or hashlib.sha256(f"asset-{index}".encode()).hexdigest()
    return Asset(
        asset_id=f"asset_{index:032x}",
        server_id="source",
        comfyui_ref=f"asset-{index}.png",
        name=f"asset-{index}.png",
        subfolder="",
        media_type="image",
        mime_type="image/png",
        size_bytes=100 + index,
        sha256=digest,
        owner_id=owner_id,
        created_at=f"2026-07-31T00:00:{index:02d}+00:00",
    )


def _service(
    repository: SQLiteAssetLibraryRepository,
    gateway: _Gateway,
    tmp_path: Path,
) -> AssetLibraryService:
    return AssetLibraryService(
        repository,
        _Servers(),
        lambda _config: gateway,
        staging_root=tmp_path / "staging",
    )


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)


def _png_text(keyword: str, value: str) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"tEXt", keyword.encode("latin-1") + b"\0" + value.encode("latin-1"))
        + _png_chunk(b"IEND", b"")
    )


def _png_texts(values: list[tuple[str, str]]) -> bytes:
    chunks = (
        _png_chunk(b"tEXt", keyword.encode("latin-1") + b"\0" + value.encode("latin-1"))
        for keyword, value in values
    )
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks) + _png_chunk(b"IEND", b"")


def _record_artifact(
    repository: SQLiteAssetLibraryRepository, store: SQLiteControlPlaneStore
) -> str:
    observation = {
        "upstream_node_id": "9",
        "output_key": "images",
        "upstream_output_index": 0,
        "filename": "output.png",
        "subfolder": "generated",
        "storage_type": "output",
        "media_type": "image",
        "mime_type": "image/png",
    }
    job = Job(
        prompt_id="prompt-1",
        server_id="source",
        workflow_id="legacy-workflow",
        status="completed",
        outputs=(observation,),
        owner_id=_OWNER,
        job_id="job_" + "1" * 32,
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, workflow_id, plan_id, revision_id, deployment_id,
                owner_id, status, retry_of, created_at, created_at_source,
                legacy_migrated, execution_origin
            ) VALUES (
                ?, ?, NULL, NULL, NULL, ?, 'running', NULL, ?, 'test',
                1, 'legacy_migrated'
            )
            """,
            (job.job_id, job.workflow_id, job.owner_id, _CREATED),
        )
        connection.execute(
            """INSERT INTO execution_attempts(
                attempt_id,job_id,attempt,server_id,upstream_prompt_id,
                upstream_job_id,client_id,submission_state,created_at
            ) VALUES(?,?,1,?,?,NULL,'','submitted',?)""",
            ("attempt_" + "2" * 32, job.job_id, job.server_id, job.prompt_id, _CREATED),
        )
    artifacts = repository.record_artifacts(job, [observation])
    assert len(artifacts) == 1
    return artifacts[0].artifact_id


def _record_generated_gif(
    repository: SQLiteAssetLibraryRepository, store: SQLiteControlPlaneStore
) -> tuple[str, dict[str, Any]]:
    observation = {
        "upstream_node_id": "7",
        "output_key": "gifs",
        "upstream_output_index": 0,
        "filename": "animation.gif",
        "subfolder": "generated",
        "storage_type": "output",
        "media_type": "image",
        "mime_type": "image/gif",
    }
    job = Job(
        prompt_id="prompt-gif",
        server_id="source",
        workflow_id="gif-producer",
        status="completed",
        outputs=(observation,),
        owner_id=_OWNER,
        job_id="job_" + "9" * 32,
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """INSERT INTO jobs(
                job_id,workflow_id,owner_id,status,created_at,created_at_source,
                legacy_migrated,execution_origin
            ) VALUES(?,?,?,'running',?,'test',1,'legacy_migrated')""",
            (job.job_id, job.workflow_id, job.owner_id, _CREATED),
        )
        connection.execute(
            """INSERT INTO execution_attempts(
                attempt_id,job_id,attempt,server_id,upstream_prompt_id,
                upstream_job_id,client_id,submission_state,created_at
            ) VALUES(?,?,1,?,?,NULL,'','submitted',?)""",
            ("attempt_" + "9" * 32, job.job_id, job.server_id, job.prompt_id, _CREATED),
        )
    artifact = repository.record_artifacts(job, [observation])[0]
    return artifact.artifact_id, artifact.to_public_dict()


def _assert_gif_asset(service: AssetLibraryService, result: dict[str, Any]) -> None:
    described = service.describe(result["asset_id"], owner_id=_OWNER)
    assert described["media_type"] == "image"
    assert described["mime_type"] == "image/gif"


@pytest.mark.parametrize("signature", [b"GIF87a", b"GIF89a"])
def test_generated_gif_artifact_passes_bounded_transfer_verification(
    repository: SQLiteAssetLibraryRepository,
    store: SQLiteControlPlaneStore,
    tmp_path: Path,
    signature: bytes,
) -> None:
    gateway = _Gateway(signature + b"bounded-gif-payload")
    artifact_id, output = _record_generated_gif(repository, store)
    service = _service(repository, gateway, tmp_path)

    assert output["media_type"] == "image"
    assert output["mime_type"] == "image/gif"
    artifact = repository.get_artifact(artifact_id, _OWNER)
    assert artifact is not None
    assert artifact.media_type == "image"
    assert artifact.mime_type == "image/gif"

    plan = service.transfer_plan(artifact_id, "target", owner_id=_OWNER)
    result = service.transfer_commit(plan["transfer_id"], plan["plan_digest"], owner_id=_OWNER)

    assert plan["planned_mime_type"] == "image/gif"
    _assert_gif_asset(service, result)


@pytest.mark.parametrize("signature", [b"GIF87a", b"GIF89a"])
def test_generated_gif_artifact_passes_bounded_import_verification(
    repository: SQLiteAssetLibraryRepository,
    store: SQLiteControlPlaneStore,
    tmp_path: Path,
    signature: bytes,
) -> None:
    gateway = _Gateway(signature + b"bounded-gif-payload")
    artifact_id, _output = _record_generated_gif(repository, store)
    _published_consumer(store, consumer_class="CustomImageLoader")
    service = _service(repository, gateway, tmp_path)

    plan = service.import_output(artifact_id, "source", "reuse", "image", owner_id=_OWNER)
    result = service.transfer_commit(plan["transfer_id"], plan["plan_digest"], owner_id=_OWNER)

    assert plan["strategy"] == "upload"
    assert plan["planned_mime_type"] == "image/gif"
    _assert_gif_asset(service, result)


def test_list_assets_uses_owner_bound_keyset_pagination(
    repository: SQLiteAssetLibraryRepository, tmp_path: Path
) -> None:
    for index in range(1, 5):
        repository.save(_asset(index))
    repository.save(_asset(5, owner_id=_OTHER_OWNER))
    service = _service(repository, _Gateway(b""), tmp_path)

    first = service.list_assets(owner_id=_OWNER, limit=2)
    second = service.list_assets(owner_id=_OWNER, limit=2, cursor=first["next_cursor"])

    assert [item["asset_id"] for item in first["items"]] == [
        "asset_00000000000000000000000000000004",
        "asset_00000000000000000000000000000003",
    ]
    assert [item["asset_id"] for item in second["items"]] == [
        "asset_00000000000000000000000000000002",
        "asset_00000000000000000000000000000001",
    ]
    assert second["next_cursor"] == ""
    assert all(
        item["resource_uri"] == f"comfyui://assets/{item['asset_id']}" for item in first["items"]
    )
    assert "comfyui_ref" not in repr(first)


def test_collection_update_is_idempotent_and_owner_bound(
    repository: SQLiteAssetLibraryRepository, tmp_path: Path
) -> None:
    asset = _asset(1)
    repository.save(asset)
    service = _service(repository, _Gateway(b""), tmp_path)

    first = service.collection_update("favorites", [asset.asset_id], "add", owner_id=_OWNER)
    second = service.collection_update("favorites", [asset.asset_id], "add", owner_id=_OWNER)
    listed = service.list_assets(owner_id=_OWNER, collection="favorites")

    assert first == second == {"collection": "favorites", "action": "add", "member_count": 1}
    assert [item["asset_id"] for item in listed["items"]] == [asset.asset_id]
    with pytest.raises(AssetNotFound):
        service.collection_update("favorites", [asset.asset_id], "add", owner_id=_OTHER_OWNER)


def test_png_metadata_extraction_projects_facts_without_raw_prompt(
    repository: SQLiteAssetLibraryRepository, tmp_path: Path
) -> None:
    payload = _png_text("prompt", '{"3":{"class_type":"SecretNode"}}')
    asset = _asset(1, sha256=hashlib.sha256(payload).hexdigest())
    repository.save(replace(asset, size_bytes=len(payload)))
    service = _service(repository, _Gateway(payload), tmp_path)

    result = service.metadata_extract(asset.asset_id, owner_id=_OWNER)

    assert result["format"] == "png"
    assert result["text_chunks"][0]["keyword"] == "prompt"
    assert result["text_chunks"][0]["size_bytes"] > 0
    assert "SecretNode" not in repr(result)
    assert "class_type" not in repr(result)


def test_delete_commit_rechecks_reference_impact(
    repository: SQLiteAssetLibraryRepository, tmp_path: Path
) -> None:
    asset = _asset(1)
    repository.save(asset)
    service = _service(repository, _Gateway(b""), tmp_path)
    plan = service.delete_plan(asset.asset_id, owner_id=_OWNER)
    service.collection_update("new-reference", [asset.asset_id], "add", owner_id=_OWNER)

    with pytest.raises(AssetLibraryConflict) as raised:
        service.delete_commit(plan["plan_id"], plan["plan_digest"], owner_id=_OWNER)

    assert raised.value.details["reason"] == "impact_changed"
    assert service.describe(asset.asset_id, owner_id=_OWNER)["asset_id"] == asset.asset_id


def _published_consumer(
    store: SQLiteControlPlaneStore,
    *,
    consumer_class: str = "LoadImageOutput",
    media_type: str = "image",
) -> None:
    revision_id = "revision_" + ("c" * 64)
    deployment_id = "deployment_" + ("d" * 64)
    graph = {"1": {"class_type": consumer_class, "inputs": {"image": "old"}}}
    parameter_schema = {
        "parameters": {
            "image": {
                "node_id": "1",
                "field": "image",
                "type": media_type,
                "storage_type": "output" if consumer_class == "LoadImageOutput" else "input",
            }
        }
    }
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            INSERT INTO workflows(workflow_id,created_at)
            VALUES('reuse','2026-07-31T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO workflow_revisions(
                revision_id,workflow_id,graph_json,parameter_schema_json,
                dependency_contract_json,content_digest,created_at
            ) VALUES(?, 'reuse', ?, ?, '{}', ?, '2026-07-31T00:00:00+00:00')
            """,
            (
                revision_id,
                json.dumps(graph, sort_keys=True, separators=(",", ":")),
                json.dumps(parameter_schema, sort_keys=True, separators=(",", ":")),
                "e" * 64,
            ),
        )
        connection.execute(
            """
            INSERT INTO workflow_deployments(
                deployment_id,workflow_id,revision_id,server_id,enabled,
                validation_status,published,created_at
            ) VALUES(?, 'reuse', ?, 'source', 1, 'valid', 1, '2026-07-31T00:00:00+00:00')
            """,
            (deployment_id, revision_id),
        )


def _revision_for_graph(
    store: SQLiteControlPlaneStore, graph: dict[str, Any], content_digest: str
) -> str:
    revision_id = "revision_" + ("f" * 64)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO workflows(workflow_id,created_at) VALUES('metadata',?)",
            (_CREATED,),
        )
        connection.execute(
            """
            INSERT INTO workflow_revisions(
                revision_id,workflow_id,graph_json,parameter_schema_json,
                dependency_contract_json,content_digest,created_at
            ) VALUES(?, 'metadata', ?, '{}', '{}', ?, ?)
            """,
            (
                revision_id,
                json.dumps(graph, sort_keys=True, separators=(",", ":")),
                content_digest,
                _CREATED,
            ),
        )
    return revision_id


def test_import_output_direct_only_for_same_server_load_image_output(
    repository: SQLiteAssetLibraryRepository,
    store: SQLiteControlPlaneStore,
    tmp_path: Path,
) -> None:
    artifact_id = _record_artifact(repository, store)
    _published_consumer(store)
    gateway = _Gateway(b"unused")
    service = _service(repository, gateway, tmp_path)

    result = service.import_output(
        artifact_id,
        "source",
        "reuse",
        "image",
        owner_id=_OWNER,
    )

    assert result == {
        "artifact_id": artifact_id,
        "target_server_id": "source",
        "workflow_id": "reuse",
        "parameter_name": "image",
        "consumer_class": "LoadImageOutput",
        "parameter_media_type": "image",
        "parameter_field": "image",
        "parameter_storage_type": "output",
        "revision_id": "revision_" + "c" * 64,
        "revision_content_digest": "e" * 64,
        "deployment_id": "deployment_" + "d" * 64,
        "compatibility_registry_version": 1,
        "strategy": "direct",
        "resource_uri": f"comfyui://artifacts/{artifact_id}",
    }
    assert gateway.destinations == []
    assert gateway.uploads == []


def test_upload_transfer_binds_verified_facts_and_rechecks_target_bytes(
    repository: SQLiteAssetLibraryRepository,
    store: SQLiteControlPlaneStore,
    tmp_path: Path,
) -> None:
    payload = _png_text("Title", "safe title")
    artifact_id = _record_artifact(repository, store)
    gateway = _Gateway(payload)
    service = _service(repository, gateway, tmp_path)
    plan = service.transfer_plan(artifact_id, "target", owner_id=_OWNER)

    assert plan["strategy"] == "upload"
    assert plan["planned_sha256"] == hashlib.sha256(payload).hexdigest()
    assert plan["planned_size_bytes"] == len(payload)
    assert plan["planned_mime_type"] == "image/png"
    assert plan["network_policy"] == {
        "version": 1,
        "mode": "bounded_source_download_target_upload_readback",
        "redirects": "disabled",
    }
    assert plan["temporary_policy"] == "private-ephemeral-restage-v1"
    verified = repository.get_artifact(artifact_id, _OWNER)
    assert verified is not None and verified.completeness == "verified"

    result = service.transfer_commit(
        plan["transfer_id"],
        plan["plan_digest"],
        owner_id=_OWNER,
    )
    assert result["asset_id"] == plan["target_asset_id"]
    assert gateway.uploads[0].name == f"{plan['target_asset_id']}.png"

    assert result["strategy"] == "upload"
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["size_bytes"] == len(payload)
    assert result["mime_type"] == "image/png"
    assert result["resource_uri"] == f"comfyui://assets/{result['asset_id']}"
    assert result["lineage_uri"] == f"comfyui://lineage/{artifact_id}"
    assert all(not destination.exists() for destination in gateway.destinations)
    assert str(tmp_path) not in repr(result)
    described = service.describe(result["asset_id"], owner_id=_OWNER)
    assert described["lineage"]["source_artifact_id"] == artifact_id
    assert str(tmp_path) not in repr(described)


def test_locator_replacement_after_plan_conflicts_before_upload(
    repository: SQLiteAssetLibraryRepository,
    store: SQLiteControlPlaneStore,
    tmp_path: Path,
) -> None:
    original = _png_text("Title", "approved")
    artifact_id = _record_artifact(repository, store)
    gateway = _Gateway(original)
    service = _service(repository, gateway, tmp_path)
    plan = service.transfer_plan(artifact_id, "target", owner_id=_OWNER)
    gateway.payload = _png_text("Title", "replacement")

    with pytest.raises(ArtifactTransferConflict) as raised:
        service.transfer_commit(plan["transfer_id"], plan["plan_digest"], owner_id=_OWNER)

    assert raised.value.details["reason"] == "content_changed"
    assert gateway.uploads == []


def test_transfer_plan_rejects_declared_png_with_bad_signature(
    repository: SQLiteAssetLibraryRepository,
    store: SQLiteControlPlaneStore,
    tmp_path: Path,
) -> None:
    artifact_id = _record_artifact(repository, store)
    service = _service(repository, _Gateway(b"not-a-png"), tmp_path)

    with pytest.raises(UnsupportedMediaType):
        service.transfer_plan(artifact_id, "target", owner_id=_OWNER)


def test_target_receipt_substitution_is_caught_by_readback(
    repository: SQLiteAssetLibraryRepository,
    store: SQLiteControlPlaneStore,
    tmp_path: Path,
) -> None:
    payload = _png_text("Title", "source")
    substituted = _png_text("Title", "other-object")
    artifact_id = _record_artifact(repository, store)
    service = _service(
        repository,
        _Gateway(payload, readback_payload=substituted),
        tmp_path,
    )
    plan = service.transfer_plan(artifact_id, "target", owner_id=_OWNER)

    with pytest.raises(UploadFailed, match="readback verification"):
        service.transfer_commit(plan["transfer_id"], plan["plan_digest"], owner_id=_OWNER)


def test_non_direct_import_returns_reviewable_plan_without_upload(
    repository: SQLiteAssetLibraryRepository,
    store: SQLiteControlPlaneStore,
    tmp_path: Path,
) -> None:
    payload = _png_text("Title", "planned")
    artifact_id = _record_artifact(repository, store)
    _published_consumer(store, consumer_class="CustomImageLoader")
    gateway = _Gateway(payload)
    service = _service(repository, gateway, tmp_path)

    result = service.import_output(artifact_id, "source", "reuse", "image", owner_id=_OWNER)

    assert result["strategy"] == "upload"
    assert result["state"] == "planned"
    assert result["workflow_id"] == "reuse"
    assert result["parameter_name"] == "image"
    assert result["consumer_class"] == "CustomImageLoader"
    assert result["parameter_media_type"] == "image"
    assert result["parameter_field"] == "image"
    assert result["parameter_storage_type"] == "input"
    assert result["revision_id"] == "revision_" + "c" * 64
    assert result["deployment_id"] == "deployment_" + "d" * 64
    assert gateway.uploads == []


def test_import_validates_parameter_media_for_unknown_consumer(
    repository: SQLiteAssetLibraryRepository,
    store: SQLiteControlPlaneStore,
    tmp_path: Path,
) -> None:
    artifact_id = _record_artifact(repository, store)
    _published_consumer(store, consumer_class="CustomAudioLoader", media_type="audio")
    gateway = _Gateway(_png_text("Title", "wrong-media"))
    service = _service(repository, gateway, tmp_path)

    with pytest.raises(AssetLibraryInvalidRequest, match="incompatible"):
        service.import_output(artifact_id, "source", "reuse", "image", owner_id=_OWNER)

    assert gateway.destinations == []


def test_png_metadata_recovers_allowlisted_parameters_and_revision_reference(
    repository: SQLiteAssetLibraryRepository,
    store: SQLiteControlPlaneStore,
    tmp_path: Path,
) -> None:
    content_digest = "a" * 64
    prompt = {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 42,
                "steps": 20,
                "cfg": 7.5,
                "sampler_name": "euler",
                "scheduler": "normal",
                "positive": "must-not-leak",
            },
        }
    }
    workflow = {
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 1024, "height": 768, "batch_size": 1},
        }
    }
    revision_id = _revision_for_graph(store, prompt, content_digest)
    payload = _png_texts([("prompt", json.dumps(prompt)), ("workflow", json.dumps(workflow))])
    asset = _asset(1, sha256=hashlib.sha256(payload).hexdigest())
    repository.save(replace(asset, size_bytes=len(payload)))
    service = _service(repository, _Gateway(payload), tmp_path)

    result = service.metadata_extract(asset.asset_id, owner_id=_OWNER)

    assert result["recovered_parameters"] == [
        {
            "node_id": "3",
            "class_type": "KSampler",
            "parameters": {
                "cfg": 7.5,
                "sampler_name": "euler",
                "scheduler": "normal",
                "seed": 42,
                "steps": 20,
            },
        },
        {
            "node_id": "5",
            "class_type": "EmptyLatentImage",
            "parameters": {"batch_size": 1, "height": 768, "width": 1024},
        },
    ]
    assert result["revision"] == {
        "revision_id": revision_id,
        "workflow_id": "metadata",
        "content_digest": content_digest,
    }
    assert "must-not-leak" not in repr(result)


def test_png_parser_rejects_non_text_chunk_length_bomb(
    repository: SQLiteAssetLibraryRepository,
    tmp_path: Path,
) -> None:
    payload = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 0xFFFFFFFF) + b"IDAT"
    asset = _asset(1, sha256=hashlib.sha256(payload).hexdigest())
    repository.save(replace(asset, size_bytes=len(payload)))
    service = _service(repository, _Gateway(payload), tmp_path)

    with pytest.raises(AssetMetadataUnavailable):
        service.metadata_extract(asset.asset_id, owner_id=_OWNER)


def test_expired_transfer_lease_reclaims_and_rejects_late_token(
    repository: SQLiteAssetLibraryRepository,
    store: SQLiteControlPlaneStore,
    tmp_path: Path,
) -> None:
    payload = _png_text("Title", "lease")
    artifact_id = _record_artifact(repository, store)
    plan = _service(repository, _Gateway(payload), tmp_path).transfer_plan(
        artifact_id, "target", owner_id=_OWNER
    )
    first_now = datetime.now(timezone.utc)
    first = repository.claim_transfer(
        plan["transfer_id"], plan["plan_digest"], _OWNER, now=first_now
    )
    second_now = first_now + timedelta(seconds=2)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE artifact_transfers SET lease_expires_at=? WHERE transfer_id=?",
            ((first_now - timedelta(seconds=1)).isoformat(), plan["transfer_id"]),
        )
    second = repository.claim_transfer(
        plan["transfer_id"], plan["plan_digest"], _OWNER, now=second_now
    )
    assert second["lease_fence"] == first["lease_fence"] + 1
    assert second["lease_token"] != first["lease_token"]
    assert second["target_asset_id"] == plan["target_asset_id"]
    stale_asset = _asset(99, owner_id=_OWNER, sha256=plan["planned_sha256"])
    with pytest.raises(ArtifactTransferConflict, match="lease"):
        repository.complete_uploaded_transfer(
            plan["transfer_id"],
            _OWNER,
            replace(
                stale_asset,
                server_id="target",
                size_bytes=plan["planned_size_bytes"],
                mime_type=plan["planned_mime_type"],
            ),
            relationship="transfer",
            size_bytes=plan["planned_size_bytes"],
            sha256=plan["planned_sha256"],
            mime_type=plan["planned_mime_type"],
            lease_token=first["lease_token"],
            lease_fence=first["lease_fence"],
            now=second_now,
        )
    repository.fail_transfer(
        plan["transfer_id"],
        _OWNER,
        "TEST_COMPLETE",
        lease_token=second["lease_token"],
        lease_fence=second["lease_fence"],
    )


def test_owner_bound_artifact_lineage_rejects_cross_owner_fk(
    repository: SQLiteAssetLibraryRepository, store: SQLiteControlPlaneStore
) -> None:
    artifact_id = _record_artifact(repository, store)
    artifact = repository.get_artifact(artifact_id, _OWNER)
    assert artifact is not None
    other_asset = _asset(7, owner_id=_OTHER_OWNER)
    repository.save(other_asset)
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO asset_artifact_lineage(
                    asset_id,owner_id,source_artifact_id,source_job_id,relationship,created_at
                ) VALUES(?,?,?,?,?,?)""",
                (
                    other_asset.asset_id,
                    _OTHER_OWNER,
                    artifact_id,
                    artifact.job_id,
                    "import",
                    _CREATED,
                ),
            )


def test_retention_keeps_referenced_artifact_location(
    repository: SQLiteAssetLibraryRepository, store: SQLiteControlPlaneStore
) -> None:
    artifact_id = _record_artifact(repository, store)
    artifact = repository.get_artifact(artifact_id, _OWNER)
    assert artifact is not None
    derived = _asset(8, owner_id=_OWNER)
    repository.save(derived)
    due = datetime(2026, 7, 30, tzinfo=timezone.utc)
    now = datetime(2026, 7, 31, tzinfo=timezone.utc)
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """INSERT INTO asset_artifact_lineage(
                asset_id,owner_id,source_artifact_id,source_job_id,relationship,created_at
            ) VALUES(?,?,?,?,?,?)""",
            (derived.asset_id, _OWNER, artifact_id, artifact.job_id, "import", _CREATED),
        )
        connection.execute(
            """INSERT INTO media_retention_bindings(
                binding_id,owner_id,asset_id,artifact_id,source_job_id,
                archive_at,delete_at,retain_until,legal_hold,created_at,updated_at
            ) VALUES('binding-artifact',?,NULL,?,?,NULL,?,NULL,0,?,?)""",
            (_OWNER, artifact_id, artifact.job_id, due.isoformat(), _CREATED, _CREATED),
        )
    result = repository.apply_retention(now=now)
    assert result == {
        "locations_archived": 0,
        "locations_deleted": 0,
        "assets_tombstoned": 0,
    }
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT state FROM media_locations WHERE artifact_id=?", (artifact_id,)
        ).fetchone() == ("available",)


def test_retention_archives_then_tombstones_unreferenced_asset(
    repository: SQLiteAssetLibraryRepository, store: SQLiteControlPlaneStore
) -> None:
    asset = _asset(9)
    repository.save(asset)
    archive_due = datetime(2026, 7, 29, tzinfo=timezone.utc)
    delete_due = datetime(2026, 8, 2, tzinfo=timezone.utc)
    first_now = datetime(2026, 7, 31, tzinfo=timezone.utc)
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """INSERT INTO media_retention_bindings(
                binding_id,owner_id,asset_id,artifact_id,source_job_id,
                archive_at,delete_at,retain_until,legal_hold,created_at,updated_at
            ) VALUES('binding-asset',?,?,NULL,NULL,?,?,NULL,0,?,?)""",
            (
                _OWNER,
                asset.asset_id,
                archive_due.isoformat(),
                delete_due.isoformat(),
                _CREATED,
                _CREATED,
            ),
        )
    assert repository.apply_retention(now=first_now) == {
        "locations_archived": 1,
        "locations_deleted": 0,
        "assets_tombstoned": 0,
    }
    described = repository.get_asset_record(asset.asset_id, _OWNER)
    assert described is not None
    assert described["locations"] == [
        {
            "location_id": f"asset:{asset.asset_id}",
            "server_id": "source",
            "storage_type": "input",
            "state": "archived",
            "size_bytes": asset.size_bytes,
            "sha256": asset.sha256,
            "mime_type": asset.mime_type,
        }
    ]
    second_now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    assert repository.apply_retention(now=second_now) == {
        "locations_archived": 0,
        "locations_deleted": 1,
        "assets_tombstoned": 1,
    }
    assert repository.get_asset_record(asset.asset_id, _OWNER) is None


def test_artifact_delete_and_transfer_fail_closed_while_backfill_pending(
    repository: SQLiteAssetLibraryRepository,
    store: SQLiteControlPlaneStore,
    tmp_path: Path,
) -> None:
    artifact_id = _record_artifact(repository, store)
    asset = _asset(10)
    repository.save(asset)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """UPDATE phase_l_backfill_state
               SET status='pending',incomplete_count=1,completed_at=NULL
               WHERE backfill_name='artifact_outputs'"""
        )
    with pytest.raises(AssetLibraryConflict) as artifact_error:
        repository.get_artifact(artifact_id, _OWNER)
    assert artifact_error.value.details["reason"] == "backfill_pending"
    with pytest.raises(AssetLibraryConflict) as delete_error:
        repository.delete_snapshot(asset.asset_id, _OWNER)
    assert delete_error.value.details["reason"] == "backfill_pending"
    with pytest.raises(AssetLibraryConflict):
        _service(repository, _Gateway(_png_text("Title", "blocked")), tmp_path).transfer_plan(
            artifact_id, "target", owner_id=_OWNER
        )


def test_delete_snapshot_contains_normalized_plan_job_revision_chain(
    repository: SQLiteAssetLibraryRepository, store: SQLiteControlPlaneStore
) -> None:
    asset = _asset(11)
    repository.save(asset)
    _published_consumer(store, consumer_class="LoadImage", media_type="image")
    identity = ExecutionPlanningService(store, SQLiteWorkflowRepository(store)).materialize(
        server_id="source",
        workflow_id="reuse",
        owner_id=_OWNER,
        arguments={"image": asset.asset_id},
        resolved_inputs={"image": asset.comfyui_ref},
        client_id="delete-impact",
    )

    snapshot = repository.delete_snapshot(asset.asset_id, _OWNER)

    assert snapshot is not None
    assert snapshot["impact"]["plan_references"] == [
        {
            "plan_id": identity.plan_id,
            "parameter_name": "image",
            "consumer_node_id": "1",
            "consumer_input_name": "image",
            "consumer_class": "LoadImage",
            "reuse_strategy": "direct",
            "workflow_id": "reuse",
            "revision_id": identity.revision_id,
            "deployment_id": identity.deployment_id,
            "job_id": identity.job_id,
            "artifact_collection_status": "complete",
        }
    ]
    assert snapshot["impact"]["locations"][0]["location_id"] == f"asset:{asset.asset_id}"


def test_artifact_lineage_traverses_input_revision_plan_job_artifact(
    repository: SQLiteAssetLibraryRepository, store: SQLiteControlPlaneStore
) -> None:
    source = _asset(12)
    repository.save(source)
    _published_consumer(store, consumer_class="LoadImage", media_type="image")
    planning = ExecutionPlanningService(store, SQLiteWorkflowRepository(store))
    identity = planning.materialize(
        server_id="source",
        workflow_id="reuse",
        owner_id=_OWNER,
        arguments={"image": source.asset_id},
        resolved_inputs={"image": source.comfyui_ref},
        client_id="lineage-chain",
    )
    planning.finalize_submission(identity, upstream_prompt_id="prompt-lineage")
    observation = {
        "upstream_node_id": "9",
        "output_key": "images",
        "upstream_output_index": 0,
        "filename": "lineage.png",
        "subfolder": "generated",
        "storage_type": "output",
        "media_type": "image",
        "mime_type": "image/png",
    }
    completed = Job(
        prompt_id="prompt-lineage",
        server_id=identity.server_id,
        workflow_id="reuse",
        status="completed",
        outputs=(observation,),
        owner_id=_OWNER,
        job_id=identity.job_id,
        plan_id=identity.plan_id,
        revision_id=identity.revision_id,
        deployment_id=identity.deployment_id,
        plan_digest=identity.plan_digest,
    )
    artifact = repository.terminalize(completed, completed.outputs)[0]

    lineage = repository.artifact_lineage(artifact.artifact_id, _OWNER)

    assert lineage is not None
    assert lineage["artifact_id"] == artifact.artifact_id
    assert lineage["chain"]["revision"]["revision_id"] == identity.revision_id
    assert lineage["chain"]["plan"]["plan_id"] == identity.plan_id
    assert lineage["chain"]["plan"]["inputs"][0]["asset_id"] == source.asset_id
    assert lineage["chain"]["job"]["job_id"] == identity.job_id
    assert lineage["chain"]["job"]["artifact_collection_status"] == "complete"
