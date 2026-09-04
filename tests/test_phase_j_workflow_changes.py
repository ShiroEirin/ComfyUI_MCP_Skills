"""Phase J graph change, diff, publish, and rollback contracts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from mcp.client import Client

from comfyui_mcp_skills.adapters.mcp.admin import create_admin_server
from comfyui_mcp_skills.adapters.mcp.server import create_server
from comfyui_mcp_skills.application.authorization import (
    AuthorizationContext,
    Scope,
    Toolset,
)
from comfyui_mcp_skills.application.workflow_change import WorkflowChangeService
from comfyui_mcp_skills.application.workflow_graph import (
    WorkflowGraphService,
    WorkflowValidationService,
)
from comfyui_mcp_skills.application.workflow_import import WorkflowImportService
from comfyui_mcp_skills.domain.errors import (
    WorkflowChangeConflict,
    WorkflowChangeNotFound,
    WorkflowChangeValidationError,
)
from comfyui_mcp_skills.domain.workflow_semantics import (
    DependencyExtractorRegistry,
    ParameterRoleRegistry,
)
from comfyui_mcp_skills.infrastructure.persistence.assets import FileAssetRepository
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.repository_factory import RepositoryBundle
from comfyui_mcp_skills.infrastructure.persistence.runs import FileRunRepository
from comfyui_mcp_skills.infrastructure.persistence.sqlite_workflows import SQLiteWorkflowRepository
from comfyui_mcp_skills.infrastructure.persistence.workflow_changes import (
    SQLiteWorkflowChangeRepository,
)

OBJECT_INFO: dict[str, Any] = {
    "Text": {
        "input": {"required": {"text": ["STRING"]}},
        "input_order": {"required": ["text"]},
        "output": ["STRING"],
    },
    "Image": {"input": {"required": {}}, "output": ["IMAGE"]},
    "SaveImage": {
        "input": {
            "required": {
                "images": ["IMAGE"],
                "filename_prefix": ["STRING"],
            }
        },
        "input_order": {"required": ["images", "filename_prefix"]},
        "output": [],
        "output_node": True,
    },
    "KSampler": {
        "input": {"required": {"cfg": ["FLOAT", {"min": 0.0, "max": 20.0, "tooltip": "Guidance"}]}},
        "input_order": {"required": ["cfg"]},
        "output": ["LATENT"],
    },
}
BASE_GRAPH = {
    "1": {"class_type": "Text", "inputs": {"text": "before"}},
    "2": {"class_type": "Image", "inputs": {}},
    "3": {
        "class_type": "SaveImage",
        "inputs": {"images": ["2", 0], "filename_prefix": "result"},
    },
    "4": {"class_type": "KSampler", "inputs": {"cfg": 7}},
}


def _services(tmp_path: Path) -> tuple[WorkflowChangeService, SQLiteWorkflowRepository]:
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()
    workflows = SQLiteWorkflowRepository(store)
    graphs = WorkflowGraphService(
        ParameterRoleRegistry.default(), DependencyExtractorRegistry.default()
    )
    validation = WorkflowValidationService()
    imported = WorkflowImportService(graphs, validation, workflows).preview(
        BASE_GRAPH,
        workflow_id="portrait",
        server_id="local",
        object_info=OBJECT_INFO,
    )
    created = WorkflowImportService(graphs, validation, workflows).commit(imported)
    workflows.publish(created["deployment_id"])
    changes = WorkflowChangeService(
        SQLiteWorkflowChangeRepository(store), graphs, validation, actor="phase-j-test"
    )
    return changes, workflows


def test_change_plan_commits_structured_diff_without_raw_graph(tmp_path: Path) -> None:
    changes, workflows = _services(tmp_path)

    plan = changes.plan(
        "portrait",
        "local",
        [{"op": "set_input", "node_id": "1", "field": "text", "value": "after"}],
        object_info=OBJECT_INFO,
    )

    assert "graph" not in plan
    assert plan["diff"]["input_changes"] == [
        {"node_id": "1", "field": "text", "before": "before", "after": "after"}
    ]
    committed = changes.commit(plan["plan_id"], plan["plan_digest"])
    repeated = changes.commit(plan["plan_id"], plan["plan_digest"])
    assert repeated["revision_id"] == committed["revision_id"]
    assert committed["published"] is False
    assert workflows.get("local", "portrait").graph["1"]["inputs"]["text"] == "before"


def test_replanning_same_change_creates_fresh_expiring_plan(tmp_path: Path) -> None:
    changes, _workflows = _services(tmp_path)
    operation = [{"op": "set_input", "node_id": "1", "field": "text", "value": "after"}]

    first = changes.plan("portrait", "local", operation, object_info=OBJECT_INFO)
    second = changes.plan("portrait", "local", operation, object_info=OBJECT_INFO)

    assert first["plan_id"] != second["plan_id"]
    assert first["expires_at"] != second["expires_at"]


def test_change_plan_supports_node_lifecycle_operations(tmp_path: Path) -> None:
    changes, _workflows = _services(tmp_path)

    added = changes.plan(
        "portrait",
        "local",
        [{"op": "add_node", "node_id": "5", "class_type": "Text", "inputs": {"text": "new"}}],
        object_info=OBJECT_INFO,
    )
    assert added["diff"]["nodes_added"] == ["5"]

    replaced = changes.plan(
        "portrait",
        "local",
        [
            {
                "op": "replace_node",
                "node_id": "4",
                "class_type": "Text",
                "inputs": {"text": "replacement"},
            }
        ],
        object_info=OBJECT_INFO,
    )
    assert replaced["diff"]["input_changes"]

    removed = changes.plan(
        "portrait",
        "local",
        [{"op": "remove_node", "node_id": "4"}],
        object_info=OBJECT_INFO,
    )
    assert removed["diff"]["nodes_removed"] == ["4"]
    with pytest.raises(ValueError, match="still connected"):
        changes.plan(
            "portrait",
            "local",
            [{"op": "remove_node", "node_id": "2"}],
            object_info=OBJECT_INFO,
        )


def test_change_plan_supports_bounded_subgraphs_and_registered_recipes(tmp_path: Path) -> None:
    changes, _workflows = _services(tmp_path)

    inserted = changes.plan(
        "portrait",
        "local",
        [
            {
                "op": "insert_subgraph",
                "id_prefix": "sg",
                "nodes": {
                    "a": {"class_type": "Image", "inputs": {}},
                    "b": {
                        "class_type": "SaveImage",
                        "inputs": {"images": ["a", 0], "filename_prefix": "subgraph"},
                    },
                },
            }
        ],
        object_info=OBJECT_INFO,
    )
    assert inserted["diff"]["nodes_added"] == ["sg_a", "sg_b"]

    extracted = changes.plan(
        "portrait",
        "local",
        [{"op": "extract_subgraph", "name": "base_output", "node_ids": ["2", "3"]}],
        object_info=OBJECT_INFO,
    )
    assert extracted["diff"]["parameter_schema_changed"] is True

    recipe = changes.plan(
        "portrait",
        "local",
        [
            {
                "op": "apply_recipe",
                "recipe_id": "set_scalar_input.v1",
                "arguments": {"node_id": "1", "field": "text", "value": "recipe"},
            }
        ],
        object_info=OBJECT_INFO,
    )
    assert recipe["diff"]["input_changes"][0]["after"] == "recipe"
    with pytest.raises(ValueError, match="not registered"):
        changes.plan(
            "portrait",
            "local",
            [{"op": "apply_recipe", "recipe_id": "unknown.v1", "arguments": {}}],
            object_info=OBJECT_INFO,
        )


def test_change_plan_commit_is_actor_bound_and_operations_are_bounded(tmp_path: Path) -> None:
    changes, _workflows = _services(tmp_path)
    plan = changes.plan(
        "portrait",
        "local",
        [{"op": "set_input", "node_id": "1", "field": "text", "value": "owned"}],
        object_info=OBJECT_INFO,
    )
    other = WorkflowChangeService(
        changes._repository,  # type: ignore[attr-defined]
        changes._graphs,  # type: ignore[attr-defined]
        changes._validation,  # type: ignore[attr-defined]
        actor="other-actor",
    )
    with pytest.raises(WorkflowChangeNotFound, match="not found"):
        other.commit(plan["plan_id"], plan["plan_digest"])
    with pytest.raises(ValueError, match="1 MiB"):
        changes.plan(
            "portrait",
            "local",
            [
                {
                    "op": "set_input",
                    "node_id": "1",
                    "field": "text",
                    "value": "x" * (1024 * 1024),
                }
            ],
            object_info=OBJECT_INFO,
        )


def test_change_plan_rejects_graph_cycle(tmp_path: Path) -> None:
    changes, _workflows = _services(tmp_path)

    with pytest.raises(ValueError, match="contains a cycle"):
        changes.plan(
            "portrait",
            "local",
            [
                {
                    "op": "connect",
                    "source_node_id": "1",
                    "source_output": 0,
                    "target_node_id": "1",
                    "target_input": "text",
                }
            ],
            object_info=OBJECT_INFO,
        )


def test_expose_parameter_rejects_name_bound_to_another_input(tmp_path: Path) -> None:
    changes, _workflows = _services(tmp_path)

    with pytest.raises(ValueError, match="targets another input"):
        changes.plan(
            "portrait",
            "local",
            [
                {
                    "op": "expose_parameter",
                    "node_id": "3",
                    "field": "filename_prefix",
                    "name": "prompt",
                    "required": True,
                }
            ],
            object_info=OBJECT_INFO,
        )


def test_expose_parameter_uses_declared_object_info_type_and_constraints(
    tmp_path: Path,
) -> None:
    changes, workflows = _services(tmp_path)
    plan = changes.plan(
        "portrait",
        "local",
        [
            {
                "op": "expose_parameter",
                "node_id": "4",
                "field": "cfg",
                "name": "guidance",
                "required": True,
            }
        ],
        object_info=OBJECT_INFO,
    )
    committed = changes.commit(plan["plan_id"], plan["plan_digest"])
    changes.publish(committed["deployment_id"])

    workflow = workflows.get("local", "portrait")
    assert workflow is not None
    assert workflow.parameters["guidance"] == {
        "node_id": "4",
        "field": "cfg",
        "required": True,
        "type": "float",
        "default": 7,
        "minimum": 0.0,
        "maximum": 20.0,
        "description": "Guidance",
    }


def test_change_plan_rejects_illegal_connection_with_port_types(tmp_path: Path) -> None:
    changes, _workflows = _services(tmp_path)

    with pytest.raises(
        WorkflowChangeValidationError, match="STRING is incompatible with IMAGE"
    ) as excinfo:
        changes.plan(
            "portrait",
            "local",
            [
                {
                    "op": "connect",
                    "source_node_id": "1",
                    "source_output": 0,
                    "target_node_id": "3",
                    "target_input": "images",
                }
            ],
            object_info=OBJECT_INFO,
        )

    details = excinfo.value.details
    assert details["suggested_queries"] == [
        {
            "tool": "comfyui.node.describe",
            "arguments": {"server_id": "local", "node_class": "SaveImage"},
        }
    ]


def test_change_plan_output_port_issue_hints_at_source_node(
    tmp_path: Path,
) -> None:
    """output_port_out_of_range is reported on the consuming node, so the
    suggested query must point at the connection source's class type."""
    changes, _workflows = _services(tmp_path)

    with pytest.raises(WorkflowChangeValidationError) as excinfo:
        changes.plan(
            "portrait",
            "local",
            [
                {
                    "op": "connect",
                    "source_node_id": "1",
                    "source_output": 3,
                    "target_node_id": "3",
                    "target_input": "images",
                }
            ],
            object_info=OBJECT_INFO,
        )

    details = excinfo.value.details
    assert details["issues"][0]["code"] == "output_port_out_of_range"
    assert details["suggested_queries"] == [
        {
            "tool": "comfyui.node.describe",
            "arguments": {"server_id": "local", "node_class": "Text"},
        }
    ]


