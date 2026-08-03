"""Phase I semantic workflow import contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from comfyui_mcp_skills.adapters.mcp.admin import create_admin_server
from comfyui_mcp_skills.adapters.mcp.server import create_server
from comfyui_mcp_skills.adapters.mcp.tooling import fixed_tools
from comfyui_mcp_skills.application.authorization import AuthorizationContext, Scope, Toolset
from comfyui_mcp_skills.application.experiments import ExperimentService
from comfyui_mcp_skills.application.workflow_conversion import convert_editor_workflow
from comfyui_mcp_skills.application.workflow_graph import (
    WorkflowGraphService,
    WorkflowValidationService,
)
from comfyui_mcp_skills.application.workflow_import import WorkflowImportService
from comfyui_mcp_skills.application.workflow_inspection import WorkflowInspectionService
from comfyui_mcp_skills.domain.workflow_schema import build_input_schema
from comfyui_mcp_skills.domain.workflow_semantics import (
    DependencyExtractorRegistry,
    ParameterRoleRegistry,
)
from comfyui_mcp_skills.infrastructure.persistence.assets import FileAssetRepository
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.repository_factory import RepositoryBundle
from comfyui_mcp_skills.infrastructure.persistence.runs import FileRunRepository
from comfyui_mcp_skills.infrastructure.persistence.sqlite_experiments import (
    SQLiteExperimentRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_workflows import (
    SQLiteWorkflowRepository,
)

OBJECT_INFO: dict[str, Any] = {
    "CheckpointLoaderSimple": {
        "input": {"required": {"ckpt_name": [["model.safetensors"]]}},
        "input_order": {"required": ["ckpt_name"]},
        "output": ["MODEL", "CLIP", "VAE"],
    },
    "ImageSource": {
        "input": {"required": {}},
        "input_order": {"required": []},
        "output": ["IMAGE"],
    },
    "CLIPTextEncode": {
        "input": {
            "required": {
                "clip": ["CLIP"],
                "text": ["STRING", {"default": ""}],
            }
        },
        "input_order": {"required": ["clip", "text"]},
        "output": ["CONDITIONING"],
    },
    "SaveImage": {
        "input": {
            "required": {
                "images": ["IMAGE"],
                "filename_prefix": ["STRING", {"default": "ComfyUI"}],
            }
        },
        "input_order": {"required": ["images", "filename_prefix"]},
        "output": [],
        "output_node": True,
    },
}

API_WORKFLOW: dict[str, Any] = {
    "1": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "model.safetensors"},
        "_meta": {"title": "Checkpoint"},
    },
    "2": {
        "class_type": "CLIPTextEncode",
        "inputs": {"clip": ["1", 1], "text": "a blue bird"},
        "_meta": {"title": "Positive Prompt"},
    },
    "3": {"class_type": "ImageSource", "inputs": {}},
    "4": {
        "class_type": "SaveImage",
        "inputs": {"images": ["3", 0], "filename_prefix": "result"},
    },
}


class _RevisionWriter:
    def __init__(self) -> None:
        self.commits: list[dict[str, Any]] = []

    def create_revision(self, **values: Any) -> dict[str, Any]:
        self.commits.append(values)
        return {
            "workflow_id": values["workflow_id"],
            "revision_id": "revision_" + "a" * 32,
            "deployment_id": "deployment_" + "b" * 32,
            "published": False,
        }


def _services() -> tuple[WorkflowGraphService, WorkflowValidationService]:
    graph = WorkflowGraphService(
        ParameterRoleRegistry.default(), DependencyExtractorRegistry.default()
    )
    return graph, WorkflowValidationService()


def test_graph_description_is_deterministic_and_semantic() -> None:
    graph, _ = _services()

    first = graph.describe(API_WORKFLOW, object_info=OBJECT_INFO)
    second = graph.describe(dict(reversed(API_WORKFLOW.items())), object_info=OBJECT_INFO)
    assert first == second

    assert first["node_count"] == 4
    assert first["edge_count"] == 2
    assert first["parameters"]["prompt"] == {
        "node_id": "2",
        "field": "text",
        "required": True,
        "type": "string",
        "description": "Text prompt",
        "role": "prompt",
    }
    assert first["outputs"] == [{"node_id": "4", "class_type": "SaveImage", "media_type": "image"}]
    assert first["dependencies"]["models"] == [
        {
            "filename": "model.safetensors",
            "folder": "checkpoints",
            "loader_node": "CheckpointLoaderSimple",
            "node_id": "1",
        }
    ]
    assert first["dependencies"]["coverage"] == "complete"


def test_dependency_registry_reports_unknown_loader_as_partial() -> None:
    registry = DependencyExtractorRegistry.default()
    graph = {
        "7": {
            "class_type": "FutureModelLoader",
            "inputs": {"model_name": "future.gguf"},
        }
    }

    report = registry.extract(graph, object_info={"FutureModelLoader": {}})

    assert report["coverage"] == "partial"
    assert report["unverified_loaders"] == ["FutureModelLoader"]
    assert report["models"] == []


def test_validation_rejects_missing_nodes_ports_and_unknown_types() -> None:
    _, validation = _services()
    invalid = {
        "1": {"class_type": "MissingNode", "inputs": {}},
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["404", 9], "text": "prompt"},
        },
    }

    result = validation.validate_api(invalid, OBJECT_INFO)

    assert result["valid"] is False
    assert result["unsupported_nodes"] == ["MissingNode"]
    assert {issue["code"] for issue in result["issues"]} == {
        "missing_source_node",
        "unknown_node_type",
    }


def test_validation_rejects_missing_required_and_invalid_scalar_inputs() -> None:
    _, validation = _services()
    graph = {
        "1": {
            "class_type": "Sampler",
            "inputs": {"steps": 101, "mode": "unknown"},
        }
    }
    object_info = {
        "Sampler": {
            "input": {
                "required": {
                    "seed": ["INT", {"min": 0, "max": 1000}],
                    "steps": ["INT", {"min": 1, "max": 50}],
                    "mode": [["fast", "quality"]],
                }
            }
        }
    }

    result = validation.validate_api(graph, object_info)

    assert {issue["code"] for issue in result["issues"]} == {
        "missing_required_input",
        "input_out_of_range",
        "invalid_enum_value",
    }


def test_validation_rejects_socket_scalars_combo_misses_and_unsafe_media_refs() -> None:
    _, validation = _services()
    graph = {
        "1": {
            "class_type": "MediaLoader",
            "inputs": {
                "images": "not-a-connection",
                "preset": "unknown",
                "filename": "C:private.png",
            },
        }
    }
    object_info = {
        "MediaLoader": {
            "input": {
                "required": {
                    "images": ["IMAGE"],
                    "preset": ["COMBO", {"options": ["fast", "quality"]}],
                    "filename": ["STRING"],
                }
            }
        }
    }

    result = validation.validate_api(graph, object_info)

    assert {issue["code"] for issue in result["issues"]} == {
        "invalid_input_type",
        "invalid_enum_value",
        "unsafe_media_path",
    }


def test_editor_conversion_marks_malformed_and_duplicate_content_as_loss() -> None:
    source = {
        "nodes": [
            {"id": 1, "type": "ImageSource"},
            {"id": 1, "type": "ImageSource"},
            {"id": "bad", "type": "ImageSource"},
            "invalid",
        ],
        "links": [[1, 1], "invalid"],
    }

    graph, unsupported, dropped = convert_editor_workflow(source, OBJECT_INFO)

    assert graph == {
        "1": {"inputs": {}, "class_type": "ImageSource", "_meta": {"title": "ImageSource"}}
    }
    assert unsupported == ()
    assert dropped
    assert "nodes[1].duplicate_id" in dropped
    assert "nodes[2].id" in dropped
    assert "nodes[3]" in dropped
    assert "links[0]" in dropped


def test_editor_conversion_marks_dangling_links_and_extra_widgets_as_loss() -> None:
    source = {
        "nodes": [
            {
                "id": 1,
                "type": "ImageSource",
                "inputs": ["malformed"],
                "widgets_values": ["extra"],
            },
            {"id": 9, "type": "Reroute", "inputs": [{"name": "", "link": 2}]},
        ],
        "links": [
            [1, 1, 0, 404, 0, "IMAGE"],
            [2, 1, 0, 9, 99, "IMAGE"],
        ],
    }

    _graph, _unsupported, dropped = convert_editor_workflow(source, OBJECT_INFO)

    assert "1.inputs[0]" in dropped
    assert "1.widgets_values[0]" in dropped
    assert "links[0].target" in dropped
    assert "links[1].target" in dropped


def test_dependency_registry_marks_model_like_custom_node_as_partial() -> None:
    graph = {"1": {"class_type": "CustomLoadModel", "inputs": {"model_name": "missing.gguf"}}}

    report = DependencyExtractorRegistry.default().extract(graph)

    assert report["coverage"] == "partial"
    assert report["unverified_loaders"] == ["CustomLoadModel"]


def test_validation_rejects_unsafe_media_paths_before_import() -> None:
    _, validation = _services()
    graph = {
        "1": {
            "class_type": "LoadImage",
            "inputs": {"image": "C:/Users/alice/private.png"},
        }
    }
    object_info = {"LoadImage": {"input": {"required": {"image": ["STRING"]}}, "output": ["IMAGE"]}}

    result = validation.validate_api(graph, object_info)

    assert result["valid"] is False
    assert [issue["code"] for issue in result["issues"]] == ["unsafe_media_path"]


def test_parameter_roles_generate_deterministic_constrained_schema() -> None:
    graph = {
        "2": {"class_type": "Latent", "inputs": {"width": 1024}},
        "1": {"class_type": "Sampler", "inputs": {"seed": 42}},
    }
    object_info = {
        "Latent": {
            "input": {"required": {"width": ["INT", {"min": 16, "max": 8192}]}},
        },
        "Sampler": {
            "input": {"required": {"seed": ["INT", {"min": 0, "max": 1000}]}},
        },
    }
    registry = ParameterRoleRegistry.default()

    first = registry.extract(graph, object_info=object_info)
    second = registry.extract(dict(reversed(graph.items())), object_info=object_info)

    assert first == second
    schema = build_input_schema(first)
    assert schema["properties"]["width"]["minimum"] == 16
    assert schema["properties"]["width"]["maximum"] == 8192
    assert schema["properties"]["seed"]["minimum"] == 0
    assert schema["properties"]["seed"]["maximum"] == 1000


def test_large_combo_options_are_summarized_without_narrowing_schema() -> None:
    options = [f"option-{index}" for index in range(250)]
    graph = {"1": {"class_type": "Choice", "inputs": {"format": "option-0"}}}
    object_info = {"Choice": {"input": {"required": {"format": [options]}}}}

    parameters = ParameterRoleRegistry.default().extract(
        graph, media_type="video", object_info=object_info
    )

    assert "enum" not in parameters["format"]
    assert parameters["format"]["options_preview"] == options[:20]
    assert parameters["format"]["options_truncated"] is True
    assert "enum" not in build_input_schema(parameters)["properties"]["format"]


def test_api_import_preview_then_commit_creates_unpublished_revision() -> None:
    graph, validation = _services()
    writer = _RevisionWriter()
    service = WorkflowImportService(graph, validation, writer)

    preview = service.preview(
        API_WORKFLOW,
        workflow_id="bird",
        server_id="local",
        object_info=OBJECT_INFO,
        node_replacements={"CLIPTextEncode": "CLIPTextEncodeV2"},
    )
    committed = service.commit(preview)

    assert preview.source_format == "api"
    assert preview.requires_manual_review is False
    assert preview.unsupported_nodes == ()
    assert preview.deprecated_nodes == (
        {
            "node_id": "2",
            "old": "CLIPTextEncode",
            "new": "CLIPTextEncodeV2",
        },
    )
    assert committed["published"] is False
    assert writer.commits[0]["graph"] == API_WORKFLOW
    assert writer.commits[0]["dependency_contract"]["coverage"] == "complete"


def test_editor_preview_reports_unsupported_nodes_without_committing() -> None:
    graph, validation = _services()
    writer = _RevisionWriter()
    service = WorkflowImportService(graph, validation, writer)
    editor = {
        "nodes": [
            {"id": 1, "type": "CLIPTextEncode", "widgets_values": ["hello"]},
            {"id": 2, "type": "MissingCustomNode", "widgets_values": []},
        ],
        "links": [],
    }

    preview = service.preview(
        editor,
        workflow_id="editor-import",
        server_id="local",
        object_info=OBJECT_INFO,
    )
    assert preview.source_format == "editor"
    assert preview.requires_manual_review is True
    assert preview.unsupported_nodes == ("MissingCustomNode",)
    with pytest.raises(ValueError, match="manual review"):
        service.commit(preview)
    assert writer.commits == []


def test_editor_conversion_preserves_reroute_combo_and_control_marker() -> None:
    object_info = {
        "Producer": {"input": {"required": {}}, "output": ["MODEL"]},
        "Target": {
            "input": {
                "required": {
                    "model": ["MODEL"],
                    "preset": ["COMBO", {"options": ["A", "B"]}],
                    "seed": ["INT", {"control_after_generate": True}],
                }
            },
            "input_order": {"required": ["model", "preset", "seed"]},
            "output": [],
        },
    }
    editor = {
        "nodes": [
            {"id": 1, "type": "Producer", "inputs": [], "widgets_values": []},
            {
                "id": 9,
                "type": "Reroute",
                "inputs": [{"name": "", "link": 10}],
                "widgets_values": [],
            },
            {
                "id": 2,
                "type": "Target",
                "inputs": [{"name": "model", "link": 11}],
                "widgets_values": ["B", 42, "fixed"],
            },
        ],
        "links": [[10, 1, 0, 9, 0, "MODEL"], [11, 9, 0, 2, 0, "MODEL"]],
    }

    converted, unsupported, dropped = convert_editor_workflow(editor, object_info)

    assert converted["2"]["inputs"] == {
        "model": ["1", 0],
        "preset": "B",
        "seed": 42,
    }
    assert unsupported == ()
    assert dropped == ()


def test_validation_rejects_incompatible_connection_types() -> None:
    _, validation = _services()
    graph = {
        "1": {"class_type": "ImageSource", "inputs": {}},
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["1", 0], "text": "prompt"},
        },
    }

    result = validation.validate_api(graph, OBJECT_INFO)

    assert result["valid"] is False
    assert [issue["code"] for issue in result["issues"]] == ["port_type_mismatch"]


def test_import_commit_persists_idempotent_unpublished_revision(tmp_path: Path) -> None:
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()
    repository = SQLiteWorkflowRepository(store)
    graph, validation = _services()
    service = WorkflowImportService(graph, validation, repository)
    preview = service.preview(
        API_WORKFLOW,
        workflow_id="bird",
        server_id="local",
        object_info=OBJECT_INFO,
    )

    first = service.commit(preview)
    repeated = service.commit(preview)
    revision = repository.get_revision(first["revision_id"])

    assert repeated == first
    assert first["published"] is False
    assert revision["workflow_id"] == "bird"
    assert revision["graph"] == API_WORKFLOW
    assert revision["parameter_schema"]["parameters"] == preview.parameter_schema
    assert revision["dependency_contract"] == preview.dependency_contract
    repository.publish(first["deployment_id"])
    after_publish = service.commit(preview)

    assert after_publish["published"] is True


class _WorkflowReader:
    def list_revisions(self, workflow_id: str) -> list[dict[str, Any]]:
        return [
            {
                "workflow_id": workflow_id,
                "revision_id": "revision_" + "a" * 32,
                "content_digest": "c" * 64,
                "created_at": "2026-07-31T00:00:00+00:00",
            }
        ]

    def describe(self, workflow_id: str, server_id: str) -> dict[str, Any]:
        return {
            "workflow_id": workflow_id,
            "server_id": server_id,
            "revision_id": "revision_" + "a" * 32,
            "deployment_id": "deployment_" + "b" * 32,
            "content_digest": "c" * 64,
            "validation_status": "valid",
            "published": True,
        }

    def get_revision(self, revision_id: str) -> dict[str, Any]:
        return {
            "revision_id": revision_id,
            "workflow_id": "bird",
            "graph": API_WORKFLOW,
            "parameter_schema": {"parameters": {}},
            "dependency_contract": DependencyExtractorRegistry.default().extract(API_WORKFLOW),
            "content_digest": "c" * 64,
            "created_at": "2026-07-31T00:00:00+00:00",
        }

    def get_published_revision(self, workflow_id: str) -> dict[str, Any]:
        revision = self.get_revision("revision_" + "a" * 32)
        revision["workflow_id"] = workflow_id
        return revision


class _Gateway:
    def get_object_info(self) -> dict[str, Any]:
        return OBJECT_INFO

    def get_models(self, folder: str) -> list[str]:
        assert folder == "checkpoints"
        return []


def test_describe_returns_semantics_without_raw_graph() -> None:
    graph, validation = _services()
    service = WorkflowInspectionService(_WorkflowReader(), graph, validation)

    result = service.describe("bird", "local", _Gateway())

    assert result["workflow_id"] == "bird"
    assert result["semantic_graph"]["node_count"] == 4
    assert "graph" not in result
    assert result["validation"]["valid"] is True


def test_dependency_check_reports_missing_models_and_installed_nodes() -> None:
    graph, validation = _services()
    service = WorkflowInspectionService(_WorkflowReader(), graph, validation)

    result = service.dependencies_check("bird", "local", _Gateway())

    assert result["missing_nodes"] == []
    assert result["missing_models"] == [
        {
            "filename": "model.safetensors",
            "folder": "checkpoints",
            "loader_node": "CheckpointLoaderSimple",
            "node_id": "1",
        }
    ]
    assert result["is_ready"] is False
    assert result["coverage"] == "complete"


def test_dependency_check_treats_migrated_empty_contract_as_partial() -> None:
    class LegacyWorkflowReader(_WorkflowReader):
        def get_revision(self, revision_id: str) -> dict[str, Any]:
            revision = super().get_revision(revision_id)
            revision["dependency_contract"] = {}
            return revision

    graph, validation = _services()
    service = WorkflowInspectionService(LegacyWorkflowReader(), graph, validation)

    result = service.dependencies_check("bird", "local", _Gateway())

    assert result["required_nodes"] == []
    assert result["required_models"] == []
    assert result["unverified_loaders"] == []
    assert result["coverage"] == "partial"
    assert result["is_ready"] is False


def test_graph_resource_uses_latest_immutable_revision_without_raw_graph() -> None:
    graph, validation = _services()
    service = WorkflowInspectionService(_WorkflowReader(), graph, validation)

    result = service.graph_resource("bird")

    assert result["workflow_id"] == "bird"
    assert result["revision_id"] == "revision_" + "a" * 32
    assert result["semantic_graph"]["edge_count"] == 2
    assert "graph" not in result


def test_phase_i_tools_publish_strict_read_only_contracts() -> None:
    tools = {tool.name: tool for tool in fixed_tools()}
    describe = tools["comfyui.workflow.describe"]
    dependencies = tools["comfyui.workflow.dependencies.check"]

    assert describe.input_schema["required"] == ["workflow_id", "server_id"]
    assert dependencies.input_schema == {
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string", "minLength": 1},
            "server_id": {"type": "string", "minLength": 1},
        },
        "required": ["workflow_id", "server_id"],
        "additionalProperties": False,
    }
    assert dependencies.annotations is not None
    assert dependencies.annotations.read_only_hint is True
    assert dependencies.annotations.open_world_hint is True


@pytest.mark.anyio
async def test_semantic_graph_resource_is_discoverable_and_bounded(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "default_server": "local",
                "servers": [{"id": "local", "url": "http://127.0.0.1:8188", "enabled": True}],
            }
        ),
        encoding="utf-8",
    )
    store = SQLiteControlPlaneStore(tmp_path / "data" / "control-plane.sqlite3")
    store.initialize()
    workflows = SQLiteWorkflowRepository(store)
    graph, validation = _services()
    import_service = WorkflowImportService(graph, validation, workflows)
    committed = import_service.commit(
        import_service.preview(
            API_WORKFLOW,
            workflow_id="bird",
            server_id="local",
            object_info=OBJECT_INFO,
        )
    )
    workflows.publish(committed["deployment_id"])
    repositories = RepositoryBundle(
        workflows=workflows,
        runs=FileRunRepository(tmp_path),
        assets=FileAssetRepository(tmp_path),
        workflow_store="sqlite",
        run_store="file",
        asset_store="file",
        store=store,
    )
    server = create_server(tmp_path, repositories=repositories)

    async with Client(server) as client:
        templates = await client.list_resource_templates()
        result = await client.read_resource("comfyui://workflows/bird/graph")
        outputs = await client.read_resource("comfyui://workflows/bird/outputs")

    template_uris = {str(template.uri_template) for template in templates.resource_templates}
    assert "comfyui://workflows/{workflow_id}/graph" in template_uris
    assert "comfyui://workflows/{workflow_id}/outputs" in template_uris
    document = json.loads(result.contents[0].text)
    assert document["semantic_graph"]["node_count"] == 4
    assert "graph" not in document
    output_document = json.loads(outputs.contents[0].text)
    assert output_document["outputs"] == [
        {"node_id": "4", "class_type": "SaveImage", "media_type": "image"}
    ]
    assert "semantic_graph" not in output_document


@pytest.mark.anyio
async def test_admin_import_tool_previews_then_commits_unpublished_revision(
    tmp_path: Path,
) -> None:
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "default_server": "local",
                "servers": [{"id": "local", "url": "http://127.0.0.1:8188", "enabled": True}],
            }
        ),
        encoding="utf-8",
    )
    store = SQLiteControlPlaneStore(tmp_path / "data" / "control-plane.sqlite3")
    store.initialize()
    workflows = SQLiteWorkflowRepository(store)
    repositories = RepositoryBundle(
        workflows=workflows,
        runs=FileRunRepository(tmp_path),
        assets=FileAssetRepository(tmp_path),
        workflow_store="sqlite",
        run_store="file",
        asset_store="file",
        store=store,
    )
    server = create_admin_server(
        tmp_path,
        enabled=True,
        repositories=repositories,
        gateway_factory=lambda _config: _Gateway(),
    )

    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        previewed = await client.call_tool(
            "comfyui.admin.workflow.import",
            {"server_id": "local", "workflow_id": "bird", "source": API_WORKFLOW},
        )
        assert workflows.list_revisions("bird") == []
        committed = await client.call_tool(
            "comfyui.admin.workflow.import",
            {
                "server_id": "local",
                "workflow_id": "bird",
                "source": API_WORKFLOW,
                "commit": True,
            },
        )

    assert "comfyui.admin.workflow.import" in names
    assert previewed.structured_content["requires_manual_review"] is False
    assert committed.structured_content["commit"]["published"] is False
    assert len(workflows.list_revisions("bird")) == 1


def test_batch_preview_isolates_each_invalid_workflow() -> None:
    graph, validation = _services()
    service = WorkflowImportService(graph, validation, _RevisionWriter())

    result = service.preview_many(
        [
            {"source_id": "good", "workflow_id": "bird", "source": API_WORKFLOW},
            {"source_id": "bad", "workflow_id": "broken", "source": {"invalid": True}},
        ],
        server_id="local",
        object_info=OBJECT_INFO,
    )

    assert result["results"][0]["status"] == "previewed"
    assert result["results"][0]["preview"]["workflow_id"] == "bird"
    assert result["results"][1] == {
        "source_id": "bad",
        "workflow_id": "broken",
        "status": "failed",
        "error": "Unrecognized workflow format",
    }
    assert result["previewed"] == 1
    assert result["failed"] == 1


def test_imported_published_workflow_is_experiment_ready(tmp_path: Path) -> None:
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()
    workflows = SQLiteWorkflowRepository(store)
    graphs, validation = _services()
    experiment_workflow = json.loads(json.dumps(API_WORKFLOW))
    experiment_workflow["3"]["inputs"] = {"width": 64, "height": 64}
    experiment_info = json.loads(json.dumps(OBJECT_INFO))
    experiment_info["ImageSource"]["input"] = {
        "required": {
            "width": ["INT", {"default": 64}],
            "height": ["INT", {"default": 64}],
        }
    }
    experiment_info["ImageSource"]["input_order"] = {"required": ["width", "height"]}
    importer = WorkflowImportService(
        graphs,
        validation,
        workflows,
        runtime_estimator=lambda _server_id, _graph: 12.0,
    )
    imported = importer.commit(
        importer.preview(
            experiment_workflow,
            workflow_id="bird",
            server_id="local",
            object_info=experiment_info,
        )
    )
    workflows.publish(imported["deployment_id"])
    experiments = ExperimentService(SQLiteExperimentRepository(store))
    plan = experiments.plan(
        "owner-a",
        "bird",
        "local",
        {"mode": "explicit", "variants": [{}]},
        {"prompt": "a blue bird", "width": 64, "height": 64},
        {
            "max_variants": 1,
            "max_concurrency": 1,
            "max_pixels": 4096,
            "max_outputs": 1,
            "max_seconds": 12,
        },
        "continue",
        1,
        0,
    )
    committed = experiments.commit(plan["plan_id"], plan["plan_digest"], "owner-a")
    assert committed["pinned_revision_id"] == imported["revision_id"]


@pytest.mark.anyio
async def test_phase_i_describe_and_dependency_tools_use_published_revision(
    tmp_path: Path,
) -> None:
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "default_server": "local",
                "servers": [{"id": "local", "url": "http://127.0.0.1:8188", "enabled": True}],
            }
        ),
        encoding="utf-8",
    )
    store = SQLiteControlPlaneStore(tmp_path / "data" / "control-plane.sqlite3")
    store.initialize()
    workflows = SQLiteWorkflowRepository(store)
    graph, validation = _services()
    importer = WorkflowImportService(graph, validation, workflows)
    committed = importer.commit(
        importer.preview(
            API_WORKFLOW,
            workflow_id="bird",
            server_id="local",
            object_info=OBJECT_INFO,
        )
    )
    workflows.publish(committed["deployment_id"])
    repositories = RepositoryBundle(
        workflows=workflows,
        runs=FileRunRepository(tmp_path),
        assets=FileAssetRepository(tmp_path),
        workflow_store="sqlite",
        run_store="file",
        asset_store="file",
        store=store,
    )
    server = create_server(
        tmp_path,
        repositories=repositories,
        gateway_factory=lambda _config: _Gateway(),
        authorization=AuthorizationContext(
            "author", frozenset({Scope.OBSERVE, Scope.AUTHOR}), Toolset.AUTHORING
        ),
    )

    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        described = await client.call_tool(
            "comfyui.workflow.describe", {"workflow_id": "bird", "server_id": "local"}
        )
        dependencies = await client.call_tool(
            "comfyui.workflow.dependencies.check",
            {"workflow_id": "bird", "server_id": "local"},
        )

    assert "comfyui.workflow.dependencies.check" in names
    assert described.structured_content["semantic_graph"]["node_count"] == 4
    assert dependencies.structured_content["missing_models"][0]["filename"] == ("model.safetensors")
