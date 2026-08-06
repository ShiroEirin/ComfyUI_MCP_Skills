"""Domain exception hierarchy independent from CLI and MCP transports."""

from __future__ import annotations

from typing import Any


class ComfyUISkillsError(Exception):
    """Base class for recoverable application failures."""

    code = "COMFYUI_SKILLS_ERROR"
    retryable = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


class AuditIdempotencyConflict(ComfyUISkillsError):
    """A caller-supplied request_id was already used for the same operation."""

    code = "AUDIT_IDEMPOTENCY_CONFLICT"
    retryable = False


class WorkflowArgumentsError(ComfyUISkillsError):
    code = "INVALID_WORKFLOW_ARGUMENTS"


class WorkflowNotFound(ComfyUISkillsError):
    code = "WORKFLOW_NOT_FOUND"


class WorkflowChangeNotFound(ComfyUISkillsError):
    code = "WORKFLOW_CHANGE_NOT_FOUND"


class WorkflowChangeConflict(ComfyUISkillsError):
    code = "WORKFLOW_CHANGE_CONFLICT"


class ServerNotFound(ComfyUISkillsError):
    code = "SERVER_NOT_FOUND"


class ServerOffline(ComfyUISkillsError):
    code = "SERVER_OFFLINE"
    retryable = True


class AssetNotFound(ComfyUISkillsError):
    code = "ASSET_NOT_FOUND"


class AssetLibraryInvalidRequest(ComfyUISkillsError):
    code = "ASSET_LIBRARY_INVALID_REQUEST"


class AssetLibraryConflict(ComfyUISkillsError):
    code = "ASSET_LIBRARY_CONFLICT"


class AssetMetadataUnavailable(ComfyUISkillsError):
    code = "ASSET_METADATA_UNAVAILABLE"


class AssetDeletePlanNotFound(ComfyUISkillsError):
    code = "ASSET_DELETE_PLAN_NOT_FOUND"


class ArtifactNotFound(ComfyUISkillsError):
    code = "ARTIFACT_NOT_FOUND"


class ArtifactTransferNotFound(ComfyUISkillsError):
    code = "ARTIFACT_TRANSFER_NOT_FOUND"


class ArtifactTransferConflict(ComfyUISkillsError):
    code = "ARTIFACT_TRANSFER_CONFLICT"


class UploadFailed(ComfyUISkillsError):
    code = "UPLOAD_FAILED"
    retryable = True


class UnsafePath(ComfyUISkillsError):
    code = "UNSAFE_PATH"


class PayloadTooLarge(ComfyUISkillsError):
    code = "PAYLOAD_TOO_LARGE"


class UnsupportedMediaType(ComfyUISkillsError):
    code = "UNSUPPORTED_MEDIA_TYPE"


class JobNotFound(ComfyUISkillsError):
    code = "JOB_NOT_FOUND"


class UnsafeCancel(ComfyUISkillsError):
    code = "UNSAFE_CANCEL"


class IdempotencyConflict(ComfyUISkillsError):
    code = "IDEMPOTENCY_CONFLICT"


class ExecutionInProgress(ComfyUISkillsError):
    code = "EXECUTION_IN_PROGRESS"
    retryable = True


class ExecutionFailed(ComfyUISkillsError):
    code = "EXECUTION_FAILED"
    retryable = True


class ExperimentInvalidRequest(ComfyUISkillsError):
    code = "EXPERIMENT_INVALID_REQUEST"


class ExperimentNotFound(ComfyUISkillsError):
    code = "EXPERIMENT_NOT_FOUND"


class ExperimentPlanConflict(ComfyUISkillsError):
    code = "EXPERIMENT_PLAN_CONFLICT"


class DiagnosticNotFound(ComfyUISkillsError):
    code = "DIAGNOSTIC_NOT_FOUND"


class RepairPlanNotFound(ComfyUISkillsError):
    code = "REPAIR_PLAN_NOT_FOUND"


class RepairPlanConflict(ComfyUISkillsError):
    code = "REPAIR_PLAN_CONFLICT"


class RetryNotAllowed(ComfyUISkillsError):
    code = "RETRY_NOT_ALLOWED"