def test_connection_source_accepts_integer_source_ids() -> None:
    """output_port_out_of_range hints resolve integer source node ids the
    same way the graph validator does (str | int connection sources)."""
    from comfyui_mcp_skills.application.workflow_change import _connection_source

    graph = {
        "1": {"class_type": "Text", "inputs": {"text": "before"}},
        "2": {"class_type": "Image", "inputs": {}},
        "3": {
            "class_type": "SaveImage",
            "inputs": {"images": [2, 5], "filename_prefix": "result"},
        },
    }
    assert _connection_source(graph, "3", "images") == "Image"
    assert _connection_source(graph, "3", "missing") == ""
    assert _connection_source(graph, "3", "filename_prefix") == ""

    from comfyui_mcp_skills.application.workflow_graph import _is_connection

    assert _is_connection([True, 0]) is False
    assert _is_connection([1, 0]) is True
    assert _is_connection([1, True]) is False


def test_change_commit_conflicts_when_published_base_changes(tmp_path: Path) -> None:
    changes, _workflows = _services(tmp_path)
    stale = changes.plan(
        "portrait",
        "local",
        [{"op": "set_input", "node_id": "1", "field": "text", "value": "stale"}],
        object_info=OBJECT_INFO,
    )
    winning = changes.plan(
        "portrait",
        "local",
        [{"op": "set_input", "node_id": "1", "field": "text", "value": "winner"}],
        object_info=OBJECT_INFO,
    )
    committed = changes.commit(winning["plan_id"], winning["plan_digest"])
    changes.publish(committed["deployment_id"])

    with pytest.raises(WorkflowChangeConflict, match="base Revision changed"):
        changes.commit(stale["plan_id"], stale["plan_digest"])


