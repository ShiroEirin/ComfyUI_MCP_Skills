"""Discover enabled workflows without transport concerns."""

from __future__ import annotations

import base64
import json
from typing import Any

from comfyui_mcp_skills.application.ports import WorkflowRepository
from comfyui_mcp_skills.domain.errors import WorkflowNotFound
from comfyui_mcp_skills.domain.models import Workflow

_MAX_LIST_LIMIT = 200


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

    def list_workflows(
        self,
        *,
        server_id: str = "",
        query: str = "",
        include_disabled: bool = False,
        limit: int = 50,
        cursor: str = "",
    ) -> dict[str, Any]:
        """List workflows with owner-bound filtering and stable keyset pagination.

        Deployment facts (revision/deployment/published/validation) are attached
        when the backing repository can describe them; file-backed repositories
        that cannot are listed without those fields.
        """
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_LIST_LIMIT
        ):
            raise ValueError(f"limit must be an integer between 1 and {_MAX_LIST_LIMIT}")
        workflows = self._repository.list()
        if not include_disabled:
            workflows = [workflow for workflow in workflows if workflow.enabled]
        if server_id:
            workflows = [workflow for workflow in workflows if workflow.server_id == server_id]
        if query:
            needle = query.casefold()
            workflows = [
                workflow
                for workflow in workflows
                if needle in workflow.workflow_id.casefold()
                or needle in workflow.description.casefold()
            ]
        workflows.sort(key=lambda workflow: (workflow.server_id, workflow.workflow_id))
        total = len(workflows)
        start = _decode_cursor(cursor)
        page = workflows[start : start + limit]
        next_cursor = ""
        if start + len(page) < total:
            last = page[-1]
            next_cursor = _encode_cursor(start + len(page), last.server_id, last.workflow_id)
        items = [self._list_item(workflow) for workflow in page]
        return {"items": items, "next_cursor": next_cursor, "total": total}

    def _list_item(self, workflow: Workflow) -> dict[str, Any]:
        item: dict[str, Any] = {
            "workflow_uri": (
                f"comfyui://workflows/{workflow.server_id}/{workflow.workflow_id}"
            ),
            "workflow_id": workflow.workflow_id,
            "server_id": workflow.server_id,
            "description": workflow.description,
            "enabled": workflow.enabled,
        }
        try:
            deployment = self._repository.describe(workflow.workflow_id, workflow.server_id)
        except (LookupError, RuntimeError, TypeError, ValueError):
            return item
        if not isinstance(deployment, dict):
            return item
        for key, target in (
            ("revision_id", "revision_id"),
            ("deployment_id", "deployment_id"),
            ("validation_status", "validation_status"),
        ):
            value = deployment.get(target)
            if isinstance(value, str) and value:
                item[key] = value
        published = deployment.get("published")
        if isinstance(published, bool):
            item["published"] = published
        return item


def _encode_cursor(offset: int, server_id: str, workflow_id: str) -> str:
    payload = json.dumps(
        {"o": offset, "s": server_id, "w": workflow_id},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> int:
    if not cursor:
        return 0
    try:
        payload = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        parsed = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("cursor is invalid") from None
    offset = parsed.get("o")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("cursor is invalid")
    return offset
