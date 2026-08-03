"""Owner-bound Asset library, reuse, transfer, metadata, and delete workflows."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import struct
import tempfile
import uuid
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn, Protocol

from comfyui_mcp_skills.application.assets import detect_media
from comfyui_mcp_skills.application.execution import (
    DIRECT_OUTPUT_COMPATIBILITY_REGISTRY_VERSION,
    direct_output_compatible,
)
from comfyui_mcp_skills.domain.errors import (
    ArtifactNotFound,
    ArtifactTransferConflict,
    ArtifactTransferNotFound,
    AssetLibraryInvalidRequest,
    AssetMetadataUnavailable,
    AssetNotFound,
    ComfyUISkillsError,
    PayloadTooLarge,
    UnsupportedMediaType,
    UploadFailed,
)
from comfyui_mcp_skills.domain.media import validate_media_locator
from comfyui_mcp_skills.domain.models import Artifact, Asset
from comfyui_mcp_skills.infrastructure.persistence.sqlite_asset_library import (
    SQLiteAssetLibraryRepository,
)

_PLAN_TTL = timedelta(minutes=10)
_MEDIA = frozenset({"image", "audio", "video"})
_MAX_PNG_CHUNKS = 256
_MAX_TEXT_CHUNKS = 64
_MAX_PNG_CHUNK_BYTES = 8 * 1024 * 1024
_MAX_TEXT_CHUNK_BYTES = 256 * 1024
_MAX_TEXT_BYTES = 1024 * 1024
_PNG_READ_BLOCK_BYTES = 64 * 1024
_NETWORK_POLICY = {
    "version": 1,
    "mode": "bounded_source_download_target_upload_readback",
    "redirects": "disabled",
}
_TEMPORARY_POLICY = "private-ephemeral-restage-v1"
_PNG_PARAMETER_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "EmptyLatentImage": ("width", "height", "batch_size"),
    "KSampler": ("seed", "steps", "cfg", "sampler_name", "scheduler", "denoise"),
    "KSamplerAdvanced": (
        "noise_seed",
        "steps",
        "cfg",
        "sampler_name",
        "scheduler",
        "start_at_step",
        "end_at_step",
        "add_noise",
        "return_with_leftover_noise",
    ),
}
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class AssetLibraryGateway(Protocol):
    def download_output_to(
        self,
        filename: str,
        destination: str | Path,
        subfolder: str = "",
        storage_type: str = "output",
        *,
        max_bytes: int,
    ) -> dict[str, Any]: ...

    def upload_file(self, path: str, *, purpose: str, original_ref: str) -> dict[str, Any]: ...


class ServerConnections(Protocol):
    def connection(self, server_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class _TransferExecution:
    artifact: Artifact
    transfer_id: str
    owner_id: str
    target_server_id: str
    target_asset_id: str
    operation: str
    planned_size_bytes: int
    planned_sha256: str
    planned_mime_type: str
    lease_token: str
    lease_fence: int

    @property
    def target_name(self) -> str:
        return self.target_asset_id + Path(self.artifact.filename).suffix.lower()


class AssetLibraryService:
    """Expose safe public projections while retaining locators inside the trust boundary."""

    def __init__(
        self,
        repository: SQLiteAssetLibraryRepository,
        servers: ServerConnections,
        gateway_factory: Callable[[dict[str, Any]], AssetLibraryGateway],
        *,
        max_bytes: int = 100 * 1024 * 1024,
        staging_root: Path | None = None,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._repository = repository
        self._servers = servers
        self._gateway_factory = gateway_factory
        self._max_bytes = max_bytes
        self._staging_root = (
            staging_root.resolve()
            if staging_root is not None
            else Path(tempfile.gettempdir()).resolve() / "comfyui-mcp-skills-phase-l"
        )
        self._staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._staging_root.chmod(0o700)

    def list_assets(
        self,
        *,
        owner_id: str,
        limit: int = 20,
        cursor: str = "",
        media_type: str = "",
        collection: str = "",
    ) -> dict[str, Any]:
        self._owner(owner_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise AssetLibraryInvalidRequest("limit must be between 1 and 100")
        if media_type and media_type not in _MEDIA:
            raise AssetLibraryInvalidRequest("media_type is invalid")
        created_at: str | None = None
        asset_id: str | None = None
        if cursor:
            created_at, asset_id = self._decode_cursor(
                cursor, owner_id=owner_id, media_type=media_type, collection=collection
            )
        try:
            rows = self._repository.list_asset_records(
                owner_id,
                limit=limit + 1,
                after_created_at=created_at,
                after_asset_id=asset_id,
                media_type=media_type,
                collection=collection,
            )
        except ValueError as exc:
            raise AssetLibraryInvalidRequest("Asset list filters are invalid") from exc
        more = len(rows) > limit
        items = rows[:limit]
        next_cursor = ""
        if more and items:
            last = items[-1]
            next_cursor = self._encode_cursor(
                str(last["created_at"]),
                str(last["asset_id"]),
                owner_id=owner_id,
                media_type=media_type,
                collection=collection,
            )
        return {"items": items, "next_cursor": next_cursor}

    def describe(self, asset_id: str, *, owner_id: str) -> dict[str, Any]:
        self._owner(owner_id)
        record = self._repository.get_asset_record(asset_id, owner_id)
        if record is None:
            raise AssetNotFound("Asset was not found", details={"asset_id": asset_id})
        return record

    def collection_update(
        self,
        collection: str,
        asset_ids: list[str],
        action: str,
        *,
        owner_id: str,
    ) -> dict[str, Any]:
        self._owner(owner_id)
        try:
            count = self._repository.collection_update(owner_id, collection, asset_ids, action)
        except AssetNotFound:
            raise
        except (TypeError, ValueError) as exc:
            raise AssetLibraryInvalidRequest("Collection update is invalid") from exc
        return {"collection": collection, "action": action, "member_count": count}

    def metadata_extract(self, asset_id: str, *, owner_id: str) -> dict[str, Any]:
        self._owner(owner_id)
        cached = self._repository.metadata_projection(asset_id, owner_id)
        if cached is not None:
            return cached
        asset = self._owned_asset(asset_id, owner_id)
        if asset.mime_type != "image/png" and Path(asset.name).suffix.lower() != ".png":
            raise AssetMetadataUnavailable(
                "Metadata extraction is supported only for PNG Assets",
                details={"asset_id": asset_id, "reason": "unsupported_format"},
            )
        try:
            staged = self._temporary_path("metadata", ".png")
        except OSError as exc:
            raise AssetMetadataUnavailable("PNG metadata could not be staged") from exc
        try:
            gateway = self._gateway(asset.server_id)
            receipt = gateway.download_output_to(
                asset.name,
                staged,
                asset.subfolder,
                storage_type="input",
                max_bytes=self._max_bytes,
            )
            size_bytes, sha256 = self._verified_file(staged, receipt)
            if size_bytes != asset.size_bytes or sha256 != asset.sha256:
                raise AssetMetadataUnavailable(
                    "Asset content no longer matches recorded facts",
                    details={"asset_id": asset_id, "reason": "content_changed"},
                )
            projection, candidate_graphs = _png_projection(staged)
            for graph in candidate_graphs:
                revision = self._repository.match_revision_graph(graph)
                if revision is not None:
                    projection["revision"] = revision
                    break
            projection.update(
                {
                    "asset_id": asset_id,
                    "resource_uri": f"comfyui://assets/{asset_id}",
                    "source_sha256": sha256,
                }
            )
            self._repository.save_metadata_projection(asset_id, owner_id, sha256, projection)
            return projection
        except ComfyUISkillsError:
            raise
        except (OSError, ValueError) as exc:
            raise AssetMetadataUnavailable("PNG metadata could not be extracted") from exc
        finally:
            self._cleanup_temporary(staged)

    def import_output(
        self,
        artifact_id: str,
        target_server_id: str,
        workflow_id: str,
        parameter_name: str,
        *,
        owner_id: str,
    ) -> dict[str, Any]:
        self._owner(owner_id)
        artifact = self._owned_artifact(artifact_id, owner_id)
        self._connection(target_server_id)
        binding = self._repository.published_parameter_binding(
            workflow_id, target_server_id, parameter_name
        )
        if binding is None:
            raise AssetLibraryInvalidRequest(
                "Published Workflow parameter was not found",
                details={"workflow_id": workflow_id, "parameter_name": parameter_name},
            )
        consumer_class = str(binding.get("consumer_class", ""))
        parameter_media_type = str(binding.get("parameter_media_type", ""))
        parameter_field = str(binding.get("parameter_field", ""))
        parameter_storage_type = str(binding.get("parameter_storage_type", ""))
        revision_id = str(binding.get("revision_id", ""))
        revision_content_digest = str(binding.get("revision_content_digest", ""))
        deployment_id = str(binding.get("deployment_id", ""))
        if (
            not consumer_class
            or parameter_media_type not in {"image", "mask", "audio", "video"}
            or not parameter_field
            or parameter_storage_type not in {"input", "output"}
            or not revision_id
            or not _is_sha256(revision_content_digest)
            or not deployment_id
        ):
            raise AssetLibraryInvalidRequest("Published Workflow parameter binding is invalid")
        expected_media = "image" if parameter_media_type == "mask" else parameter_media_type
        if artifact.media_type != expected_media:
            raise AssetLibraryInvalidRequest(
                "Artifact media type is incompatible with the published Workflow parameter",
                details={
                    "artifact_id": artifact_id,
                    "parameter_name": parameter_name,
                    "expected_media_type": expected_media,
                },
            )
        projection = {
            "workflow_id": workflow_id,
            "parameter_name": parameter_name,
            "consumer_class": consumer_class,
            "parameter_media_type": parameter_media_type,
            "parameter_field": parameter_field,
            "parameter_storage_type": parameter_storage_type,
            "revision_id": revision_id,
            "revision_content_digest": revision_content_digest,
            "deployment_id": deployment_id,
        }
        if artifact.server_id == target_server_id and direct_output_compatible(
            consumer_class,
            parameter_media_type,
            parameter_field,
            parameter_storage_type,
        ):
            return {
                "artifact_id": artifact_id,
                "target_server_id": target_server_id,
                **projection,
                "compatibility_registry_version": (DIRECT_OUTPUT_COMPATIBILITY_REGISTRY_VERSION),
                "strategy": "direct",
                "resource_uri": artifact.resource_uri,
            }
        transfer = self._plan_transfer(
            artifact,
            target_server_id,
            owner_id=owner_id,
            operation="import",
        )
        transfer.update(projection)
        return transfer

    def delete_plan(self, asset_id: str, *, owner_id: str) -> dict[str, Any]:
        self._owner(owner_id)
        snapshot = self._repository.delete_snapshot(asset_id, owner_id)
        if snapshot is None:
            raise AssetNotFound("Asset was not found", details={"asset_id": asset_id})
        now = datetime.now(timezone.utc)
        expires_at = now + _PLAN_TTL
        body = {
            "owner_id": owner_id,
            "asset_id": asset_id,
            "asset_identity_digest": snapshot["asset_identity_digest"],
            "impact_digest": snapshot["impact_digest"],
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        plan_digest = _digest(body)
        plan = {
            **body,
            "plan_id": "plan_" + uuid.uuid4().hex,
            "plan_digest": plan_digest,
            "impact": snapshot["impact"],
        }
        self._repository.save_delete_plan(plan)
        return {
            "plan_id": plan["plan_id"],
            "plan_digest": plan_digest,
            "asset_id": asset_id,
            "resource_uri": f"comfyui://assets/{asset_id}",
            "impact": snapshot["impact"],
            "expires_at": expires_at.isoformat(),
        }

    def delete_commit(self, plan_id: str, plan_digest: str, *, owner_id: str) -> dict[str, Any]:
        self._owner(owner_id)
        return self._repository.commit_delete_plan(
            plan_id, plan_digest, owner_id, now=datetime.now(timezone.utc)
        )

    def transfer_plan(
        self, artifact_id: str, target_server_id: str, *, owner_id: str
    ) -> dict[str, Any]:
        self._owner(owner_id)
        artifact = self._owned_artifact(artifact_id, owner_id)
        self._connection(target_server_id)
        return self._plan_transfer(
            artifact, target_server_id, owner_id=owner_id, operation="transfer"
        )

    def transfer_commit(
        self, transfer_id: str, plan_digest: str, *, owner_id: str
    ) -> dict[str, Any]:
        self._owner(owner_id)
        claimed = self._repository.claim_transfer(
            transfer_id, plan_digest, owner_id, now=datetime.now(timezone.utc)
        )
        if claimed.get("completed"):
            return self._completed_transfer(transfer_id, owner_id)
        execution = self._transfer_execution(claimed, transfer_id, owner_id)
        staged: Path | None = None
        try:
            staged, size_bytes, sha256, mime_type = self._download_artifact(
                execution.artifact, upload_name=execution.target_name
            )
            if (size_bytes, sha256, mime_type) != (
                execution.planned_size_bytes,
                execution.planned_sha256,
                execution.planned_mime_type,
            ):
                raise ArtifactTransferConflict(
                    "Artifact content changed after transfer planning",
                    details={"reason": "content_changed"},
                )
            name, subfolder = self._upload_and_verify(execution, staged)
            self._complete_transfer(execution, name, subfolder)
            return self._completed_transfer(transfer_id, owner_id)
        except ComfyUISkillsError:
            self._fail_transfer_claim(
                transfer_id,
                owner_id,
                execution.lease_token,
                execution.lease_fence,
                "TRANSFER_FAILED",
            )
            raise
        except Exception as exc:
            self._fail_transfer_claim(
                transfer_id,
                owner_id,
                execution.lease_token,
                execution.lease_fence,
                "TRANSFER_FAILED",
            )
            raise UploadFailed("Artifact transfer failed") from exc
        finally:
            if staged is not None:
                self._cleanup_temporary(staged)

    def _transfer_execution(
        self, claimed: dict[str, Any], transfer_id: str, owner_id: str
    ) -> _TransferExecution:
        artifact = claimed.get("artifact")
        if not isinstance(artifact, Artifact):
            raise ArtifactTransferNotFound("Artifact transfer was not found")
        lease_token = claimed.get("lease_token")
        lease_fence = claimed.get("lease_fence")
        if (
            not isinstance(lease_token, str)
            or not lease_token
            or isinstance(lease_fence, bool)
            or not isinstance(lease_fence, int)
            or lease_fence < 1
        ):
            raise ArtifactTransferConflict(
                "Transfer claim is invalid", details={"reason": "invalid_claim"}
            )
        if claimed.get("strategy") != "upload":
            self._reject_transfer_claim(
                transfer_id,
                owner_id,
                lease_token,
                lease_fence,
                "UNSUPPORTED_STRATEGY",
                "Transfer strategy is not supported",
                "unsupported_strategy",
            )
        size_bytes, sha256, mime_type = self._claimed_plan_facts(
            claimed, transfer_id, owner_id, lease_token, lease_fence
        )
        target_asset_id, operation = self._claimed_target(
            claimed, transfer_id, owner_id, lease_token, lease_fence
        )
        return _TransferExecution(
            artifact=artifact,
            transfer_id=transfer_id,
            owner_id=owner_id,
            target_server_id=str(claimed.get("target_server_id", "")),
            target_asset_id=target_asset_id,
            operation=operation,
            planned_size_bytes=size_bytes,
            planned_sha256=str(sha256),
            planned_mime_type=mime_type,
            lease_token=lease_token,
            lease_fence=lease_fence,
        )

    def _claimed_plan_facts(
        self,
        claimed: dict[str, Any],
        transfer_id: str,
        owner_id: str,
        lease_token: str,
        lease_fence: int,
    ) -> tuple[int, str, str]:
        size_bytes = claimed.get("planned_size_bytes")
        sha256 = claimed.get("planned_sha256")
        mime_type = claimed.get("planned_mime_type")
        if (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
            or not _is_sha256(sha256)
            or not isinstance(mime_type, str)
            or not mime_type
            or claimed.get("network_policy") != _NETWORK_POLICY
            or claimed.get("temporary_policy") != _TEMPORARY_POLICY
        ):
            self._reject_transfer_claim(
                transfer_id,
                owner_id,
                lease_token,
                lease_fence,
                "INVALID_PLAN_FACTS",
                "Transfer plan facts are invalid",
                "invalid_plan_facts",
            )
        return size_bytes, str(sha256), mime_type

    def _claimed_target(
        self,
        claimed: dict[str, Any],
        transfer_id: str,
        owner_id: str,
        lease_token: str,
        lease_fence: int,
    ) -> tuple[str, str]:
        target_asset_id = str(claimed.get("target_asset_id", ""))
        operation = str(claimed.get("operation", ""))
        if not _is_asset_id(target_asset_id) or operation not in {"import", "transfer"}:
            self._reject_transfer_claim(
                transfer_id,
                owner_id,
                lease_token,
                lease_fence,
                "INVALID_TARGET_IDENTITY",
                "Transfer target identity is invalid",
                "invalid_target_identity",
            )
        return target_asset_id, operation

    def _reject_transfer_claim(
        self,
        transfer_id: str,
        owner_id: str,
        lease_token: str,
        lease_fence: int,
        failure_code: str,
        message: str,
        reason: str,
    ) -> NoReturn:
        self._fail_transfer_claim(transfer_id, owner_id, lease_token, lease_fence, failure_code)
        raise ArtifactTransferConflict(message, details={"reason": reason})

    def _upload_and_verify(self, execution: _TransferExecution, staged: Path) -> tuple[str, str]:
        target = self._gateway(execution.target_server_id)
        uploaded = target.upload_file(
            str(staged), purpose=execution.artifact.media_type, original_ref=""
        )
        try:
            name, subfolder = validate_media_locator(
                uploaded.get("name"), uploaded.get("subfolder", "")
            )
        except (AttributeError, ValueError) as exc:
            raise UploadFailed("ComfyUI upload returned an unsafe media locator") from exc
        if name != execution.target_name:
            raise UploadFailed("ComfyUI upload returned an unexpected media locator")
        readback: Path | None = None
        try:
            readback = self._temporary_path("readback", Path(name).suffix)
            receipt = target.download_output_to(
                name,
                readback,
                subfolder,
                storage_type="input",
                max_bytes=self._max_bytes,
            )
            size_bytes, sha256 = self._verified_file(readback, receipt)
            try:
                mime_type, media_type = self._validated_media(readback)
            except UnsupportedMediaType as exc:
                raise UploadFailed("Target input failed readback verification") from exc
            if media_type != execution.artifact.media_type or (size_bytes, sha256, mime_type) != (
                execution.planned_size_bytes,
                execution.planned_sha256,
                execution.planned_mime_type,
            ):
                raise UploadFailed("Target input failed readback verification")
            return name, subfolder
        finally:
            if readback is not None:
                self._cleanup_temporary(readback)

    def _complete_transfer(self, execution: _TransferExecution, name: str, subfolder: str) -> None:
        now = datetime.now(timezone.utc)
        asset = Asset(
            asset_id=execution.target_asset_id,
            server_id=execution.target_server_id,
            comfyui_ref=f"{subfolder}/{name}" if subfolder else name,
            name=name,
            subfolder=subfolder,
            media_type=execution.artifact.media_type,
            mime_type=execution.planned_mime_type,
            size_bytes=execution.planned_size_bytes,
            sha256=execution.planned_sha256,
            owner_id=execution.owner_id,
            created_at=now.isoformat(),
        )
        self._repository.complete_uploaded_transfer(
            execution.transfer_id,
            execution.owner_id,
            asset,
            relationship=execution.operation,
            size_bytes=execution.planned_size_bytes,
            sha256=execution.planned_sha256,
            mime_type=execution.planned_mime_type,
            lease_token=execution.lease_token,
            lease_fence=execution.lease_fence,
            now=now,
        )

    def _completed_transfer(self, transfer_id: str, owner_id: str) -> dict[str, Any]:
        completed = self._repository.get_transfer(transfer_id, owner_id)
        if completed is None:
            raise ArtifactTransferNotFound("Artifact transfer was not found")
        return completed

    def transfer_get(self, transfer_id: str, *, owner_id: str) -> dict[str, Any]:
        self._owner(owner_id)
        result = self._repository.get_transfer(transfer_id, owner_id)
        if result is None:
            raise ArtifactTransferNotFound("Artifact transfer was not found")
        return result

    def _plan_transfer(
        self,
        artifact: Artifact,
        target_server_id: str,
        *,
        owner_id: str,
        operation: str,
    ) -> dict[str, Any]:
        staged: Path | None = None
        try:
            staged, size_bytes, sha256, mime_type = self._download_artifact(artifact)
            if artifact.completeness == "verified" and (
                artifact.size_bytes != size_bytes
                or artifact.sha256 != sha256
                or artifact.mime_type != mime_type
            ):
                raise ArtifactTransferConflict(
                    "Artifact content no longer matches verified facts",
                    details={"reason": "content_changed"},
                )
            verified = self._repository.verify_artifact_facts(
                artifact.artifact_id,
                owner_id,
                size_bytes=size_bytes,
                sha256=sha256,
                mime_type=mime_type,
                observed_at=datetime.now(timezone.utc),
            )
            return self._create_transfer(
                verified,
                target_server_id,
                owner_id=owner_id,
                operation=operation,
                size_bytes=size_bytes,
                sha256=sha256,
                mime_type=mime_type,
            )
        except ComfyUISkillsError:
            raise
        except Exception as exc:
            raise UploadFailed("Artifact transfer planning failed") from exc
        finally:
            if staged is not None:
                self._cleanup_temporary(staged)

    def _create_transfer(
        self,
        artifact: Artifact,
        target_server_id: str,
        *,
        owner_id: str,
        operation: str,
        size_bytes: int,
        sha256: str,
        mime_type: str,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        body = {
            "owner_id": owner_id,
            "artifact_id": artifact.artifact_id,
            "artifact_identity_digest": self._repository.artifact_identity_digest(artifact),
            "target_server_id": target_server_id,
            "operation": operation,
            "strategy": "upload",
            "planned_size_bytes": size_bytes,
            "planned_sha256": sha256,
            "planned_mime_type": mime_type,
            "network_policy": _NETWORK_POLICY,
            "temporary_policy": _TEMPORARY_POLICY,
            "created_at": now.isoformat(),
            "expires_at": (now + _PLAN_TTL).isoformat(),
        }
        plan = {
            **body,
            "transfer_id": "transfer_" + uuid.uuid4().hex,
            "plan_digest": _digest(body),
        }
        return self._repository.save_transfer_plan(plan)

    def _download_artifact(
        self, artifact: Artifact, *, upload_name: str = ""
    ) -> tuple[Path, int, str, str]:
        try:
            staged = (
                self._temporary_named_path("upload", upload_name)
                if upload_name
                else self._temporary_path("transfer", Path(artifact.filename).suffix)
            )
        except (OSError, ValueError) as exc:
            raise UploadFailed("Artifact content could not be staged") from exc
        try:
            receipt = self._gateway(artifact.server_id).download_output_to(
                artifact.filename,
                staged,
                artifact.subfolder,
                storage_type=artifact.storage_type,
                max_bytes=self._max_bytes,
            )
            size_bytes, sha256 = self._verified_file(staged, receipt)
            mime_type, media_type = self._validated_media(staged)
            if media_type != artifact.media_type:
                raise UnsupportedMediaType(
                    "Artifact content does not match its declared media type"
                )
            if artifact.mime_type and artifact.mime_type != mime_type:
                raise UnsupportedMediaType("Artifact content does not match its declared MIME type")
            return staged, size_bytes, sha256, mime_type
        except ComfyUISkillsError:
            self._cleanup_temporary(staged)
            raise
        except Exception as exc:
            self._cleanup_temporary(staged)
            raise UploadFailed("Artifact content could not be downloaded") from exc

    @staticmethod
    def _validated_media(path: Path) -> tuple[str, str]:
        with path.open("rb") as handle:
            prefix = handle.read(16)
        return detect_media(path, prefix)

    def _fail_transfer_claim(
        self,
        transfer_id: str,
        owner_id: str,
        lease_token: str,
        lease_fence: int,
        failure_code: str,
    ) -> None:
        try:
            self._repository.fail_transfer(
                transfer_id,
                owner_id,
                failure_code,
                lease_token=lease_token,
                lease_fence=lease_fence,
            )
        except ArtifactTransferConflict:
            pass

    def _owned_asset(self, asset_id: str, owner_id: str) -> Asset:
        asset = self._repository.get(asset_id)
        if asset is None or asset.owner_id != owner_id:
            raise AssetNotFound("Asset was not found", details={"asset_id": asset_id})
        return asset

    def _owned_artifact(self, artifact_id: str, owner_id: str) -> Artifact:
        artifact = self._repository.get_artifact(artifact_id, owner_id)
        if artifact is None:
            raise ArtifactNotFound("Artifact was not found", details={"artifact_id": artifact_id})
        try:
            validate_media_locator(artifact.filename, artifact.subfolder)
        except ValueError as exc:
            raise ArtifactNotFound(
                "Artifact was not found", details={"artifact_id": artifact_id}
            ) from exc
        if artifact.storage_type != "output" or artifact.media_type not in _MEDIA:
            raise ArtifactNotFound("Artifact was not found", details={"artifact_id": artifact_id})
        if artifact.completeness == "verified" and (
            artifact.size_bytes is None
            or artifact.size_bytes < 0
            or len(artifact.sha256) != 64
            or any(character not in "0123456789abcdef" for character in artifact.sha256)
        ):
            raise ArtifactNotFound("Artifact was not found", details={"artifact_id": artifact_id})
        return artifact

    def _connection(self, server_id: str) -> dict[str, Any]:
        try:
            return self._servers.connection(server_id)
        except ComfyUISkillsError:
            raise
        except (TypeError, ValueError) as exc:
            raise AssetLibraryInvalidRequest("target_server_id is invalid") from exc

    def _gateway(self, server_id: str) -> AssetLibraryGateway:
        return self._gateway_factory(self._connection(server_id))

    def _temporary_path(self, prefix: str, suffix: str) -> Path:
        safe_suffix = suffix.lower() if suffix and len(suffix) <= 16 else ""
        path = self._staging_root / f"{prefix}-{uuid.uuid4().hex}{safe_suffix}"
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        path.chmod(0o600)
        return path

    def _temporary_named_path(self, prefix: str, filename: str) -> Path:
        name, subfolder = validate_media_locator(filename, "")
        if subfolder:
            raise ValueError("temporary filename must not contain a subfolder")
        directory = self._staging_root / f"{prefix}-{uuid.uuid4().hex}"
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
        path = directory / name
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        path.chmod(0o600)
        return path

    def _cleanup_temporary(self, path: Path) -> None:
        path.unlink(missing_ok=True)
        if path.parent != self._staging_root:
            try:
                path.parent.rmdir()
            except OSError:
                pass

    def _verified_file(self, path: Path, receipt: object) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                if size > self._max_bytes:
                    raise PayloadTooLarge("Downloaded output exceeds the transfer limit")
                digest.update(chunk)
        sha256 = digest.hexdigest()
        if not isinstance(receipt, dict):
            raise UploadFailed("Gateway returned an invalid download receipt")
        receipt_size = receipt.get("size_bytes")
        receipt_sha256 = receipt.get("sha256")
        if (
            isinstance(receipt_size, bool)
            or not isinstance(receipt_size, int)
            or receipt_size != size
            or not isinstance(receipt_sha256, str)
            or receipt_sha256 != sha256
        ):
            raise UploadFailed("Downloaded output failed digest verification")
        return size, sha256

    @staticmethod
    def _owner(owner_id: str) -> None:
        if not isinstance(owner_id, str) or not owner_id:
            raise AssetLibraryInvalidRequest("owner_id is required")

    @staticmethod
    def _cursor_signature(payload: dict[str, str]) -> str:
        return _digest(payload)

    def _encode_cursor(
        self,
        created_at: str,
        asset_id: str,
        *,
        owner_id: str,
        media_type: str,
        collection: str,
    ) -> str:
        payload = {
            "created_at": created_at,
            "asset_id": asset_id,
            "owner_id": owner_id,
            "media_type": media_type,
            "collection": collection,
        }
        wrapped = {**payload, "digest": self._cursor_signature(payload)}
        return base64.urlsafe_b64encode(_canonical(wrapped)).decode("ascii").rstrip("=")

    def _decode_cursor(
        self, cursor: str, *, owner_id: str, media_type: str, collection: str
    ) -> tuple[str, str]:
        try:
            padding = "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(cursor + padding))
            if not isinstance(value, dict):
                raise ValueError
            payload = {
                key: str(value[key])
                for key in ("created_at", "asset_id", "owner_id", "media_type", "collection")
            }
            if value.get("digest") != self._cursor_signature(payload):
                raise ValueError
            if (
                payload["owner_id"] != owner_id
                or payload["media_type"] != media_type
                or payload["collection"] != collection
            ):
                raise ValueError
            datetime.fromisoformat(payload["created_at"])
            return payload["created_at"], payload["asset_id"]
        except (binascii.Error, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AssetLibraryInvalidRequest("cursor is invalid") from exc


def _png_projection(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    chunks: list[dict[str, Any]] = []
    candidate_graphs: list[dict[str, Any]] = []
    recovered_parameters: list[dict[str, Any]] = []
    recovered_keys: set[bytes] = set()
    graph_keys: set[bytes] = set()
    total_text = 0
    with path.open("rb") as handle:
        if handle.read(8) != _PNG_SIGNATURE:
            raise ValueError("not a PNG")
        for _ in range(_MAX_PNG_CHUNKS):
            length_raw = handle.read(4)
            if len(length_raw) != 4:
                raise ValueError("truncated PNG")
            length = struct.unpack(">I", length_raw)[0]
            kind = handle.read(4)
            if len(kind) != 4 or any(
                not (ord("A") <= byte <= ord("Z") or ord("a") <= byte <= ord("z")) for byte in kind
            ):
                raise ValueError("invalid PNG chunk type")
            is_text = kind in {b"tEXt", b"zTXt", b"iTXt"}
            maximum = _MAX_TEXT_CHUNK_BYTES if is_text else _MAX_PNG_CHUNK_BYTES
            if length > maximum:
                raise ValueError("PNG chunk is too large")
            data, actual_crc = _read_png_chunk(handle, kind, length, collect=is_text)
            crc_raw = handle.read(4)
            if len(crc_raw) != 4:
                raise ValueError("truncated PNG")
            if actual_crc != struct.unpack(">I", crc_raw)[0]:
                raise ValueError("invalid PNG checksum")
            if is_text:
                keyword, value = _png_text_value(kind, data)
                total_text += len(value)
                if len(chunks) >= _MAX_TEXT_CHUNKS or total_text > _MAX_TEXT_BYTES:
                    raise ValueError("PNG text metadata exceeds limits")
                chunks.append(
                    {
                        "keyword": keyword,
                        "size_bytes": len(value),
                        "sha256": hashlib.sha256(value).hexdigest(),
                    }
                )
                if keyword.casefold() in {"prompt", "workflow"}:
                    document = _metadata_document(value)
                    for recovered in _recover_parameters(document):
                        key = _canonical(recovered)
                        if key not in recovered_keys:
                            recovered_keys.add(key)
                            recovered_parameters.append(recovered)
                    for graph in _candidate_graphs(document):
                        key = _canonical(graph)
                        if key not in graph_keys:
                            graph_keys.add(key)
                            candidate_graphs.append(graph)
            if kind == b"IEND":
                if length != 0 or handle.read(1):
                    raise ValueError("invalid PNG terminator")
                return (
                    {
                        "format": "png",
                        "text_chunks": chunks,
                        "recovered_parameters": recovered_parameters,
                    },
                    candidate_graphs,
                )
    raise ValueError("PNG chunk count exceeds limit")


def _read_png_chunk(handle: Any, kind: bytes, length: int, *, collect: bool) -> tuple[bytes, int]:
    remaining = length
    checksum = zlib.crc32(kind)
    collected = bytearray() if collect else None
    while remaining:
        block = handle.read(min(remaining, _PNG_READ_BLOCK_BYTES))
        if not block:
            raise ValueError("truncated PNG")
        remaining -= len(block)
        checksum = zlib.crc32(block, checksum)
        if collected is not None:
            collected.extend(block)
    return bytes(collected or b""), checksum & 0xFFFFFFFF


def _png_text_value(kind: bytes, data: bytes) -> tuple[str, bytes]:
    if kind == b"tEXt":
        keyword, separator, value = data.partition(b"\0")
        if not separator:
            raise ValueError("invalid tEXt chunk")
        return _keyword(keyword), value
    if kind == b"zTXt":
        keyword, separator, rest = data.partition(b"\0")
        if not separator or not rest or rest[0] != 0:
            raise ValueError("invalid zTXt chunk")
        return _keyword(keyword), _bounded_decompress(rest[1:])
    fields = data.split(b"\0", 5)
    if len(fields) != 6 or len(fields[1]) != 1 or len(fields[2]) != 1:
        raise ValueError("invalid iTXt chunk")
    keyword, compressed, method, _language, _translated, value = fields
    if compressed not in {b"\0", b"\1"} or method != b"\0":
        raise ValueError("invalid iTXt compression")
    return _keyword(keyword), _bounded_decompress(value) if compressed == b"\1" else value


def _bounded_decompress(value: bytes) -> bytes:
    decompressor = zlib.decompressobj()
    result = decompressor.decompress(value, _MAX_TEXT_CHUNK_BYTES + 1)
    if len(result) > _MAX_TEXT_CHUNK_BYTES or decompressor.unconsumed_tail or not decompressor.eof:
        raise ValueError("compressed PNG text exceeds limits")
    return result


def _keyword(value: bytes) -> str:
    if not 1 <= len(value) <= 79:
        raise ValueError("invalid PNG text keyword")
    decoded = value.decode("latin-1")
    if any(marker in decoded for marker in ("/", "\\", ":")) or not all(
        character.isprintable() and character not in "\r\n" for character in decoded
    ):
        raise ValueError("invalid PNG text keyword")
    return decoded


def _reject_json_constant(_value: str) -> None:
    raise ValueError("invalid number")


def _metadata_document(value: bytes) -> dict[str, Any]:
    try:
        document = json.loads(value.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("invalid ComfyUI PNG metadata") from exc
    if not isinstance(document, dict):
        raise ValueError("invalid ComfyUI PNG metadata")
    return document


def _candidate_graphs(document: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if _is_api_graph(document):
        candidates.append(document)
    for field in ("prompt", "graph"):
        nested = document.get(field)
        if isinstance(nested, dict) and _is_api_graph(nested):
            candidates.append(nested)
    return candidates


def _is_api_graph(value: dict[str, Any]) -> bool:
    return bool(value) and all(
        isinstance(node, dict)
        and isinstance(node.get("class_type"), str)
        and isinstance(node.get("inputs"), dict)
        for node in value.values()
    )


def _recover_parameters(document: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[tuple[str, dict[str, Any]]] = []
    for graph in _candidate_graphs(document):
        nodes.extend((str(node_id), node) for node_id, node in graph.items())
    workflow_nodes = document.get("nodes")
    if isinstance(workflow_nodes, list):
        for index, node in enumerate(workflow_nodes):
            if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
                nodes.append((str(node.get("id", index)), node))
    recovered: list[dict[str, Any]] = []
    for node_id, node in sorted(nodes, key=lambda item: _node_sort_key(item[0])):
        class_type = node.get("class_type", node.get("type"))
        if not isinstance(class_type, str):
            continue
        allowed = _PNG_PARAMETER_ALLOWLIST.get(class_type)
        inputs = node.get("inputs")
        if allowed is None or not isinstance(inputs, dict):
            continue
        parameters = {
            field: sanitized
            for field in allowed
            if field in inputs and (sanitized := _sanitized_parameter(inputs[field])) is not None
        }
        if parameters:
            recovered.append(
                {"node_id": node_id, "class_type": class_type, "parameters": parameters}
            )
    return recovered


def _sanitized_parameter(value: object) -> bool | int | float | str | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and -(2**63) <= value < 2**63:
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if (
        isinstance(value, str)
        and len(value) <= 256
        and not any(marker in value for marker in ("/", "\\", ":", ".."))
        and all(character.isprintable() and character not in "\r\n" for character in value)
    ):
        return value
    return None


def _node_sort_key(value: str) -> tuple[int, int | str]:
    return (0, int(value)) if value.isdigit() else (1, value)


def _is_asset_id(value: str) -> bool:
    suffix = value.removeprefix("asset_")
    return (
        value.startswith("asset_")
        and len(suffix) in {32, 64}
        and all(character in "0123456789abcdef" for character in suffix)
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()
