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
from comfyui_mcp_skills.application.workflow_recipes import (
    RecipeError,
    apply_recipe,
    declared_parameter,
)
from comfyui_mcp_skills.domain.errors import (
    WorkflowChangeNotFound,
    WorkflowChangeValidationError,
)
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
    def commit_change_plan(self, plan_id: str, plan_digest: str, actor: str) -> dict[str, Any]: ...
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
            encoded_operations = json.dumps(
                operations, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode()
        except (TypeError, ValueError) as exc:
            raise ValueError("operations must contain finite JSON") from exc
        if len(encoded_operations) > 1024 * 1024:
            raise ValueError("operations exceed 1 MiB")
        _validate_json_depth(operations, maximum=32)
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
            _apply_operation(
                graph, parameter_schema, operation, object_info=object_info, index=index
            )
            for index, operation in enumerate(operations)
        )
        _validate_acyclic(graph)
        validation = self._validation.validate_api(graph, object_info)
        if not validation["valid"]:
            issues = validation["issues"][:10]
            raise WorkflowChangeValidationError(
                _change_validation_error(issues, graph),
                details={
                    "issues": _issue_summaries(issues),
                    "suggested_queries": _suggested_queries(issues, graph, server_id),
                },
            )
        after_semantic = self._graphs.describe(graph, object_info=object_info)
        generated_parameters = after_semantic["parameters"]
        explicit = parameter_schema.get("parameters")
        explicit = explicit if isinstance(explicit, dict) else {}
        explicit_targets = {
            (str(metadata.get("node_id", "")), str(metadata.get("field", "")))
            for metadata in explicit.values()
            if isinstance(metadata, dict)
        }
        parameters = {
            name: metadata
            for name, metadata in generated_parameters.items()
            if (str(metadata.get("node_id", "")), str(metadata.get("field", "")))
            not in explicit_targets
        }
        parameters.update(explicit)
        _apply_exposures(parameters, normalized_operations, graph, object_info)
        parameter_schema["parameters"] = normalize_parameters({"parameters": parameters})
        validate_parameter_targets(parameter_schema["parameters"], graph)
        build_input_schema(parameter_schema["parameters"])
        parameter_schema["_output_contract"] = _complete_output_contract(after_semantic["outputs"])
        base_dependencies = _json_copy(base.get("dependency_contract"), "base dependencies")
        trusted_seconds = base_dependencies.get("trusted_seconds_per_run")
        if (
            isinstance(trusted_seconds, bool)
            or not isinstance(trusted_seconds, (int, float))
            or float(trusted_seconds) <= 0
        ):
            raise ValueError("Published Workflow has no trusted runtime estimate")
        dependencies = dict(after_semantic["dependencies"])
        dependencies["output_cardinality"] = len(after_semantic["outputs"])
        dependencies["trusted_seconds_per_run"] = float(trusted_seconds)
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
            include_mermaid=False,
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
        return self._repository.commit_change_plan(plan_id, plan_digest, self._actor)

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


def _change_validation_error(issues: list[dict[str, str]], graph: dict[str, Any]) -> str:
    """Format validation issues with node/field location and a repair hint.

    The hint steers agents to the node catalog tool instead of guessing
    class types or enum values from memory (see FEATURE_REQUESTS P0-3).
    Known classes carry the concrete class_type (node.describe requires it);
    unknown classes point at node.list since describe would fail on them.
    """
    parts: list[str] = []
    for issue in issues:
        code = str(issue.get("code", "invalid"))
        message = str(issue.get("message", ""))
        node_id = issue.get("node_id", "")
        field = issue.get("field", "")
        location = f"node {node_id}" if node_id else "graph"
        if field:
            location = f"{location} field {field}"
        parts.append(f"{location} [{code}]: {message}")
        if code == "unknown_node_type":
            parts.append(
                "hint: 该节点类型不存在于服务器，用 comfyui.node.list 搜索可用节点类型"
            )
        elif code in {
            "missing_required_input",
            "invalid_enum_value",
            "invalid_input",
            "unknown_input",
            "input_type_mismatch",
            "input_out_of_range",
        }:
            class_type = ""
            if node_id:
                node = graph.get(node_id)
                if isinstance(node, dict):
                    raw_class_type = node.get("class_type")
                    if isinstance(raw_class_type, str):
                        class_type = raw_class_type.strip()
            if class_type:
                parts.append(
                    f"hint: 用 comfyui.node.describe {class_type} "
                    "查看该节点的输入签名与枚举值"
                )
            else:
                parts.append(
                    "hint: 用 comfyui.node.list 搜索节点类型后用 "
                    "comfyui.node.describe 查看输入签名与枚举值"
                )
    return "Workflow change is invalid: " + "; ".join(parts)


