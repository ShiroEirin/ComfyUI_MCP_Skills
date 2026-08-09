"""Registered high-level branch recipes: upscale / save / lora insertion."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from comfyui_mcp_skills.application.workflow_change import WorkflowChangeService
from comfyui_mcp_skills.application.workflow_graph import (
    WorkflowGraphService,
    WorkflowValidationService,
)
from comfyui_mcp_skills.application.workflow_import import WorkflowImportService
from comfyui_mcp_skills.application.workflow_recipes import apply_recipe
from comfyui_mcp_skills.domain.workflow_semantics import (
    DependencyExtractorRegistry,
    ParameterRoleRegistry,
)
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
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
        "input": {"required": {"cfg": ["FLOAT", {"min": 0.0, "max": 20.0}]}},
        "input_order": {"required": ["cfg"]},
        "output": ["LATENT"],
    },
    "UpscaleModelLoader": {
        "input": {
            "required": {
                "model_name": [["4x-UltraSharp.pth", "4x_NMKD-Superscale-SP_178000_G.pth"]]
            }
        },
        "input_order": {"required": ["model_name"]},
        "output": ["UPSCALE_MODEL"],
    },
    "ImageUpscaleWithModel": {
        "input": {"required": {"upscale_model": ["UPSCALE_MODEL"], "image": ["IMAGE"]}},
        "input_order": {"required": ["upscale_model", "image"]},
        "output": ["IMAGE"],
    },
    "LoraLoader": {
        "input": {
            "required": {
                "model": ["MODEL"],
                "clip": ["CLIP"],
                "lora_name": [["lora.safetensors", "detail.safetensors"]],
                "strength_model": ["FLOAT", {"default": 1.0, "min": -10.0, "max": 10.0}],
                "strength_clip": ["FLOAT", {"default": 1.0, "min": -10.0, "max": 10.0}],
            }
        },
        "input_order": {
            "required": ["model", "clip", "lora_name", "strength_model", "strength_clip"]
        },
        "output": ["MODEL", "CLIP"],
    },
    "CheckpointLoaderSimple": {
        "input": {"required": {"ckpt_name": [["sdxl.safetensors"]]}},
        "input_order": {"required": ["ckpt_name"]},
        "output": ["MODEL", "CLIP", "VAE"],
    },
    "PreviewModel": {
        "input": {"required": {"model": ["MODEL"]}},
        "input_order": {"required": ["model"]},
        "output": [],
        "output_node": True,
    },
    "PreviewClip": {
        "input": {"required": {"clip": ["CLIP"]}},
        "input_order": {"required": ["clip"]},
        "output": [],
        "output_node": True,
    },
}

BASE_GRAPH: dict[str, Any] = {
    "1": {"class_type": "Text", "inputs": {"text": "before"}},
    "2": {"class_type": "Image", "inputs": {}},
    "3": {
        "class_type": "SaveImage",
        "inputs": {"images": ["2", 0], "filename_prefix": "result"},
    },
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sdxl.safetensors"}},
    "5": {"class_type": "PreviewModel", "inputs": {"model": ["4", 0]}},
    "6": {"class_type": "PreviewClip", "inputs": {"clip": ["4", 1]}},
}


def _project(tmp_path: Path) -> Path:
    base = tmp_path / "proj"
    base.mkdir(parents=True)
    (base / "config.json").write_text(
        json.dumps({"servers": [{"id": "local", "url": "http://127.0.0.1:8188"}]}),
        encoding="utf-8",
    )
    directory = base / "data" / "local" / "portrait"
    directory.mkdir(parents=True)
    (directory / "workflow.json").write_text(json.dumps(BASE_GRAPH), encoding="utf-8")
    (directory / "schema.json").write_text(
        json.dumps(
            {
                "description": "portrait",
                "enabled": True,
                "parameters": {
                    "prompt": {"type": "string", "required": True, "node_id": "1", "field": "text"}
                },
            }
        ),
        encoding="utf-8",
    )
    return base


def _services(tmp_path: Path) -> tuple[WorkflowChangeService, SQLiteWorkflowRepository]:
    base = _project(tmp_path)
    store = SQLiteControlPlaneStore((base / "data" / "control-plane.sqlite3").resolve())
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
        SQLiteWorkflowChangeRepository(store),
        graphs,
        validation,
        actor="recipe-test",
    )
    return changes, workflows

# ---------------------------------------------------------------------------
# upscale_image.v1
# ---------------------------------------------------------------------------


def test_upscale_recipe_inserts_chain_and_rewires_consumers(tmp_path: Path) -> None:
    changes, _workflows = _services(tmp_path)

    plan = changes.plan(
        "portrait",
        "local",
        [
            {
                "op": "apply_recipe",
                "recipe_id": "upscale_image.v1",
                "arguments": {"after_node_id": "2", "model": "4x-UltraSharp.pth"},
            }
        ],
        object_info=OBJECT_INFO,
    )
    graph, schema = _after(changes, _workflows, plan)

    assert "upscale_loader_1" in graph
    assert graph["upscale_loader_1"]["inputs"]["model_name"] == "4x-UltraSharp.pth"
    assert "upscaler_1" in graph
    assert graph["upscaler_1"]["inputs"]["image"] == ["2", 0]
    # The SaveImage consumer of node 2 is rewired to the upscaler output.
    assert graph["3"]["inputs"]["images"] == ["upscaler_1", 0]
    # New chain nodes must not reference themselves.
    assert graph["upscaler_1"]["inputs"]["upscale_model"] == ["upscale_loader_1", 0]
    # Parameter exposure for the model name.
    parameters = schema["parameters"]
    assert "upscale_loader_1.model_name" in parameters
    assert parameters["upscale_loader_1.model_name"]["node_id"] == "upscale_loader_1"


def test_upscale_recipe_rejects_non_image_anchor(tmp_path: Path) -> None:
    changes, _workflows = _services(tmp_path)

    with pytest.raises(ValueError, match="must be IMAGE"):
        changes.plan(
            "portrait",
            "local",
            [
                {
                    "op": "apply_recipe",
                    "recipe_id": "upscale_image.v1",
                    "arguments": {"after_node_id": "1", "model": "4x-UltraSharp.pth"},
                }
            ],
            object_info=OBJECT_INFO,
        )


def test_upscale_recipe_rejects_unknown_class(tmp_path: Path) -> None:
    changes, _workflows = _services(tmp_path)

    with pytest.raises(ValueError, match="not in object_info"):
        changes.plan(
            "portrait",
            "local",
            [
                {
                    "op": "apply_recipe",
                    "recipe_id": "upscale_image.v1",
                    "arguments": {"after_node_id": "2", "model": "4x-UltraSharp.pth"},
                }
            ],
            object_info={
                key: value
                for key, value in OBJECT_INFO.items()
                if key != "UpscaleModelLoader"
            },  # no UpscaleModelLoader metadata
        )


# ---------------------------------------------------------------------------
# save_image.v1
# ---------------------------------------------------------------------------


def test_save_recipe_inserts_save_with_default_and_explicit_prefix(tmp_path: Path) -> None:
    changes, _workflows = _services(tmp_path)

    defaulted = changes.plan(
        "portrait",
        "local",
        [{"op": "apply_recipe", "recipe_id": "save_image.v1", "arguments": {"after_node_id": "2"}}],
        object_info=OBJECT_INFO,
    )
    graph, schema = _after(changes, _workflows, defaulted)
    assert graph["save_1"]["class_type"] == "SaveImage"
    assert graph["save_1"]["inputs"]["filename_prefix"] == "recipe"
    assert graph["save_1"]["inputs"]["images"] == ["2", 0]

    explicit = changes.plan(
        "portrait",
        "local",
        [
            {
                "op": "apply_recipe",
                "recipe_id": "save_image.v1",
                "arguments": {"after_node_id": "2", "filename_prefix": "my-output"},
            }
        ],
        object_info=OBJECT_INFO,
    )
    graph_explicit, schema_explicit = _after(changes, _workflows, explicit)
    assert graph_explicit["save_1"]["inputs"]["filename_prefix"] == "my-output"
    assert "save_1.filename_prefix" in schema_explicit["parameters"]


def test_save_recipe_rejects_non_image_anchor(tmp_path: Path) -> None:
    changes, _workflows = _services(tmp_path)

    with pytest.raises(ValueError, match="must be IMAGE"):
        changes.plan(
            "portrait",
            "local",
            [
                {
                    "op": "apply_recipe",
                    "recipe_id": "save_image.v1",
                    "arguments": {"after_node_id": "1"},
                }
            ],
            object_info=OBJECT_INFO,
        )


# ---------------------------------------------------------------------------
# lora_model.v1
# ---------------------------------------------------------------------------


def test_lora_recipe_rewires_model_and_clip_consumers(tmp_path: Path) -> None:
    changes, _workflows = _services(tmp_path)

    plan = changes.plan(
        "portrait",
        "local",
        [
            {
                "op": "apply_recipe",
                "recipe_id": "lora_model.v1",
                "arguments": {"loader_node_id": "4", "lora_name": "detail.safetensors"},
            }
        ],
        object_info=OBJECT_INFO,
    )
    graph, schema = _after(changes, _workflows, plan)

    assert graph["lora_1"]["class_type"] == "LoraLoader"
    assert graph["lora_1"]["inputs"]["model"] == ["4", 0]
    assert graph["lora_1"]["inputs"]["clip"] == ["4", 1]
    assert graph["lora_1"]["inputs"]["strength_model"] == 1.0  # default
    assert graph["lora_1"]["inputs"]["strength_clip"] == 1.0  # default
    assert graph["5"]["inputs"]["model"] == ["lora_1", 0]  # MODEL consumer rewired
    assert graph["6"]["inputs"]["clip"] == ["lora_1", 1]  # CLIP consumer rewired
    parameters = schema["parameters"]
    assert "lora_1.lora_name" in parameters
    assert "lora_1.strength_model" in parameters
    assert "lora_1.strength_clip" in parameters
    assert plan["diff"]["nodes_added"] == ["lora_1"]


def test_lora_recipe_rejects_loader_without_clip_output(tmp_path: Path) -> None:
    changes, _workflows = _services(tmp_path)
    info = {
        **OBJECT_INFO,
        "CheckpointLoaderSimple": {
            **OBJECT_INFO["CheckpointLoaderSimple"],
            "output": ["MODEL"],
        },
    }

    with pytest.raises(ValueError, match="must be CLIP"):
        changes.plan(
            "portrait",
            "local",
            [
                {
                    "op": "apply_recipe",
                    "recipe_id": "lora_model.v1",
                    "arguments": {"loader_node_id": "4", "lora_name": "detail.safetensors"},
                }
            ],
            object_info=info,
        )


# ---------------------------------------------------------------------------
# registration contract
# ---------------------------------------------------------------------------


def test_recipe_contract_validation(tmp_path: Path) -> None:
    changes, _workflows = _services(tmp_path)
    graph = _graph_copy(BASE_GRAPH)
    schema: dict[str, Any] = {"parameters": {}}

    with pytest.raises(ValueError, match="not registered"):
        apply_recipe(graph, schema, "unknown.v1", {}, OBJECT_INFO, index=0)
    with pytest.raises(ValueError, match="arguments is invalid"):
        apply_recipe(
            graph, schema, "upscale_image.v1", {"after_node_id": "2"}, OBJECT_INFO, index=0
        )
    with pytest.raises(ValueError, match="arguments is invalid"):
        apply_recipe(
            graph,
            schema,
            "upscale_image.v1",
            {"after_node_id": "2", "model": "m", "extra": 1},
            OBJECT_INFO,
            index=0,
        )
    # Optional keys are accepted with defaults.
    apply_recipe(graph, schema, "save_image.v1", {"after_node_id": "2"}, OBJECT_INFO, index=0)
    assert graph["save_1"]["inputs"]["filename_prefix"] == "recipe"


def test_recipe_repeated_application_uses_stable_distinct_ids(tmp_path: Path) -> None:
    changes, _workflows = _services(tmp_path)
    first = changes.plan(
        "portrait",
        "local",
        [{"op": "apply_recipe", "recipe_id": "save_image.v1", "arguments": {"after_node_id": "2"}}],
        object_info=OBJECT_INFO,
    )
    committed_first = changes.commit(first["plan_id"], first["plan_digest"])
    _workflows.publish(committed_first["deployment_id"])
    second = changes.plan(
        "portrait",
        "local",
        [{"op": "apply_recipe", "recipe_id": "save_image.v1", "arguments": {"after_node_id": "2"}}],
        object_info=OBJECT_INFO,
    )
    graph_first, schema_first = _after(changes, _workflows, first)
    graph_second, schema_second = _after(changes, _workflows, second)
    assert "save_1" in graph_first and "save_2" in graph_second
    assert "save_1.filename_prefix" in schema_first["parameters"]
    assert "save_2.filename_prefix" in schema_second["parameters"]


def test_set_scalar_preserves_exposed_parameters_and_accepts_optional_input(
    tmp_path: Path,
) -> None:
    changes, _workflows = _services(tmp_path)
    # Expose the prompt parameter and publish it, then update through the
    # legacy recipe on top of the exposed revision.
    exposed = changes.plan(
        "portrait",
        "local",
        [
            {
                "op": "expose_parameter",
                "node_id": "1",
                "field": "text",
                "name": "my_prompt",
                "required": True,
            }
        ],
        object_info=OBJECT_INFO,
    )
    committed_exposed = changes.commit(exposed["plan_id"], exposed["plan_digest"])
    _workflows.publish(committed_exposed["deployment_id"])
    plan = changes.plan(
        "portrait",
        "local",
        [
            {
                "op": "apply_recipe",
                "recipe_id": "set_scalar_input.v1",
                "arguments": {"node_id": "1", "field": "text", "value": "after"},
            }
        ],
        object_info=OBJECT_INFO,
    )
    graph, schema = _after(changes, _workflows, plan)
    assert graph["1"]["inputs"]["text"] == "after"
    assert "my_prompt" in schema["parameters"]  # exposed parameter preserved
    # Optional input the graph omits is settable (object_info decides legality).
    optional_plan = changes.plan(
        "portrait",
        "local",
        [
            {
                "op": "apply_recipe",
                "recipe_id": "set_scalar_input.v1",
                "arguments": {"node_id": "2", "field": "optional_text", "value": "x"},
            }
        ],
        object_info={
            **OBJECT_INFO,
            "Image": {
                "input": {"required": {}, "optional": {"optional_text": ["STRING"]}},
                "input_order": {"required": [], "optional": ["optional_text"]},
                "output": ["IMAGE"],
            },
        },
    )
    assert _after(changes, _workflows, optional_plan)[0]["2"]["inputs"]["optional_text"] == "x"


def test_set_scalar_rejects_non_string_ids_and_non_finite_strength() -> None:
    from comfyui_mcp_skills.application.workflow_recipes import apply_recipe

    graph = _graph_copy(BASE_GRAPH)
    schema: dict[str, Any] = {"parameters": {}}
    with pytest.raises(ValueError, match="bounded string"):
        apply_recipe(
            graph,
            schema,
            "set_scalar_input.v1",
            {"node_id": 5, "field": "text", "value": "x"},
            OBJECT_INFO,
            index=0,
        )
    with pytest.raises(ValueError, match="finite number"):
        apply_recipe(
            graph,
            schema,
            "lora_model.v1",
            {
                "loader_node_id": "4",
                "lora_name": "detail.safetensors",
                "strength_model": float("inf"),
            },
            OBJECT_INFO,
            index=0,
        )


def test_recipe_rejects_duplicate_parameter_name(tmp_path: Path) -> None:
    from comfyui_mcp_skills.application.workflow_recipes import apply_recipe

    graph = _graph_copy(BASE_GRAPH)
    schema: dict[str, Any] = {
        "parameters": {"save_1.filename_prefix": {"type": "string", "node_id": "save_1"}}
    }
    with pytest.raises(ValueError, match="already exists"):
        apply_recipe(
            graph,
            schema,
            "save_image.v1",
            {"after_node_id": "2"},
            OBJECT_INFO,
            index=0,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _after(
    changes: WorkflowChangeService,
    workflows: SQLiteWorkflowRepository,
    plan: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    committed = changes.commit(plan["plan_id"], plan["plan_digest"])
    revision = workflows.get_revision(committed["revision_id"])
    return _graph_copy(revision["graph"]), _graph_copy(revision["parameter_schema"])


def _graph_copy(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False))