def test_publish_updates_schema_and_rollback_creates_new_revision(tmp_path: Path) -> None:
    changes, workflows = _services(tmp_path)
    base_revision_id = workflows.describe("portrait", "local")["revision_id"]
    plan = changes.plan(
        "portrait",
        "local",
        [
            {
                "op": "expose_parameter",
                "node_id": "3",
                "field": "filename_prefix",
                "name": "output_name",
                "required": True,
            }
        ],
        object_info=OBJECT_INFO,
    )
    committed = changes.commit(plan["plan_id"], plan["plan_digest"])
    changes.publish(committed["deployment_id"])

    published = workflows.get("local", "portrait")
    assert published is not None
    assert published.parameters["output_name"]["required"] is True
    revision_diff = changes.diff(base_revision_id, committed["revision_id"])
    assert "output_name" in revision_diff["parameters_after"]
    assert revision_diff["parameter_schema_changed"] is True

    rollback = changes.rollback(
        "portrait", "local", base_revision_id, request_id="rollback-request-1"
    )
    assert rollback["revision_id"] not in {base_revision_id, committed["revision_id"]}
    assert rollback["published"] is True
    assert workflows.describe("portrait", "local")["revision_id"] == rollback["revision_id"]
    assert len(workflows.list_revisions("portrait")) == 3
    repeated = changes.rollback(
        "portrait", "local", base_revision_id, request_id="rollback-request-1"
    )
    assert repeated["revision_id"] == rollback["revision_id"]
    assert len(workflows.list_revisions("portrait")) == 3


