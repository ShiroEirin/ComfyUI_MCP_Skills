"""Read-only semantic workflow descriptions and dependency checks."""

from __future__ import annotations

from typing import Any, Protocol

from comfyui_mcp_skills.application.workflow_graph import (
    WorkflowGraphService,
    WorkflowValidationService,
)

_MAX_DEPENDENCY_NODES = 10_000
_MAX_MODEL_FOLDERS = 64
_MAX_MODELS_PER_FOLDER = 100_000


class WorkflowInspectionRepository(Protocol):
    def describe(self, workflow_id: str, server_id: str) -> dict[str, Any]: ...

    def list_revisions(self, workflow_id: str) -> list[dict[str, Any]]: ...

    def get_revision(self, revision_id: str) -> dict[str, Any]: ...
    def get_published_revision(self, workflow_id: str) -> dict[str, Any]: ...


class WorkflowInspectionGateway(Protocol):
    def get_object_info(self) -> dict[str, Any]: ...

    def get_models(self, folder: str) -> list[str]: ...


class WorkflowInspectionService:
    def __init__(
        self,
        workflows: WorkflowInspectionRepository,
        graphs: WorkflowGraphService,
        validation: WorkflowValidationService,
    ) -> None:
        self._workflows = workflows
        self._graphs = graphs
        self._validation = validation

    def describe(
        self,
        workflow_id: str,
        server_id: str,
        gateway: WorkflowInspectionGateway,
    ) -> dict[str, Any]:
        deployment, revision = self._published_revision(workflow_id, server_id)
        object_info = gateway.get_object_info()
        graph = _revision_graph(revision)
        semantic = self._graphs.describe(graph, object_info=object_info)
        # ``current_validation`` judges the Revision against today's runtime while
        # ``historical_validity`` only replays what was recorded when it was
        # accepted. A graph that stops satisfying a newer rule therefore stays
        # readable and describable, it just stops being currently executable.
        current_validation = self._validation.validate_api(graph, object_info)
        return {
            **_safe_deployment(deployment),
            "revision_created_at": str(revision.get("created_at", "")),
            "semantic_graph": semantic,
            "parameter_schema": _object(revision.get("parameter_schema"), "parameter schema"),
            "dependency_contract": _object(
                revision.get("dependency_contract"), "dependency contract"
            ),
            "validation": current_validation,
            "current_validation": current_validation,
            "historical_validity": _historical_validity(revision, deployment),
            "currently_executable": bool(current_validation.get("valid")),
        }

    def dependencies_check(
        self,
        workflow_id: str,
        server_id: str,
        gateway: WorkflowInspectionGateway,
    ) -> dict[str, Any]:
        deployment, revision = self._published_revision(workflow_id, server_id)
        contract = _object(revision.get("dependency_contract"), "dependency contract")
        required_nodes = _bounded_strings(contract.get("nodes", []), _MAX_DEPENDENCY_NODES)
        models = _bounded_models(contract.get("models", []))
        installed_nodes = set(gateway.get_object_info())
        missing_nodes = [node for node in required_nodes if node not in installed_nodes]
        folders = sorted({model["folder"] for model in models})
        if len(folders) > _MAX_MODEL_FOLDERS:
            raise ValueError("Workflow dependency contract exceeds model folder limit")
        installed_models: dict[str, set[str]] = {}
        for folder in folders:
            values = gateway.get_models(folder)
            if not isinstance(values, list) or len(values) > _MAX_MODELS_PER_FOLDER:
                raise ValueError("ComfyUI model inventory is invalid or oversized")
            installed_models[folder] = {
                value for value in values if isinstance(value, str) and value
            }
        missing_models = [
            model for model in models if model["filename"] not in installed_models[model["folder"]]
        ]
        coverage = str(contract.get("coverage", "partial"))
        if coverage not in {"complete", "partial"}:
            coverage = "partial"
        return {
            **_safe_deployment(deployment),
            "required_nodes": required_nodes,
            "required_models": models,
            "missing_nodes": missing_nodes,
            "missing_models": missing_models,
            "coverage": coverage,
            "unverified_loaders": _bounded_strings(
                contract.get("unverified_loaders", []), _MAX_DEPENDENCY_NODES
            ),
            "is_ready": not missing_nodes and not missing_models and coverage == "complete",
        }

    def graph_resource(self, workflow_id: str) -> dict[str, Any]:
        revision = self._workflows.get_published_revision(workflow_id)
        revision_id = revision.get("revision_id")
        if not isinstance(revision_id, str) or not revision_id:
            raise ValueError("Published Workflow revision identity is invalid")
        graph = _revision_graph(revision)
        return {
            "workflow_id": workflow_id,
            "revision_id": revision_id,
            "content_digest": str(revision.get("content_digest", "")),
            "created_at": str(revision.get("created_at", "")),
            "semantic_graph": self._graphs.describe(graph),
            "parameter_schema": _object(revision.get("parameter_schema"), "parameter schema"),
            "dependency_contract": _object(
                revision.get("dependency_contract"), "dependency contract"
            ),
        }

    def _published_revision(
        self, workflow_id: str, server_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        deployment = self._workflows.describe(workflow_id, server_id)
        revision_id = deployment.get("revision_id")
        if not isinstance(revision_id, str) or not revision_id:
            raise ValueError("Published Workflow has no immutable revision")
        revision = self._workflows.get_revision(revision_id)
        if revision.get("workflow_id") != workflow_id:
            raise ValueError("Workflow revision identity does not match deployment")
        return deployment, revision


def _safe_deployment(value: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "server_id",
        "workflow_id",
        "description",
        "revision_id",
        "deployment_id",
        "content_digest",
        "validation_status",
        "published",
        "historical_validity",
    )
    return {field: value[field] for field in fields if field in value}


def _historical_validity(
    revision: dict[str, Any], deployment: dict[str, Any]
) -> dict[str, Any]:
    """Return the recorded validity of a Revision, never a re-judgement.

    Repositories that already expose ``historical_validity`` are authoritative;
    otherwise the recorded deployment ``validation_status`` is the only fact
    available and must not be replaced by a validation run under today's rules.
    """
    for recorded in (revision.get("historical_validity"), deployment.get("historical_validity")):
        if isinstance(recorded, dict):
            return dict(recorded)
    status = str(deployment.get("validation_status", "")) or str(
        revision.get("validation_status", "")
    )
    return {
        "valid": status == "valid",
        "status": status or "unrecorded",
        "revision_id": str(revision.get("revision_id", "")),
        "source": "recorded_deployment",
    }


def _revision_graph(revision: dict[str, Any]) -> dict[str, Any]:
    return _object(revision.get("graph"), "Workflow graph")


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return dict(value)


def _bounded_strings(value: object, maximum: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("Workflow dependency string list is invalid or oversized")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError("Workflow dependency string list contains invalid values")
    return sorted(set(value))


def _bounded_models(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > _MAX_DEPENDENCY_NODES:
        raise ValueError("Workflow model dependency list is invalid or oversized")
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Workflow model dependency must be an object")
        fields = ("filename", "folder", "loader_node", "node_id")
        normalized = {field: item.get(field) for field in fields}
        if any(
            not isinstance(field_value, str) or not field_value
            for field_value in normalized.values()
        ):
            raise ValueError("Workflow model dependency contains invalid fields")
        result.append(normalized)  # type: ignore[arg-type]
    result.sort(key=lambda item: (item["folder"], item["filename"], item["node_id"]))
    return result