def _issue_summaries(issues: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Bounded, transport-safe projection of validation issues."""
    return [
        {
            key: str(issue.get(key, ""))
            for key in ("code", "message", "node_id", "field")
            if issue.get(key) not in (None, "")
        }
        for issue in issues
    ]


def _suggested_queries(
    issues: list[dict[str, Any]], graph: dict[str, Any], server_id: str
) -> list[dict[str, Any]]:
    """Executable follow-up queries for each validation issue.

    Every issue yields at least one suggestion so agents can repair the
    workflow without parsing message text (FEATURE_REQUESTS P0-3). Arguments
    always carry the server_id and stay within the tool schemas (blueprint
    query is capped at its 256-char maxLength).
    """
    suggestions: list[dict[str, Any]] = []
    for issue in issues:
        code = str(issue.get("code", ""))
        node_id = issue.get("node_id", "")
        field = issue.get("field", "")
        if code == "unknown_node_type":
            class_type = _issue_class_type(issue, graph)
            query = (class_type or "").strip()[:256]
            if query:
                suggestions.append(
                    {
                        "tool": "comfyui.node.blueprint",
                        "arguments": {"server_id": server_id, "query": query},
                    }
                )
            suggestions.append(
                {"tool": "comfyui.node.list", "arguments": {"server_id": server_id}}
            )
        elif code == "output_port_out_of_range":
            source = _connection_source(graph, node_id, field)
            if source:
                suggestions.append(
                    {
                        "tool": "comfyui.node.describe",
                        "arguments": {
                            "server_id": server_id,
                            "node_class": source,
                        },
                    }
                )
            else:
                suggestions.append(
                    {"tool": "comfyui.node.list", "arguments": {"server_id": server_id}}
                )
        elif code in {
            "missing_required_input",
            "invalid_input_name",
            "input_too_large",
            "unknown_input_port",
            "invalid_connection",
            "missing_source_node",
            "port_type_mismatch",
            "invalid_enum_value",
            "invalid_input_type",
            "input_out_of_range",
            "unsafe_media_path",
        }:
            class_type = _issue_class_type(issue, graph)
            if class_type:
                suggestions.append(
                    {
                        "tool": "comfyui.node.describe",
                        "arguments": {
                            "server_id": server_id,
                            "node_class": class_type,
                        },
                    }
                )
            else:
                suggestions.append(
                    {"tool": "comfyui.node.list", "arguments": {"server_id": server_id}}
                )
        else:
            suggestions.append(
                {"tool": "comfyui.node.list", "arguments": {"server_id": server_id}}
            )
    return suggestions


def _issue_class_type(
    issue: dict[str, Any], graph: dict[str, Any]
) -> str:
    """Class type of the node named by the issue, if it can be located."""
    node_id = issue.get("node_id", "")
    node = graph.get(str(node_id))
    if isinstance(node, dict):
        raw_class_type = node.get("class_type")
        if isinstance(raw_class_type, str):
            return raw_class_type.strip()
    return ""


def _connection_source(
    graph: dict[str, Any], node_id: Any, field: Any
) -> str:
    """Class type of the source node feeding graph[node_id].inputs[field].

    output_port_out_of_range issues are reported on the consuming node, but
    the offending port belongs to the connection source, so the repair hint
    must point there (see workflow_graph validation).
    """
    node = graph.get(str(node_id))
    if not isinstance(node, dict):
        return ""
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        return ""
    connection = inputs.get(str(field))
    if (
        isinstance(connection, list)
        and len(connection) >= 1
        and isinstance(connection[0], (str, int))
        and not isinstance(connection[0], bool)
    ):
        return _issue_class_type({"node_id": str(connection[0])}, graph)
    return ""


def _apply_operation(
    graph: dict[str, Any],
    parameter_schema: dict[str, Any],
    operation: dict[str, Any],
    *,
    object_info: dict[str, Any] | None = None,
    index: int,
) -> dict[str, Any]:
    if not isinstance(operation, dict):
        raise TypeError(f"operations[{index}] must be an object")
    copied = _json_copy(operation, f"operations[{index}]")
    kind = copied.get("op")
    allowed_by_kind = {
        "add_node": {"op", "node_id", "class_type", "inputs"},
        "remove_node": {"op", "node_id"},
        "replace_node": {"op", "node_id", "class_type", "inputs"},
        "set_input": {"op", "node_id", "field", "value"},
        "connect": {"op", "source_node_id", "source_output", "target_node_id", "target_input"},
        "disconnect": {"op", "node_id", "field"},
        "expose_parameter": {"op", "node_id", "field", "name", "required"},
        "insert_subgraph": {"op", "id_prefix", "nodes", "subgraph"},
        "extract_subgraph": {"op", "name", "node_ids"},
        "apply_recipe": {"op", "recipe_id", "arguments"},
    }
    if kind not in allowed_by_kind:
        raise ValueError(f"operations[{index}].op is unsupported")
    unexpected = set(copied) - allowed_by_kind[str(kind)]
    if unexpected:
        raise ValueError(
            f"operations[{index}] has unexpected fields: {', '.join(sorted(unexpected))}"
        )
    if kind == "add_node":
        node_id = _operation_string(copied, "node_id", index)
        if node_id in graph:
            raise ValueError(f"operations[{index}] node {node_id} already exists")
        graph[node_id] = _operation_node(copied, index)
    elif kind == "remove_node":
        node_id = _operation_string(copied, "node_id", index)
        _inputs(graph, node_id, index)
        for other_id, node in graph.items():
            if other_id != node_id and _node_references(node, node_id):
                raise ValueError(f"operations[{index}] node {node_id} is still connected")
        del graph[node_id]
        _remove_node_parameters(parameter_schema, node_id)
    elif kind == "replace_node":
        node_id = _operation_string(copied, "node_id", index)
        _inputs(graph, node_id, index)
        graph[node_id] = _operation_node(copied, index)
        _remove_node_parameters(parameter_schema, node_id)
    elif kind == "insert_subgraph":
        _insert_subgraph(graph, parameter_schema, copied, index)
    elif kind == "extract_subgraph":
        name = validate_identifier(_operation_string(copied, "name", index), field="subgraph_name")
        node_ids = copied.get("node_ids")
        if not isinstance(node_ids, list) or not node_ids or len(node_ids) > 100:
            raise ValueError(f"operations[{index}].node_ids must contain between 1 and 100 IDs")
        if any(not isinstance(node_id, str) or node_id not in graph for node_id in node_ids):
            raise ValueError(f"operations[{index}].node_ids references a missing node")
        revision_metadata = parameter_schema.setdefault("_revision", {})
        if not isinstance(revision_metadata, dict):
            raise ValueError("stored revision metadata is invalid")
        subgraphs = revision_metadata.setdefault("extracted_subgraphs", {})
        if not isinstance(subgraphs, dict):
            raise ValueError("stored subgraph catalog is invalid")
        if name in subgraphs:
            raise ValueError(f"operations[{index}] subgraph {name} already exists")
        subgraphs[name] = _extracted_definition(graph, node_ids)
    elif kind == "apply_recipe":
        recipe_id = _operation_string(copied, "recipe_id", index)
        try:
            apply_recipe(
                graph,
                parameter_schema,
                recipe_id,
                copied.get("arguments"),
                object_info,
                index=index,
            )
        except RecipeError as exc:
            raise ValueError(str(exc)) from exc
    elif kind == "connect":
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
            if _is_connection(value):
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


def _operation_node(operation: dict[str, Any], index: int) -> dict[str, Any]:
    class_type = validate_identifier(
        _operation_string(operation, "class_type", index), field="class_type"
    )
    inputs = operation.get("inputs")
    if not isinstance(inputs, dict) or len(inputs) > 256:
        raise ValueError(f"operations[{index}].inputs must be a bounded object")
    return {"class_type": class_type, "inputs": _json_copy(inputs, "node inputs")}


def _node_references(node: object, source_id: str) -> bool:
    if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
        return False
    return any(
        _is_connection(value) and str(value[0]) == source_id for value in node["inputs"].values()
    )


def _remove_node_parameters(schema: dict[str, Any], node_id: str) -> None:
    parameters = schema.get("parameters")
    if not isinstance(parameters, dict):
        return
    for name in list(parameters):
        metadata = parameters[name]
        if isinstance(metadata, dict) and str(metadata.get("node_id", "")) == node_id:
            del parameters[name]


def _insert_subgraph(
    graph: dict[str, Any],
    parameter_schema: dict[str, Any],
    operation: dict[str, Any],
    index: int,
) -> None:
    prefix = validate_identifier(
        _operation_string(operation, "id_prefix", index), field="subgraph_prefix"
    )
    has_nodes = "nodes" in operation
    has_subgraph = "subgraph" in operation
    if has_nodes == has_subgraph:
        raise ValueError(
            f"operations[{index}] requires exactly one of 'nodes' or 'subgraph'"
        )
    if has_subgraph:
        name = validate_identifier(
            _operation_string(operation, "subgraph", index), field="subgraph_name"
        )
        revision_metadata = parameter_schema.get("_revision")
        if not isinstance(revision_metadata, dict):
            raise ValueError("stored revision metadata is invalid")
        subgraphs = revision_metadata.get("extracted_subgraphs")
        if not isinstance(subgraphs, dict) or name not in subgraphs:
            raise ValueError(f"operations[{index}] subgraph {name} is not extracted")
        definition = subgraphs[name]
        if not isinstance(definition, dict):
            raise ValueError(f"operations[{index}] subgraph {name} definition is invalid")
        nodes = definition.get("nodes")
        boundary_inputs = definition.get("boundary_inputs")
        boundary_outputs = definition.get("boundary_outputs")
        if not isinstance(nodes, dict) or not nodes or len(nodes) > 100:
            raise ValueError(
                f"operations[{index}] subgraph {name} definition is invalid"
            )
        if boundary_inputs is not None and not isinstance(boundary_inputs, dict):
            raise ValueError(
                f"operations[{index}] subgraph {name} definition is invalid"
            )
        if boundary_outputs is not None and not isinstance(boundary_outputs, list):
            raise ValueError(
                f"operations[{index}] subgraph {name} definition is invalid"
            )
    else:
        nodes = operation.get("nodes")
        boundary_inputs = None
    if not isinstance(nodes, dict) or not nodes or len(nodes) > 100:
        raise ValueError(f"operations[{index}].nodes must contain between 1 and 100 nodes")
    mapping = {
        str(node_id): validate_identifier(f"{prefix}_{node_id}", field="subgraph_node_id")
        for node_id in nodes
    }
    if any(node_id in graph for node_id in mapping.values()):
        raise ValueError(f"operations[{index}] subgraph node ID collides with the graph")
    for source_id, raw_node in nodes.items():
        if not isinstance(raw_node, dict):
            raise ValueError(f"operations[{index}].nodes contains an invalid node")
        node = _operation_node(
            {
                "class_type": raw_node.get("class_type"),
                "inputs": raw_node.get("inputs"),
            },
            index,
        )
        for field, value in list(node["inputs"].items()):
            if _is_connection(value) and str(value[0]) in mapping:
                node["inputs"][field] = [mapping[str(value[0])], value[1]]
        graph[mapping[str(source_id)]] = node
    if boundary_inputs:
        for key in boundary_inputs:
            node_id, separator, field = key.partition(".")
            if not separator:
                continue
            target = mapping.get(node_id)
            if target is None:
                continue
            inputs = graph[target].get("inputs")
            if not isinstance(inputs, dict) or not _is_connection(inputs.get(field)):
                continue
            del inputs[field]


def _extracted_definition(graph: dict[str, Any], node_ids: list[str]) -> dict[str, Any]:
    """Snapshot selected nodes with boundary contracts for later by-name reuse.

    ``boundary_inputs`` records connection inputs sourced outside the selection
    (keyed ``node_id.field``); ``boundary_outputs`` records consumers outside the
    selection. By-name instantiation disconnects boundary inputs so the reusable
    unit never carries stale external references.
    """
    selected = set(node_ids)
    boundary_inputs: dict[str, Any] = {}
    for node_id in node_ids:
        inputs = graph[node_id].get("inputs")
        if not isinstance(inputs, dict):
            continue
        for field, value in inputs.items():
            if _is_connection(value) and str(value[0]) not in selected:
                boundary_inputs[f"{node_id}.{field}"] = {
                    "source_node_id": str(value[0]),
                    "source_output": value[1],
                }
    boundary_outputs: list[dict[str, Any]] = []
    for other_id, node in graph.items():
        if str(other_id) in selected or not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for field, value in inputs.items():
            if _is_connection(value) and str(value[0]) in selected:
                boundary_outputs.append(
                    {
                        "node_id": str(value[0]),
                        "source_output": value[1],
                        "target_node_id": str(other_id),
                        "target_field": field,
                    }
                )
    boundary_outputs.sort(
        key=lambda item: (
            item["node_id"],
            item["target_node_id"],
            item["target_field"],
        )
    )
    return {
        "nodes": {node_id: _json_copy(graph[node_id], "subgraph node") for node_id in node_ids},
        "boundary_inputs": boundary_inputs,
        "boundary_outputs": boundary_outputs,
    }


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


_MAX_MERMAID_NODES = 50
_MERMAID_LABEL_MAX = 64


def _mermaid_label(value: object) -> str:
    """One safe Mermaid node label: stripped, bounded, quotes neutralized."""
    label = str(value).strip()
    if len(label) > _MERMAID_LABEL_MAX:
        label = label[:_MERMAID_LABEL_MAX - 1] + "…"
    return label.replace('"', "'").replace("\\", "/")


def _graph_mermaid(
    graph: dict[str, Any],
    *,
    highlight_nodes: set[object] | None = None,
) -> str:
    """Render one workflow graph as a bounded Mermaid flowchart.

    Node ids are aliased (N1, N2, ...) in stable order so arbitrary engine
    node ids cannot corrupt the diagram; edges come from ``["<id>", index]``
    input references. Graphs beyond the node limit fail loudly instead of
    silently truncating the diagram.
    """
    if not isinstance(graph, dict) or not graph:
        raise ValueError("workflow graph must be a non-empty object")
    if len(graph) > _MAX_MERMAID_NODES:
        raise ValueError(
            f"workflow graph exceeds the visualization limit of {_MAX_MERMAID_NODES} nodes"
        )
    ordered = sorted(graph, key=_node_sort_key)
    alias_by_id: dict[object, str] = {
        node_id: f"N{index + 1}" for index, node_id in enumerate(ordered)
    }
    highlights = {str(node_id) for node_id in (highlight_nodes or set())}
    lines = ["flowchart LR"]
    if highlights:
        lines.append("    classDef added fill:#d4edda,stroke:#28a745;")
    for node_id in ordered:
        node = graph[node_id]
        class_type = ""
        if isinstance(node, dict):
            raw_class_type = node.get("class_type")
            if isinstance(raw_class_type, str):
                class_type = raw_class_type
        label = _mermaid_label(class_type or node_id)
        alias = alias_by_id[node_id]
        if str(node_id) in highlights:
            lines.append(f'    {alias}["{label}"]:::added')
        else:
            lines.append(f'    {alias}["{label}"]')
    for node_id in ordered:
        node = graph[node_id]
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for raw_value in inputs.values():
            if (
                isinstance(raw_value, list)
                and raw_value
                and isinstance(raw_value[0], (str, int))
                and raw_value[0] in alias_by_id
            ):
                lines.append(
                    f"    {alias_by_id[raw_value[0]]} --> {alias_by_id[node_id]}"
                )
    return "\n".join(lines) + "\n"


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
    include_mermaid: bool = True,
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
        "subgraphs_added": sorted(
            set(_subgraph_catalog(schema_after)) - set(_subgraph_catalog(schema_before))
        ),
        "subgraphs_removed": sorted(
            set(_subgraph_catalog(schema_before)) - set(_subgraph_catalog(schema_after))
        ),
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
    if include_mermaid:
        result["mermaid"] = _graph_mermaid(
            graph_after,
            highlight_nodes=set(after_nodes - before_nodes),
        )
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


def _subgraph_catalog(schema: dict[str, Any]) -> dict[str, Any]:
    revision_metadata = schema.get("_revision")
    if not isinstance(revision_metadata, dict):
        return {}
    subgraphs = revision_metadata.get("extracted_subgraphs")
    return subgraphs if isinstance(subgraphs, dict) else {}


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


# declared_parameter lives in workflow_recipes (shared with recipe exposure);
# keep the private alias for the existing expose_parameter flow.
_declared_parameter = declared_parameter


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


def _validate_json_depth(value: object, *, maximum: int) -> None:
    stack = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > maximum:
            raise ValueError(f"operations JSON depth exceeds {maximum}")
        if isinstance(current, dict):
            if len(current) > 1024:
                raise ValueError("operations object exceeds 1024 fields")
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            if len(current) > 10_000:
                raise ValueError("operations array exceeds 10000 items")
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, str) and len(current) > 256 * 1024:
            raise ValueError("operations string exceeds 256 KiB")


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
