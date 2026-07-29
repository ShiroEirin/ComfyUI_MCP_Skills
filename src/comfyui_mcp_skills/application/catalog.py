"""Discover enabled workflows without transport concerns."""

from __future__ import annotations

from comfyui_mcp_skills.application.ports import WorkflowRepository
from comfyui_mcp_skills.domain.errors import WorkflowNotFound
from comfyui_mcp_skills.domain.models import Workflow


class WorkflowCatalog:
    def __init__(self, repository: WorkflowRepository) -> None:
        self._repository = repository

    def list_enabled(self) -> list[Workflow]:
        return [workflow for workflow in self._repository.list() if workflow.enabled]

    def get(self, server_id: str, workflow_id: str) -> Workflow:
        workflow = self._repository.get(server_id, workflow_id)
        if workflow is None or not workflow.enabled:
            raise WorkflowNotFound(f"Workflow not found: {server_id}/{workflow_id}")
        return workflow