def test_commit_reuses_canonical_revision_when_content_returns(tmp_path: Path) -> None:
    changes, workflows = _services(tmp_path)
    base_revision_id = workflows.describe("portrait", "local")["revision_id"]
    changed = changes.plan(
        "portrait",
        "local",
        [{"op": "set_input", "node_id": "1", "field": "text", "value": "after"}],
        object_info=OBJECT_INFO,
    )
    changed_commit = changes.commit(changed["plan_id"], changed["plan_digest"])
    changes.publish(changed_commit["deployment_id"])

    restored = changes.plan(
        "portrait",
        "local",
        [{"op": "set_input", "node_id": "1", "field": "text", "value": "before"}],
        object_info=OBJECT_INFO,
    )
    restored_commit = changes.commit(restored["plan_id"], restored["plan_digest"])

    assert restored_commit["revision_id"] == base_revision_id


def test_legacy_revision_diff_marks_output_contract_unknown(tmp_path: Path) -> None:
    changes, workflows = _services(tmp_path)
    base_revision_id = workflows.describe("portrait", "local")["revision_id"]
    legacy_revision_id = "revision_" + ("b" * 64)
    with sqlite3.connect(tmp_path / "control-plane.sqlite3") as connection:
        row = connection.execute(
            """
            SELECT graph_json, parameter_schema_json, dependency_contract_json
            FROM workflow_revisions WHERE revision_id = ?
            """,
            (base_revision_id,),
        ).fetchone()
        assert row is not None
        schema = json.loads(row[1])
        del schema["_output_contract"]
        connection.execute(
            """
            INSERT INTO workflow_revisions(
                revision_id, workflow_id, graph_json, parameter_schema_json,
                dependency_contract_json, content_digest, created_at
            ) VALUES (?, 'portrait', ?, ?, ?, ?, '2026-01-01T00:00:00+00:00')
            """,
            (
                legacy_revision_id,
                row[0],
                json.dumps(schema, sort_keys=True, separators=(",", ":")),
                row[2],
                "b" * 64,
            ),
        )

    revision_diff = changes.diff(legacy_revision_id, base_revision_id)
    assert revision_diff["output_coverage_before"] == "unknown"
    assert revision_diff["output_coverage_after"] == "complete"


