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
