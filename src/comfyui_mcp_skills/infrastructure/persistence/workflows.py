"""Safe workflow files repository."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from comfyui_mcp_skills.domain.identifiers import validate_identifier
from comfyui_mcp_skills.domain.models import Workflow
from comfyui_mcp_skills.domain.workflow_schema import (
    build_input_schema,
    normalize_parameters,
    validate_parameter_targets,
)

logger = logging.getLogger(__name__)


class FileWorkflowRepository:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir.resolve()
        self._root = (self._base_dir / "data").resolve()

    def list(self) -> list[Workflow]:
        if not self._root.exists():
            return []
        workflows: list[Workflow] = []
        for server_dir in sorted(self._root.iterdir(), key=lambda path: path.name):
            if not server_dir.is_dir() or server_dir.name in {"assets", "runs"}:
                continue
            for workflow_dir in sorted(server_dir.iterdir(), key=lambda path: path.name):
                try:
                    workflow = self._load(server_dir.name, workflow_dir.name)
                except (OSError, TypeError, ValueError) as exc:
                    logger.warning(
                        "Skipping invalid workflow %s/%s: %s",
                        server_dir.name,
                        workflow_dir.name,
                        exc,
                    )
                    continue
                if workflow is not None:
                    workflows.append(workflow)
        return workflows

    def get(self, server_id: str, workflow_id: str) -> Workflow | None:
        return self._load(server_id, workflow_id)

    def _load(self, server_id: str, workflow_id: str) -> Workflow | None:
        directory = self._safe_directory(server_id, workflow_id)
        schema_path = directory / "schema.json"
        graph_path = directory / "workflow.json"
        if not schema_path.is_file() or not graph_path.is_file():
            return None
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Skipping unreadable workflow %s/%s: %s", server_id, workflow_id, exc)
            return None
        if not isinstance(schema, dict) or not isinstance(graph, dict):
            logger.warning("Skipping workflow %s/%s: schema and graph must be objects", server_id, workflow_id)
            return None
        try:
            parameters = normalize_parameters(schema)
            validate_parameter_targets(parameters, graph)
            build_input_schema(parameters)
        except ValueError as exc:
            logger.warning("Skipping workflow %s/%s: %s", server_id, workflow_id, exc)
            return None
        return Workflow(
            server_id=server_id,
            workflow_id=workflow_id,
            description=str(schema.get("description", "")),
            parameters=parameters,
            graph=graph,
            enabled=schema.get("enabled", True) is True,
        )

    def _safe_directory(self, server_id: str, workflow_id: str) -> Path:
        server_id = validate_identifier(server_id, field="server_id")
        workflow_id = validate_identifier(workflow_id, field="workflow_id")
        directory = (self._root / server_id / workflow_id).resolve()
        try:
            directory.relative_to(self._root)
        except ValueError as exc:
            raise ValueError("Unsafe workflow path") from exc
        return directory