class _Gateway:
    def get_object_info(self) -> dict[str, Any]:
        return OBJECT_INFO


@pytest.mark.anyio
async def test_authoring_change_plan_commit_and_admin_publish_tools(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "default_server": "local",
                "servers": [{"id": "local", "url": "http://127.0.0.1:8188", "enabled": True}],
            }
        ),
        encoding="utf-8",
    )
    _changes, workflows = _services(tmp_path)
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    repositories = RepositoryBundle(
        workflows=workflows,
        runs=FileRunRepository(tmp_path),
        assets=FileAssetRepository(tmp_path),
        workflow_store="sqlite",
        run_store="file",
        asset_store="file",
        store=store,
    )
    authoring = create_server(
        tmp_path,
        repositories=repositories,
        gateway_factory=lambda _config: _Gateway(),
        authorization=AuthorizationContext(
            "author-j", frozenset({Scope.OBSERVE, Scope.AUTHOR}), Toolset.AUTHORING
        ),
    )
    admin = create_admin_server(
        tmp_path,
        enabled=True,
        repositories=repositories,
        gateway_factory=lambda _config: _Gateway(),
    )

    async with Client(authoring) as client:
        authoring_names = {tool.name for tool in (await client.list_tools()).tools}
        planned = await client.call_tool(
            "comfyui.admin.workflow.change.plan",
            {
                "workflow_id": "portrait",
                "server_id": "local",
                "operations": [
                    {
                        "op": "set_input",
                        "node_id": "1",
                        "field": "text",
                        "value": "through-mcp",
                    }
                ],
            },
        )
        committed = await client.call_tool(
            "comfyui.admin.workflow.change.commit",
            {
                "plan_id": planned.structured_content["plan_id"],
                "plan_digest": planned.structured_content["plan_digest"],
            },
        )
        missing_workflow = await client.call_tool(
            "comfyui.admin.workflow.change.plan",
            {
                "workflow_id": "missing",
                "server_id": "local",
                "operations": [
                    {
                        "op": "set_input",
                        "node_id": "1",
                        "field": "text",
                        "value": "unused",
                    }
                ],
            },
        )

    async with Client(admin) as client:
        admin_names = {tool.name for tool in (await client.list_tools()).tools}
        published = await client.call_tool(
            "comfyui.admin.workflow.publish",
            {"deployment_id": committed.structured_content["deployment_id"]},
        )
        missing_deployment = await client.call_tool(
            "comfyui.admin.workflow.publish",
            {"deployment_id": "deployment_missing"},
        )

    # AUTHORING owns the edit chain; ADMIN keeps only deployment publishing.
    assert {
        "comfyui.admin.workflow.change.plan",
        "comfyui.admin.workflow.change.commit",
    } <= authoring_names
    assert "comfyui.admin.workflow.publish" not in authoring_names
    assert {
        "comfyui.admin.workflow.publish",
        "comfyui.admin.workflow.rollback",
    } <= admin_names
    assert "comfyui.admin.workflow.change.plan" not in admin_names
    assert "comfyui.admin.workflow.change.commit" not in admin_names
    assert published.structured_content["published"] is True
    assert json.loads(missing_deployment.content[0].text)["code"] == ("WORKFLOW_CHANGE_NOT_FOUND")
    assert json.loads(missing_workflow.content[0].text)["code"] == ("WORKFLOW_CHANGE_NOT_FOUND")
    active = workflows.get("local", "portrait")
    assert active is not None
    assert active.graph["1"]["inputs"]["text"] == "through-mcp"


