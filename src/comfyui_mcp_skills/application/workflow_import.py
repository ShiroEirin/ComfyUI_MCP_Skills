"""Semantic graph, validation, and immutable workflow import services."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from comfyui_mcp_skills.application.workflow_conversion import (
    convert_editor_workflow,
    detect_workflow_format,
)
from comfyui_mcp_skills.application.workflow_graph import (
    WorkflowGraphService,
    WorkflowValidationService,
)
from comfyui_mcp_skills.domain.identifiers import validate_identifier

_DEFAULT_TRUSTED_SECONDS_PER_RUN = 300.0


def _default_runtime_estimator(_server_id: str, _graph: dict[str, Any]) -> float:
    return _DEFAULT_TRUSTED_SECONDS_PER_RUN


class WorkflowRevisionWriter(Protocol):
    def create_revision(
        self,
        *,
        workflow_id: str,
        server_id: str,
        graph: dict[str, Any],
        parameter_schema: dict[str, Any],
        dependency_contract: dict[str, Any],
        content_digest: str,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ImportPreview:
    workflow_id: str
    server_id: str
    source_format: str
    graph: dict[str, Any]
    semantic_graph: dict[str, Any]
    parameter_schema: dict[str, Any]
    dependency_contract: dict[str, Any]
    content_digest: str
    deprecated_nodes: tuple[dict[str, str], ...]
    unsupported_nodes: tuple[str, ...]
    dropped_fields: tuple[str, ...]
    issues: tuple[dict[str, str], ...]
    requires_manual_review: bool

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "server_id": self.server_id,
            "source_format": self.source_format,
            "content_digest": self.content_digest,
            "semantic_graph": self.semantic_graph,
            "parameter_schema": self.parameter_schema,
            "dependency_contract": self.dependency_contract,
            "deprecated_nodes": [dict(node) for node in self.deprecated_nodes],
            "unsupported_nodes": list(self.unsupported_nodes),
            "dropped_fields": list(self.dropped_fields),
            "issues": [dict(issue) for issue in self.issues],
            "requires_manual_review": self.requires_manual_review,
        }


class WorkflowImportService:
    def __init__(
        self,
        graphs: WorkflowGraphService,
        validation: WorkflowValidationService,
        revisions: WorkflowRevisionWriter,
        *,
        runtime_estimator: Callable[[str, dict[str, Any]], float] = _default_runtime_estimator,
    ) -> None:
        self._graphs = graphs
        self._validation = validation
        self._revisions = revisions
        self._runtime_estimator = runtime_estimator

    def preview(
        self,
        source: object,
        *,
        workflow_id: str,
        server_id: str,
        object_info: dict[str, Any],
        media_type: str = "image",
        node_replacements: dict[str, str] | None = None,
    ) -> ImportPreview:
        workflow_id = validate_identifier(workflow_id, field="workflow_id")
        server_id = validate_identifier(server_id, field="server_id")
        source_format = detect_workflow_format(source)
        unsupported: tuple[str, ...] = ()
        dropped: tuple[str, ...] = ()
        if source_format == "editor":
            graph, unsupported, dropped = convert_editor_workflow(source, object_info)
        else:
            graph = _copy_json_object(source, field="workflow")
        validation = self._validation.validate_api(graph, object_info)
        unsupported = tuple(sorted(set(unsupported) | set(validation["unsupported_nodes"])))
        semantic = self._graphs.describe(graph, object_info=object_info, media_type=media_type)
        parameters = semantic["parameters"]
        dependencies = dict(semantic["dependencies"])
        output_cardinality = len(semantic["outputs"])
        trusted_seconds = self._runtime_estimator(server_id, graph)
        if output_cardinality > 100_000:
            raise ValueError("Workflow output cardinality must not exceed 100000")
        if (
            isinstance(trusted_seconds, bool)
            or not isinstance(trusted_seconds, (int, float))
            or not math.isfinite(float(trusted_seconds))
            or not 0 < float(trusted_seconds) <= 31_536_000
        ):
            raise ValueError("trusted Workflow runtime estimate must be finite and positive")
        dependencies["output_cardinality"] = output_cardinality
        dependencies["trusted_seconds_per_run"] = float(trusted_seconds)
        digest = _content_digest(graph, parameters, dependencies, semantic["outputs"])
        deprecated = _deprecated_nodes(graph, node_replacements or {})
        issues = tuple(dict(issue) for issue in validation["issues"])
        manual = bool(unsupported or dropped or issues or dependencies["coverage"] != "complete")
        return ImportPreview(
            workflow_id,
            server_id,
            source_format,
            graph,
            semantic,
            parameters,
            dependencies,
            digest,
            deprecated,
            unsupported,
            dropped,
            issues,
            manual,
        )

    def preview_many(
        self,
        items: list[dict[str, Any]],
        *,
        server_id: str,
        object_info: dict[str, Any],
        media_type: str = "image",
        node_replacements: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not 1 <= len(items) <= 100:
            raise ValueError("Workflow import batch must contain between 1 and 100 items")
        results: list[dict[str, Any]] = []
        for item in items:
            source_id = str(item.get("source_id", ""))[:128]
            workflow_id = str(item.get("workflow_id", ""))
            try:
                preview = self.preview(
                    item.get("source"),
                    workflow_id=workflow_id,
                    server_id=server_id,
                    object_info=object_info,
                    media_type=media_type,
                    node_replacements=node_replacements,
                )
            except (TypeError, ValueError) as exc:
                results.append(
                    {
                        "source_id": source_id,
                        "workflow_id": workflow_id[:128],
                        "status": "failed",
                        "error": str(exc)[:500],
                    }
                )
                continue
            results.append(
                {
                    "source_id": source_id,
                    "workflow_id": preview.workflow_id,
                    "status": "previewed",
                    "preview": preview.to_public_dict(),
                }
            )
        return {
            "results": results,
            "previewed": sum(item["status"] == "previewed" for item in results),
            "failed": sum(item["status"] == "failed" for item in results),
        }

    def commit(self, preview: ImportPreview) -> dict[str, Any]:
        if preview.requires_manual_review:
            raise ValueError("Workflow import requires manual review")
        if int(preview.dependency_contract.get("output_cardinality", 0)) <= 0:
            raise ValueError("Workflow import has no executable output contract")
        return self._revisions.create_revision(
            workflow_id=preview.workflow_id,
            server_id=preview.server_id,
            graph=preview.graph,
            parameter_schema={
                "description": "",
                "enabled": True,
                "parameters": preview.parameter_schema,
                "_output_contract": {
                    "version": 1,
                    "coverage": "complete",
                    "outputs": preview.semantic_graph["outputs"],
                },
            },
            dependency_contract=preview.dependency_contract,
            content_digest=preview.content_digest,
        )


def _copy_json_object(value: object, *, field: str) -> dict[str, Any]:
    try:
        copied = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be finite JSON") from exc
    if not isinstance(copied, dict):
        raise ValueError(f"{field} must be an object")
    return copied


def _deprecated_nodes(
    graph: dict[str, Any], replacements: dict[str, str]
) -> tuple[dict[str, str], ...]:
    result: list[dict[str, str]] = []
    for node_id in sorted(graph, key=_node_sort_key):
        node = graph[node_id]
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type", ""))
        replacement = replacements.get(class_type)
        if isinstance(replacement, str) and replacement:
            result.append({"node_id": str(node_id), "old": class_type, "new": replacement})
    return tuple(result)


def _content_digest(
    graph: dict[str, Any],
    parameters: dict[str, Any],
    dependencies: dict[str, Any],
    outputs: list[dict[str, Any]],
) -> str:
    payload = json.dumps(
        {
            "identity_version": 2,
            "graph": graph,
            "parameters": parameters,
            "dependencies": dependencies,
            "output_contract": {
                "version": 1,
                "coverage": "complete",
                "outputs": outputs,
            },
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _node_sort_key(value: object) -> tuple[int, int | str]:
    text = str(value)
    return (0, int(text)) if text.isdigit() else (1, text)
