"""Plan, diff, commit, publish, and rollback immutable Workflow revisions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from comfyui_mcp_skills.application.workflow_graph import (
    WorkflowGraphService,
    WorkflowValidationService,
)
from comfyui_mcp_skills.domain.errors import WorkflowChangeNotFound
from comfyui_mcp_skills.domain.identifiers import validate_identifier
from comfyui_mcp_skills.domain.workflow_schema import (
    build_input_schema,
    normalize_parameters,
    validate_parameter_targets,
)

_MAX_OPERATIONS = 100
_PLAN_TTL_SECONDS = 900


class WorkflowChangeRepository(Protocol):
    def describe(self, workflow_id: str, server_id: str) -> dict[str, Any]: ...
    def get_revision(self, revision_id: str) -> dict[str, Any]: ...
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
    def save_change_plan(self, plan: dict[str, Any]) -> dict[str, Any]: ...
    def commit_change_plan(self, plan_id: str, plan_digest: str) -> dict[str, Any]: ...
    def publish(self, deployment_id: str) -> None: ...
    def rollback(
        self,
        workflow_id: str,
        server_id: str,
        target_revision_id: str,
        request_id: str,
        actor: str,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class WorkflowChangePlan:
    plan_id: str
    plan_digest: str
    workflow_id: str
    server_id: str
    base_revision_id: str
    operations: tuple[dict[str, Any], ...]
    graph: dict[str, Any]
    parameter_schema: dict[str, Any]
    dependency_contract: dict[str, Any]
    content_digest: str
    diff: dict[str, Any]
    actor: str
    created_at: str
    expires_at: str

    def to_record(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "plan_digest": self.plan_digest,
            "workflow_id": self.workflow_id,
            "server_id": self.server_id,
            "base_revision_id": self.base_revision_id,
            "operations": [dict(operation) for operation in self.operations],
            "graph": self.graph,
            "parameter_schema": self.parameter_schema,
            "dependency_contract": self.dependency_contract,
            "content_digest": self.content_digest,
            "diff": self.diff,
            "actor": self.actor,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    def to_public_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.to_record().items()
            if key not in {"graph", "parameter_schema", "dependency_contract", "actor"}
        }


class WorkflowChangeService:
    def __init__(
        self,
        repository: WorkflowChangeRepository,
        graphs: WorkflowGraphService,
        validation: WorkflowValidationService,
        *,
        actor: str,
    ) -> None:
        if not actor or len(actor) > 128:
            raise ValueError("actor must contain between 1 and 128 characters")
        self._repository = repository
        self._graphs = graphs
        self._validation = validation
        self._actor = actor

    def plan(
        self,
        workflow_id: str,
        server_id: str,
        operations: list[dict[str, Any]],
        *,
        object_info: dict[str, Any],
    ) -> dict[str, Any]:
        workflow_id = validate_identifier(workflow_id, field="workflow_id")
        server_id = validate_identifier(server_id, field="server_id")
        if not 1 <= len(operations) <= _MAX_OPERATIONS:
            raise ValueError("operations must contain between 1 and 100 items")
        try:
            deployment = self._repository.describe(workflow_id, server_id)
        except LookupError as exc:
            raise WorkflowChangeNotFound(
                "Published Workflow was not found",
                details={"workflow_id": workflow_id, "server_id": server_id},
            ) from exc
        base_revision_id = _required_string(deployment, "revision_id")
        base = self._repository.get_revision(base_revision_id)
        graph = _json_copy(base.get("graph"), "Workflow graph")
        base_schema = _json_copy(base.get("parameter_schema"), "parameter schema")
        parameter_schema = _json_copy(base_schema, "parameter schema")
        normalized_operations = tuple(
            _apply_operation(graph, parameter_schema, operation, index=index)
            for index, operation in enumerate(operations)
        )
        _validate_acyclic(graph)
        validation = self._validation.validate_api(graph, object_info)
        if not validation["valid"]:
            messages = "; ".join(issue["message"] for issue in validation["issues"][:10])
            raise ValueError(f"Workflow change is invalid: {messages}")
        after_semantic = self._graphs.describe(graph, object_info=object_info)
        generated_parameters = after_semantic["parameters"]
        explicit = parameter_schema.get("parameters")
        explicit = explicit if isinstance(explicit, dict) else {}
        parameters = {**generated_parameters, **explicit}
        _apply_exposures(parameters, normalized_operations, graph, object_info)
        parameter_schema["parameters"] = normalize_parameters({"parameters": parameters})
        validate_parameter_targets(parameter_schema["parameters"], graph)
        build_input_schema(parameter_schema["parameters"])
        parameter_schema["_output_contract"] = _complete_output_contract(after_semantic["outputs"])
        dependencies = after_semantic["dependencies"]
        content_digest = _revision_digest(graph, parameter_schema, dependencies)
        if content_digest == str(base.get("content_digest", "")):
            raise ValueError("Workflow change has no observable effect")
        before_output_contract = _stored_output_contract(base_schema)
        diff = _semantic_diff(
            base_revision_id,
            graph_before=_json_copy(base.get("graph"), "base Workflow graph"),
            graph_after=graph,
            schema_before=base_schema,
            schema_after=parameter_schema,
            dependencies_before=_json_copy(base.get("dependency_contract"), "base dependencies"),
            dependencies_after=dependencies,
            outputs_before=before_output_contract["outputs"],
            outputs_after=after_semantic["outputs"],
            output_coverage_before=str(before_output_contract["coverage"]),
            output_coverage_after="complete",
        )
        now = datetime.now(timezone.utc)
        created_at = now.isoformat()
        expires_at = (now + timedelta(seconds=_PLAN_TTL_SECONDS)).isoformat()
        plan_digest = _sha256(
            {
                "workflow_id": workflow_id,
                "server_id": server_id,
                "base_revision_id": base_revision_id,
                "operations": normalized_operations,
                "content_digest": content_digest,
                "actor": self._actor,
                "created_at": created_at,
                "expires_at": expires_at,
            }
        )
        plan_id = "plan_" + plan_digest
        plan = WorkflowChangePlan(
            plan_id,
            plan_digest,
            workflow_id,
            server_id,
            base_revision_id,
            normalized_operations,
            graph,
            parameter_schema,
            dependencies,
            content_digest,
            diff,
            self._actor,
            created_at,
            expires_at,
        )
        stored = self._repository.save_change_plan(plan.to_record())
        return {
            **plan.to_public_dict(),
            "committed_revision_id": stored.get("committed_revision_id"),
        }

    def commit(self, plan_id: str, plan_digest: str) -> dict[str, Any]:
        return self._repository.commit_change_plan(plan_id, plan_digest)

    def diff(self, from_revision_id: str, to_revision_id: str) -> dict[str, Any]:
        try:
            before = self._repository.get_revision(from_revision_id)
            after = self._repository.get_revision(to_revision_id)
        except LookupError as exc:
            raise WorkflowChangeNotFound(
                "Workflow Revision was not found",
                details={"from_revision_id": from_revision_id, "to_revision_id": to_revision_id},
            ) from exc
        if before.get("workflow_id") != after.get("workflow_id"):
            raise ValueError("Workflow revision diff requires one workflow identity")
        before_graph = _json_copy(before.get("graph"), "from Workflow graph")
        after_graph = _json_copy(after.get("graph"), "to Workflow graph")
        before_schema = _json_copy(before.get("parameter_schema"), "from parameter schema")
        after_schema = _json_copy(after.get("parameter_schema"), "to parameter schema")
        before_output_contract = _stored_output_contract(before_schema)
        after_output_contract = _stored_output_contract(after_schema)
        return _semantic_diff(
            from_revision_id,
            graph_before=before_graph,
            graph_after=after_graph,
            schema_before=before_schema,
            schema_after=after_schema,
            dependencies_before=_json_copy(before.get("dependency_contract"), "from dependencies"),
            dependencies_after=_json_copy(after.get("dependency_contract"), "to dependencies"),
            outputs_before=before_output_contract["outputs"],
            outputs_after=after_output_contract["outputs"],
            output_coverage_before=str(before_output_contract["coverage"]),
            output_coverage_after=str(after_output_contract["coverage"]),
            to_revision_id=to_revision_id,
        )

    def publish(self, deployment_id: str) -> dict[str, Any]:
        try:
            self._repository.publish(deployment_id)
        except LookupError as exc:
            raise WorkflowChangeNotFound(
                "Workflow Deployment was not found",
                details={"deployment_id": deployment_id},
            ) from exc
        return {"deployment_id": deployment_id, "published": True}

    def rollback(
        self,
        workflow_id: str,
        server_id: str,
        target_revision_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        workflow_id = validate_identifier(workflow_id, field="workflow_id")
        server_id = validate_identifier(server_id, field="server_id")
        if not request_id or len(request_id) > 256:
            raise ValueError("request_id must contain between 1 and 256 characters")
        return self._repository.rollback(
            workflow_id,
            server_id,
            target_revision_id,
            request_id,
            self._actor,
        )


def _apply_operation(
    graph: dict[str, Any],
    parameter_schema: dict[str, Any],
    operation: dict[str, Any],
    *,
    index: int,
) -> dict[str, Any]:
    if not isinstance(operation, dict):
        raise TypeError(f"operations[{index}] must be an object")
    copied = _json_copy(operation, f"operations[{index}]")
    kind = copied.get("op")
    if kind not in {"set_input", "connect", "disconnect", "expose_parameter"}:
        raise ValueError(f"operations[{index}].op is unsupported")
    allowed = {
        "set_input": {"op", "node_id", "field", "value"},
        "connect": {"op", "source_node_id", "source_output", "target_node_id", "target_input"},
        "disconnect": {"op", "node_id", "field"},
        "expose_parameter": {"op", "node_id", "field", "name", "required"},
    }[str(kind)]
    unexpected = set(copied) - allowed
    if unexpected:
        raise ValueError(
            f"operations[{index}] has unexpected fields: {', '.join(sorted(unexpected))}"
        )
    if kind == "connect":
        source_id = _operation_string(copied, "source_node_id", index)
        target_id = _operation_string(copied, "target_node_id", index)
        target_field = _operation_string(copied, "target_input", index)
        source_output = copied.get("source_output")
        if (
            isinstance(source_output, bool)
            or not isinstance(source_output, int)
            or source_output < 0
        ):
            raise TypeError(f"operations[{index}].source_output must be a non-negative integer")
        _inputs(graph, source_id, index)
        _inputs(graph, target_id, index)[target_field] = [source_id, source_output]
        _remove_parameter_target(parameter_schema, target_id, target_field)
    else:
        node_id = _operation_string(copied, "node_id", index)
        field = _operation_string(copied, "field", index)
        inputs = _inputs(graph, node_id, index)
        if kind == "set_input":
            value = copied.get("value")
            if isinstance(value, list):
                raise ValueError(f"operations[{index}].value cannot encode a connection")
            inputs[field] = value
        elif kind == "disconnect":
            current = inputs.get(field)
            if not _is_connection(current):
                raise ValueError(f"operations[{index}] target is not connected")
            del inputs[field]
            _remove_parameter_target(parameter_schema, node_id, field)
        else:
            if field not in inputs or _is_connection(inputs[field]):
                raise ValueError(f"operations[{index}] can expose only a scalar input")
            validate_identifier(_operation_string(copied, "name", index), field="parameter_name")
            required = copied.get("required", False)
            if not isinstance(required, bool):
                raise TypeError(f"operations[{index}].required must be a boolean")
    return copied


def _inputs(graph: dict[str, Any], node_id: str, index: int) -> dict[str, Any]:
    node = graph.get(node_id)
    if not isinstance(node, dict):
        raise ValueError(f"operations[{index}] references missing node {node_id}")
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError(f"operations[{index}] references a node without inputs")
    return inputs


def _remove_parameter_target(schema: dict[str, Any], node_id: str, field: str) -> None:
    parameters = schema.get("parameters")
    if not isinstance(parameters, dict):
        return
    for name in list(parameters):
        parameter = parameters[name]
        if (
            isinstance(parameter, dict)
            and parameter.get("node_id") == node_id
            and parameter.get("field") == field
        ):
            del parameters[name]


def _semantic_diff(
    base_revision_id: str,
    *,
    graph_before: dict[str, Any],
    graph_after: dict[str, Any],
    schema_before: dict[str, Any],
    schema_after: dict[str, Any],
    dependencies_before: dict[str, Any],
    dependencies_after: dict[str, Any],
    outputs_before: list[dict[str, Any]],
    outputs_after: list[dict[str, Any]],
    output_coverage_before: str,
    output_coverage_after: str,
    to_revision_id: str = "",
) -> dict[str, Any]:
    before_nodes = set(graph_before)
    after_nodes = set(graph_after)
    input_changes: list[dict[str, Any]] = []
    for node_id in sorted(before_nodes & after_nodes, key=_node_sort_key):
        before_inputs = graph_before[node_id].get("inputs", {})
        after_inputs = graph_after[node_id].get("inputs", {})
        if not isinstance(before_inputs, dict) or not isinstance(after_inputs, dict):
            continue
        for field in sorted(set(before_inputs) | set(after_inputs)):
            before_value = before_inputs.get(field, _MISSING)
            after_value = after_inputs.get(field, _MISSING)
            if before_value != after_value:
                input_changes.append(
                    {
                        "node_id": str(node_id),
                        "field": field,
                        "before": None if before_value is _MISSING else before_value,
                        "after": None if after_value is _MISSING else after_value,
                    }
                )
    before_parameters = _parameter_map(schema_before)
    after_parameters = _parameter_map(schema_after)
    before_names = set(before_parameters)
    after_names = set(after_parameters)
    parameter_changes = [
        {
            "name": name,
            "before": _public_parameter(before_parameters[name]),
            "after": _public_parameter(after_parameters[name]),
        }
        for name in sorted(before_names & after_names)
        if before_parameters[name] != after_parameters[name]
    ]
    result = {
        "base_revision_id": base_revision_id,
        "nodes_added": sorted(after_nodes - before_nodes, key=_node_sort_key),
        "nodes_removed": sorted(before_nodes - after_nodes, key=_node_sort_key),
        "input_changes": input_changes,
        "parameter_schema_changed": schema_before != schema_after,
        "parameters_added": [
            {"name": name, **_public_parameter(after_parameters[name])}
            for name in sorted(after_names - before_names)
        ],
        "parameters_removed": [
            {"name": name, **_public_parameter(before_parameters[name])}
            for name in sorted(before_names - after_names)
        ],
        "parameter_changes": parameter_changes,
        "dependencies_before": dependencies_before,
        "dependencies_after": dependencies_after,
        "outputs_before": outputs_before,
        "outputs_after": outputs_after,
        "output_coverage_before": output_coverage_before,
        "output_coverage_after": output_coverage_after,
        "parameters_before": sorted(before_names),
        "parameters_after": sorted(after_names),
    }
    if to_revision_id:
        result["to_revision_id"] = to_revision_id
    return result


def _parameter_map(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    parameters = schema.get("parameters")
    if not isinstance(parameters, dict):
        return {}
    return {
        name: metadata
        for name, metadata in parameters.items()
        if isinstance(name, str) and isinstance(metadata, dict)
    }


def _public_parameter(metadata: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "node_id",
        "field",
        "type",
        "required",
        "description",
        "default",
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "format",
    )
    return {field: metadata[field] for field in fields if field in metadata}


def _complete_output_contract(outputs: list[dict[str, Any]]) -> dict[str, Any]:
    return {"version": 1, "coverage": "complete", "outputs": outputs}


def _stored_output_contract(schema: dict[str, Any]) -> dict[str, Any]:
    contract = schema.get("_output_contract")
    if contract is None:
        return {"version": 0, "coverage": "unknown", "outputs": []}
    if isinstance(contract, list):
        contract = _complete_output_contract(contract)
    if not isinstance(contract, dict):
        raise ValueError("stored output contract is invalid")
    outputs = contract.get("outputs")
    coverage = contract.get("coverage")
    if coverage not in {"complete", "unknown"} or not isinstance(outputs, list):
        raise ValueError("stored output contract is invalid")
    if not all(isinstance(item, dict) for item in outputs):
        raise ValueError("stored output contract is invalid")
    return json.loads(json.dumps(contract, ensure_ascii=False, allow_nan=False))


def _apply_exposures(
    parameters: dict[str, dict[str, Any]],
    operations: tuple[dict[str, Any], ...],
    graph: dict[str, Any],
    object_info: dict[str, Any],
) -> None:
    for operation in operations:
        if operation.get("op") != "expose_parameter":
            continue
        node_id = str(operation["node_id"])
        field = str(operation["field"])
        name = str(operation["name"])
        node = graph.get(node_id)
        class_type = str(node.get("class_type", "")) if isinstance(node, dict) else ""
        inputs = node.get("inputs") if isinstance(node, dict) else None
        current = inputs.get(field) if isinstance(inputs, dict) else None
        declared = _declared_parameter(object_info.get(class_type), field, current)
        matches = [
            (candidate_name, metadata)
            for candidate_name, metadata in parameters.items()
            if metadata.get("node_id") == node_id and metadata.get("field") == field
        ]
        collision = parameters.get(name)
        if collision is not None and not any(
            candidate_name == name for candidate_name, _ in matches
        ):
            raise ValueError(f'Workflow parameter name "{name}" targets another input')
        metadata = dict(matches[0][1]) if matches else {}
        metadata.update(declared)
        metadata.update({"node_id": node_id, "field": field})
        for candidate_name, _ in matches:
            del parameters[candidate_name]
        metadata["required"] = bool(operation.get("required", False))
        parameters[name] = metadata
    targets: dict[tuple[str, str], str] = {}
    for name, metadata in parameters.items():
        target = (str(metadata.get("node_id", "")), str(metadata.get("field", "")))
        previous = targets.setdefault(target, name)
        if target != ("", "") and previous != name:
            raise ValueError(f'Workflow parameters "{previous}" and "{name}" target the same input')


def _declared_parameter(info: object, field: str, current: object) -> dict[str, Any]:
    if not isinstance(info, dict):
        raise ValueError(f'Input "{field}" has no ComfyUI object_info metadata')
    inputs = info.get("input")
    definition: object = None
    if isinstance(inputs, dict):
        for section in ("required", "optional"):
            values = inputs.get(section)
            if isinstance(values, dict) and field in values:
                definition = values[field]
                break
    if not isinstance(definition, list) or not definition:
        raise ValueError(f'Input "{field}" has no ComfyUI object_info metadata')
    declared = definition[0]
    settings = definition[1] if len(definition) > 1 and isinstance(definition[1], dict) else {}
    type_map = {
        "INT": "int",
        "FLOAT": "float",
        "BOOLEAN": "boolean",
        "STRING": "string",
        "IMAGE": "image",
        "AUDIO": "audio",
        "VIDEO": "video",
    }
    if isinstance(declared, str):
        parameter_type = type_map.get(declared.upper(), "string")
    elif isinstance(declared, list):
        parameter_type = _type_guess(declared[0]) if declared else "string"
    else:
        raise ValueError(f'Input "{field}" has unsupported ComfyUI object_info metadata')
    metadata: dict[str, Any] = {"type": parameter_type, "default": current}
    for source, target in (("min", "minimum"), ("max", "maximum")):
        value = settings.get(source)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metadata[target] = value
    options = declared if isinstance(declared, list) else settings.get("options")
    if isinstance(options, list) and len(options) <= 200:
        metadata["enum"] = list(options)
    description = settings.get("tooltip", settings.get("description"))
    if isinstance(description, str) and description:
        metadata["description"] = description
    return metadata


def _validate_acyclic(graph: dict[str, Any]) -> None:
    outgoing: dict[str, set[str]] = {str(node_id): set() for node_id in graph}
    indegree = {node_id: 0 for node_id in outgoing}
    for target_id, node in graph.items():
        inputs = node.get("inputs", {}) if isinstance(node, dict) else {}
        if not isinstance(inputs, dict):
            continue
        for value in inputs.values():
            if not _is_connection(value):
                continue
            source_id = str(value[0])
            target = str(target_id)
            if source_id == target:
                raise ValueError("Workflow graph contains a cycle")
            if source_id in outgoing and target not in outgoing[source_id]:
                outgoing[source_id].add(target)
                indegree[target] += 1
    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        source = ready.pop()
        visited += 1
        for target in outgoing[source]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if visited != len(indegree):
        raise ValueError("Workflow graph contains a cycle")


def _operation_string(operation: dict[str, Any], field: str, index: int) -> str:
    value = operation.get(field)
    if not isinstance(value, str) or not value or len(value) > 256:
        raise TypeError(f"operations[{index}].{field} must be a bounded non-empty string")
    return value


def _required_string(value: dict[str, Any], field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result:
        raise ValueError(f"{field} is missing")
    return result


def _json_copy(value: object, field: str) -> dict[str, Any]:
    try:
        copied = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be finite JSON") from exc
    if not isinstance(copied, dict):
        raise ValueError(f"{field} must be an object")
    return copied


def _revision_digest(
    graph: dict[str, Any], schema: dict[str, Any], dependencies: dict[str, Any]
) -> str:
    value = {
        "identity_version": 2,
        "graph": graph,
        "parameters": schema.get("parameters", {}),
        "dependencies": dependencies,
        "output_contract": schema.get("_output_contract"),
    }
    metadata = schema.get("_revision")
    if isinstance(metadata, dict) and metadata:
        value["revision_metadata"] = metadata
    return _sha256(value)


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _type_guess(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "string"


def _is_connection(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], (str, int))
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    )


def _node_sort_key(value: object) -> tuple[int, int | str]:
    text = str(value)
    return (0, int(text)) if text.isdigit() else (1, text)


_MISSING = object()