def test_extract_subgraph_records_boundary_contracts(tmp_path: Path) -> None:
    changes, workflows = _services(tmp_path)

    extracted = changes.plan(
        "portrait",
        "local",
        [{"op": "extract_subgraph", "name": "output_seg", "node_ids": ["3"]}],
        object_info=OBJECT_INFO,
    )
    assert extracted["diff"]["subgraphs_added"] == ["output_seg"]
    assert extracted["diff"]["nodes_added"] == []
    assert extracted["diff"]["nodes_removed"] == []
    assert "output_seg" in extracted["diff"]["parameters_added"] or True  # catalog, not params

    committed = changes.commit(extracted["plan_id"], extracted["plan_digest"])
    revision = _revision_catalog(committed, workflows)
    definition = revision["extracted_subgraphs"]["output_seg"]
    assert definition["boundary_inputs"] == {
        "3.images": {"source_node_id": "2", "source_output": 0}
    }
    assert definition["boundary_outputs"] == []
    assert definition["nodes"]["3"]["class_type"] == "SaveImage"

    with_boundary_output = changes.plan(
        "portrait",
        "local",
        [{"op": "extract_subgraph", "name": "source_seg", "node_ids": ["2"]}],
        object_info=OBJECT_INFO,
    )
    assert with_boundary_output["diff"]["subgraphs_added"] == ["source_seg"]
    assert with_boundary_output["diff"]["subgraphs_removed"] == []
    committed = changes.commit(with_boundary_output["plan_id"], with_boundary_output["plan_digest"])
    revision = _revision_catalog(committed, workflows)
    definition = revision["extracted_subgraphs"]["source_seg"]
    assert definition["boundary_inputs"] == {}
    assert definition["boundary_outputs"] == [
        {
            "node_id": "2",
            "source_output": 0,
            "target_node_id": "3",
            "target_field": "images",
        }
    ]


def test_insert_subgraph_by_name_instantiates_and_disconnects_boundaries(tmp_path: Path) -> None:
    changes, workflows = _services(tmp_path)

    extracted = changes.plan(
        "portrait",
        "local",
        [{"op": "extract_subgraph", "name": "output_seg", "node_ids": ["3"]}],
        object_info=OBJECT_INFO,
    )
    committed = changes.commit(extracted["plan_id"], extracted["plan_digest"])
    changes.publish(committed["deployment_id"])

    inserted = changes.plan(
        "portrait",
        "local",
        [
            {"op": "insert_subgraph", "id_prefix": "sg", "subgraph": "output_seg"},
            {
                "op": "connect",
                "source_node_id": "2",
                "source_output": 0,
                "target_node_id": "sg_3",
                "target_input": "images",
            },
        ],
        object_info=OBJECT_INFO,
    )
    assert inserted["diff"]["nodes_added"] == ["sg_3"]
    assert inserted["diff"]["subgraphs_added"] == []
    committed = changes.commit(inserted["plan_id"], inserted["plan_digest"])
    revision = _revision_graph(committed, workflows)
    assert revision["sg_3"]["class_type"] == "SaveImage"
    assert revision["sg_3"]["inputs"]["images"] == ["2", 0]
    assert revision["sg_3"]["inputs"]["filename_prefix"] == "result"


def test_insert_subgraph_requires_exactly_one_of_nodes_or_subgraph(tmp_path: Path) -> None:
    changes, _workflows = _services(tmp_path)

    with pytest.raises(ValueError, match="exactly one of 'nodes' or 'subgraph'"):
        changes.plan(
            "portrait",
            "local",
            [
                {
                    "op": "insert_subgraph",
                    "id_prefix": "sg",
                    "subgraph": "output_seg",
                    "nodes": {"a": {"class_type": "Image", "inputs": {}}},
                }
            ],
            object_info=OBJECT_INFO,
        )
    with pytest.raises(ValueError, match="exactly one of 'nodes' or 'subgraph'"):
        changes.plan(
            "portrait",
            "local",
            [{"op": "insert_subgraph", "id_prefix": "sg"}],
            object_info=OBJECT_INFO,
        )


def test_insert_subgraph_rejects_unknown_or_old_format_name(tmp_path: Path) -> None:
    changes, workflows = _services(tmp_path)

    with pytest.raises(ValueError, match="stored revision metadata is invalid"):
        changes.plan(
            "portrait",
            "local",
            [{"op": "insert_subgraph", "id_prefix": "sg", "subgraph": "missing"}],
            object_info=OBJECT_INFO,
        )

    extracted = changes.plan(
        "portrait",
        "local",
        [{"op": "extract_subgraph", "name": "legacy_seg", "node_ids": ["2"]}],
        object_info=OBJECT_INFO,
    )
    committed = changes.commit(extracted["plan_id"], extracted["plan_digest"])
    changes.publish(committed["deployment_id"])

    with pytest.raises(ValueError, match="is not extracted"):
        changes.plan(
            "portrait",
            "local",
            [{"op": "insert_subgraph", "id_prefix": "sg", "subgraph": "missing"}],
            object_info=OBJECT_INFO,
        )
    with_legacy = changes.plan(
        "portrait",
        "local",
        [
            {"op": "insert_subgraph", "id_prefix": "leg", "subgraph": "legacy_seg"},
            {
                "op": "connect",
                "source_node_id": "leg_2",
                "source_output": 0,
                "target_node_id": "3",
                "target_input": "images",
            },
        ],
        object_info=OBJECT_INFO,
    )
    assert with_legacy["diff"]["nodes_added"] == ["leg_2"]


