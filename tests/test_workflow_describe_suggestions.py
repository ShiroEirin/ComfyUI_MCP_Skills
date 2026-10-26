"""describe(suggest_parameters=true) reads the Revision's real parameter schema."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from comfyui_mcp_skills.adapters.mcp.server import create_server
from comfyui_mcp_skills.application.authorization import AuthorizationContext, Scope, Toolset
from comfyui_mcp_skills.application.workflow_change import WorkflowChangeService
from comfyui_mcp_skills.application.workflow_graph import (
    WorkflowGraphService,
    WorkflowValidationService,
)
from comfyui_mcp_skills.application.workflow_import import WorkflowImportService
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
    "CheckpointLoaderSimple": {
        "input": {"required": {"ckpt_name": [["sdxl.safetensors"]]}},
        "input_order": {"required": ["ckpt_name"]},
        "output": ["MODEL", "CLIP", "VAE"],
    },
    "KSampler": {
        "input": {
            "required": {
                "seed": ["INT", {"min": 0, "max": 2**63 - 1}],
                "steps": ["INT", {"min": 1, "max": 10_000}],
                "cfg": ["FLOAT", {"min": 0.0, "max": 100.0}],
                "sampler_name": [["euler", "dpmpp_2m"]],
                "scheduler": [["normal", "karras"]],
                "denoise": ["FLOAT", {"min": 0.0, "max": 1.0}],
                "model": ["MODEL"],
            }
        },
        "input_order": {
            "required": [
                "seed",
                "steps",
                "cfg",
                "sampler_name",
                "scheduler",
                "denoise",
                "model",
            ]
        },
        "output": ["LATENT"],
    },
}

# The import auto-exposes role inputs (prompt, ckpt_name, steps). KSampler's
# seed/cfg/sampler/scheduler/denoise carry no role, so they stay suggestions --
# which is exactly what makes them prove the stored schema is read correctly.
GRAPH: dict[str, Any] = {
    "1": {"class_type": "Text", "inputs": {"text": "before"}},
    "4": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "sdxl.safetensors"},
    },
    "7": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 1,
            "steps": 20,
            "cfg": 7.0,
            "sampler_name": "euler",
            "scheduler": "normal",
            "denoise": 1.0,
            "model": ["4", 0],
        },
    },
    "2": {"class_type": "Image", "inputs": {}},
    "3": {
        "class_type": "SaveImage",
        "inputs": {"images": ["2", 0], "filename_prefix": "result"},
    },
}


class _Gateway:
    def get_object_info(self) -> dict[str, Any]:
        return OBJECT_INFO

    def get_models(self, folder: str) -> list[str]:
        return []


def _published_store(tmp_path: Path) -> tuple[SQLiteControlPlaneStore, SQLiteWorkflowRepository]:
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "default_server": "local",
                "servers": [{"id": "local", "url": "http://127.0.0.1:8188"}],
            }
        ),
        encoding="utf-8",
    )
    store = SQLiteControlPlaneStore((tmp_path / "data" / "control-plane.sqlite3").resolve())
    store.initialize()
    workflows = SQLiteWorkflowRepository(store)
    graphs = WorkflowGraphService(
        ParameterRoleRegistry.default(), DependencyExtractorRegistry.default()
    )
    validation = WorkflowValidationService()
    importer = WorkflowImportService(graphs, validation, workflows)
    created = importer.commit(
        importer.preview(
            GRAPH,
            workflow_id="portrait",
            server_id="local",
            object_info=OBJECT_INFO,
        )
    )
    workflows.publish(created["deployment_id"])
    return store, workflows


def _server(
    tmp_path: Path, store: SQLiteControlPlaneStore, workflows: SQLiteWorkflowRepository
) -> Any:
    repositories = RepositoryBundle(
        workflows=workflows,
        runs=FileRunRepository(tmp_path),
        assets=FileAssetRepository(tmp_path),
        workflow_store="sqlite",
        run_store="file",
        asset_store="file",
        store=store,
    )
    return create_server(
        tmp_path,
        repositories=repositories,
        gateway_factory=lambda _config: _Gateway(),
        authorization=AuthorizationContext(
            "suggest-agent", frozenset({Scope.OBSERVE, Scope.AUTHOR}), Toolset.AUTHORING
        ),
    )


def _targets(suggestions: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(str(item["node_id"]), str(item["field"])) for item in suggestions}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_describe_suggestions_read_the_stored_parameter_schema(tmp_path: Path) -> None:
    store, workflows = _published_store(tmp_path)
    revision = workflows.get_revision(workflows.describe("portrait", "local")["revision_id"])
    # The Revision keeps its parameters under parameter_schema["parameters"].
    stored = revision["parameter_schema"]["parameters"]
    assert ("prompt" in stored) and ("steps" in stored)
    assert "parameters" not in revision

    server = _server(tmp_path, store, workflows)
    async with Client(server) as client:
        described = await client.call_tool(
            "comfyui.workflow.describe",
            {"workflow_id": "portrait", "server_id": "local", "suggest_parameters": True},
        )

    suggestions = described.structured_content["parameter_suggestions"]
    targets = _targets(suggestions)
    # Role inputs auto-exposed by the import are never suggested twice.
    assert ("7", "steps") not in targets
    assert ("7", "seed") not in targets
    # Inputs without a role stay suggestions, which only holds when the stored
    # schema is read from parameter_schema["parameters"].
    assert {("7", "cfg"), ("7", "sampler_name"), ("4", "ckpt_name")} <= targets


@pytest.mark.anyio
async def test_describe_suggestions_skip_a_manually_exposed_target(tmp_path: Path) -> None:
    """An expose_parameter target must stop being suggested (stored schema wins)."""
    store, workflows = _published_store(tmp_path)
    graphs = WorkflowGraphService(
        ParameterRoleRegistry.default(), DependencyExtractorRegistry.default()
    )
    changes = WorkflowChangeService(
        SQLiteWorkflowChangeRepository(store),
        graphs,
        WorkflowValidationService(),
        actor="suggest-agent",
    )
    plan = changes.plan(
        "portrait",
        "local",
        [{"op": "expose_parameter", "node_id": "7", "field": "cfg", "name": "guidance"}],
        object_info=OBJECT_INFO,
    )
    committed = changes.commit(plan["plan_id"], plan["plan_digest"])
    workflows.publish(committed["deployment_id"])

    server = _server(tmp_path, store, workflows)
    async with Client(server) as client:
        described = await client.call_tool(
            "comfyui.workflow.describe",
            {"workflow_id": "portrait", "server_id": "local", "suggest_parameters": True},
        )

    stored = described.structured_content["parameter_schema"]["parameters"]
    assert stored["guidance"]["node_id"] == "7"
    targets = _targets(described.structured_content["parameter_suggestions"])
    assert ("7", "cfg") not in targets
    assert ("7", "sampler_name") in targets