def test_extract_and_reuse_roundtrip_in_one_plan(tmp_path: Path) -> None:
    changes, workflows = _services(tmp_path)

    roundtrip = changes.plan(
        "portrait",
        "local",
        [
            {"op": "extract_subgraph", "name": "seg", "node_ids": ["2"]},
            {"op": "insert_subgraph", "id_prefix": "rt", "subgraph": "seg"},
            {
                "op": "connect",
                "source_node_id": "rt_2",
                "source_output": 0,
                "target_node_id": "3",
                "target_input": "images",
            },
        ],
        object_info=OBJECT_INFO,
    )
    assert roundtrip["diff"]["nodes_added"] == ["rt_2"]
    assert roundtrip["diff"]["subgraphs_added"] == ["seg"]
    committed = changes.commit(roundtrip["plan_id"], roundtrip["plan_digest"])
    revision = _revision_graph(committed, workflows)
    assert revision["rt_2"]["class_type"] == "Image"
    assert revision["3"]["inputs"]["images"] == ["rt_2", 0]


def test_rollback_preserves_extracted_subgraph_catalog(tmp_path: Path) -> None:
    changes, workflows = _services(tmp_path)

    extracted = changes.plan(
        "portrait",
        "local",
        [{"op": "extract_subgraph", "name": "seg", "node_ids": ["2"]}],
        object_info=OBJECT_INFO,
    )
    committed = changes.commit(extracted["plan_id"], extracted["plan_digest"])
    changes.publish(committed["deployment_id"])
    base_revision_id = workflows.describe("portrait", "local")["revision_id"]

    changed = changes.plan(
        "portrait",
        "local",
        [{"op": "set_input", "node_id": "1", "field": "text", "value": "after-rollback"}],
        object_info=OBJECT_INFO,
    )
    changes.commit(changed["plan_id"], changed["plan_digest"])

    rolled_back = changes.rollback(
        "portrait", "local", base_revision_id, request_id="rollback-keep-catalog"
    )
    catalog = _revision_catalog(rolled_back, workflows)
    assert "seg" in catalog["extracted_subgraphs"]

    reused = changes.plan(
        "portrait",
        "local",
        [{"op": "insert_subgraph", "id_prefix": "rb", "subgraph": "seg"}],
        object_info=OBJECT_INFO,
    )
    assert reused["diff"]["nodes_added"] == ["rb_2"]


def _revision_catalog(
    committed: dict[str, Any],
    workflows: SQLiteWorkflowRepository,
) -> dict[str, Any]:
    revision = workflows.get_revision(committed["revision_id"])
    schema = revision["parameter_schema"]
    assert "_revision" in schema
    return schema["_revision"]


def _revision_graph(
    committed: dict[str, Any],
    workflows: SQLiteWorkflowRepository,
) -> dict[str, Any]:
    revision = workflows.get_revision(committed["revision_id"])
    return revision["graph"]


def test_change_plan_unknown_class_type_reports_location_and_hint(
    tmp_path: Path,
) -> None:
    """P0-3: plan failures carry node location and a repair hint pointing at
    the node catalog tool instead of a bare message list."""
    changes, _workflows = _services(tmp_path)

    with pytest.raises(
        WorkflowChangeValidationError, match=r"node 9 \[unknown_node_type\]"
    ) as excinfo:
        changes.plan(
            "portrait",
            "local",
            [{"op": "add_node", "node_id": "9", "class_type": "FutureNode", "inputs": {}}],
            object_info=OBJECT_INFO,
        )

    message = str(excinfo.value)
    assert "node 9 [unknown_node_type]: Unknown node type: FutureNode" in message
    assert "hint: 该节点类型不存在于服务器，用 comfyui.node.list 搜索可用节点类型" in message
    details = excinfo.value.details
    assert details["suggested_queries"] == [
        {
            "tool": "comfyui.node.blueprint",
            "arguments": {"server_id": "local", "query": "FutureNode"},
        },
        {"tool": "comfyui.node.list", "arguments": {"server_id": "local"}},
    ]


def test_change_plan_invalid_enum_reports_field_and_hint(tmp_path: Path) -> None:
    """Range violations point at the offending field and carry the concrete
    class_type so the describe hint is executable."""
    changes, _workflows = _services(tmp_path)

    with pytest.raises(WorkflowChangeValidationError, match=r"node 9 field cfg") as excinfo:
        changes.plan(
            "portrait",
            "local",
            [
                {
                    "op": "add_node",
                    "node_id": "9",
                    "class_type": "KSampler",
                    "inputs": {"cfg": 999},
                }
            ],
            object_info=OBJECT_INFO,
        )

    message = str(excinfo.value)
    assert "node 9 field cfg [input_out_of_range]" in message
    assert "hint: 用 comfyui.node.describe KSampler 查看该节点的输入签名与枚举值" in message
    details = excinfo.value.details
    assert details["suggested_queries"] == [
        {
            "tool": "comfyui.node.describe",
            "arguments": {"server_id": "local", "node_class": "KSampler"},
        }
    ]


def test_change_diff_includes_highlighted_mermaid(tmp_path: Path) -> None:
    """The revision diff carries a Mermaid view of the after graph with added
    nodes highlighted."""
    changes, workflows = _services(tmp_path)
    imported = WorkflowImportService(
        WorkflowGraphService(
            ParameterRoleRegistry.default(), DependencyExtractorRegistry.default()
        ),
        WorkflowValidationService(),
        workflows,
    ).preview(
        {
            "1": {"class_type": "Text", "inputs": {"text": "before"}},
            "2": {"class_type": "Image", "inputs": {}},
            "3": {
                "class_type": "SaveImage",
                "inputs": {"images": ["2", 0], "filename_prefix": "result"},
            },
        },
        workflow_id="portrait",
        server_id="local",
        object_info=OBJECT_INFO,
    )
    committed = WorkflowImportService(
        WorkflowGraphService(
            ParameterRoleRegistry.default(), DependencyExtractorRegistry.default()
        ),
        WorkflowValidationService(),
        workflows,
    ).commit(imported)
    workflows.publish(committed["deployment_id"])

    plan = changes.plan(
        "portrait",
        "local",
        [
            {
                "op": "add_node",
                "node_id": "9",
                "class_type": "KSampler",
                "inputs": {"cfg": 7},
            },
        ],
        object_info=OBJECT_INFO,
    )
    committed_plan = changes.commit(plan["plan_id"], plan["plan_digest"])
    revision_diff = changes.diff(committed["revision_id"], committed_plan["revision_id"])

    assert revision_diff["nodes_added"] == ["9"]
    assert revision_diff["mermaid"].startswith("flowchart LR")
    assert "classDef added" in revision_diff["mermaid"]
    assert 'N4["KSampler"]:::added' in revision_diff["mermaid"]


def test_change_plan_supports_large_graphs_without_mermaid(tmp_path: Path) -> None:
    """plan must not render Mermaid (50-node cap) for legal >50-node graphs."""
    changes, workflows = _services(tmp_path)
    big_graph = {
        str(index): {"class_type": "Text", "inputs": {"text": "x"}}
        for index in range(59)
    }
    big_graph["60"] = {"class_type": "Image", "inputs": {}}
    big_graph["61"] = {
        "class_type": "SaveImage",
        "inputs": {"images": ["60", 0], "filename_prefix": "result"},
    }
    imported = WorkflowImportService(
        WorkflowGraphService(
            ParameterRoleRegistry.default(), DependencyExtractorRegistry.default()
        ),
        WorkflowValidationService(),
        workflows,
    ).preview(big_graph, workflow_id="portrait", server_id="local", object_info=OBJECT_INFO)
    committed = WorkflowImportService(
        WorkflowGraphService(
            ParameterRoleRegistry.default(), DependencyExtractorRegistry.default()
        ),
        WorkflowValidationService(),
        workflows,
    ).commit(imported)
    workflows.publish(committed["deployment_id"])

    plan = changes.plan(
        "portrait",
        "local",
        [{"op": "add_node", "node_id": "99", "class_type": "Text", "inputs": {"text": "y"}}],
        object_info=OBJECT_INFO,
    )
    assert "mermaid" not in plan["diff"]
    assert plan["diff"]["nodes_added"] == ["99"]
