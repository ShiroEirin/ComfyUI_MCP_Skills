"""Declared parameter metadata follows the graph inputs a change plan rewrites."""

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
        "input": {"required": {"ckpt_name": [["sdxl.safetensors", "detail.safetensors"]]}},
        "input_order": {"required": ["ckpt_name"]},
        "output": ["MODEL", "CLIP", "VAE"],
    },
}

GRAPH: dict[str, Any] = {
    "1": {"class_type": "Text", "inputs": {"text": "before"}},
    "2": {"class_type": "Image", "inputs": {}},
    "3": {
        "class_type": "SaveImage",
        "inputs": {"images": ["2", 0], "filename_prefix": "result"},
    },
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sdxl.safetensors"}},
}


def _store(tmp_path: Path) -> SQLiteControlPlaneStore:
    store = SQLiteControlPlaneStore((tmp_path / "control-plane.sqlite3").resolve())
    store.initialize()
    return store


def _services(
    tmp_path: Path,
) -> tuple[WorkflowChangeService, SQLiteWorkflowRepository, SQLiteControlPlaneStore]:
    store = _store(tmp_path)
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
    changes = WorkflowChangeService(
        SQLiteWorkflowChangeRepository(store), graphs, validation, actor="defaults-test"
    )
    return changes, workflows, store


def _commit(
    changes: WorkflowChangeService,
    workflows: SQLiteWorkflowRepository,
    operations: list[dict[str, Any]],
    *,
    object_info: dict[str, Any] | None = None,
    publish: bool = True,
) -> dict[str, Any]:
    plan = changes.plan(
        "portrait",
        "local",
        operations,
        object_info=object_info or OBJECT_INFO,
    )
    committed = changes.commit(plan["plan_id"], plan["plan_digest"])
    if publish:
        workflows.publish(committed["deployment_id"])
    return committed


def _parameters(workflows: SQLiteWorkflowRepository, revision_id: str) -> dict[str, Any]:
    revision = workflows.get_revision(revision_id)
    return revision["parameter_schema"]["parameters"]


def _published_revision(workflows: SQLiteWorkflowRepository) -> str:
    return workflows.describe("portrait", "local")["revision_id"]


def test_exposed_default_follows_the_rewritten_input(tmp_path: Path) -> None:
    changes, workflows, _store_handle = _services(tmp_path)
    _commit(
        changes,
        workflows,
        [
            {
                "op": "expose_parameter",
                "node_id": "1",
                "field": "text",
                "name": "prompt_text",
                "required": True,
            }
        ],
    )
    assert _parameters(workflows, _published_revision(workflows))["prompt_text"]["default"] == (
        "before"
    )

    committed = _commit(
        changes,
        workflows,
        [{"op": "set_input", "node_id": "1", "field": "text", "value": "after"}],
    )

    parameters = _parameters(workflows, committed["revision_id"])
    assert parameters["prompt_text"]["default"] == "after"


def test_untouched_explicit_default_is_preserved(tmp_path: Path) -> None:
    changes, workflows, _store_handle = _services(tmp_path)
    _commit(
        changes,
        workflows,
        [
            {
                "op": "expose_parameter",
                "node_id": "1",
                "field": "text",
                "name": "prompt_text",
                "required": True,
            },
            {
                "op": "expose_parameter",
                "node_id": "3",
                "field": "filename_prefix",
                "name": "prefix",
            },
        ],
    )

    committed = _commit(
        changes,
        workflows,
        [{"op": "set_input", "node_id": "1", "field": "text", "value": "after"}],
    )

    parameters = _parameters(workflows, committed["revision_id"])
    assert parameters["prompt_text"]["default"] == "after"
    # The untouched target keeps the default it declared.
    assert parameters["prefix"]["default"] == "result"


def test_enum_follows_the_node_definition_when_the_input_changes(tmp_path: Path) -> None:
    changes, workflows, _store_handle = _services(tmp_path)
    _commit(
        changes,
        workflows,
        [
            {
                "op": "expose_parameter",
                "node_id": "4",
                "field": "ckpt_name",
                "name": "checkpoint",
                "required": True,
            }
        ],
    )
    parameters = _parameters(workflows, _published_revision(workflows))
    assert parameters["checkpoint"]["enum"] == ["sdxl.safetensors", "detail.safetensors"]
    assert parameters["checkpoint"]["default"] == "sdxl.safetensors"

    updated_info = {
        **OBJECT_INFO,
        "CheckpointLoaderSimple": {
            "input": {"required": {"ckpt_name": [["detail.safetensors", "wai.safetensors"]]}},
            "input_order": {"required": ["ckpt_name"]},
            "output": ["MODEL", "CLIP", "VAE"],
        },
    }
    committed = _commit(
        changes,
        workflows,
        [{"op": "set_input", "node_id": "4", "field": "ckpt_name", "value": "detail.safetensors"}],
        object_info=updated_info,
    )

    parameters = _parameters(workflows, committed["revision_id"])
    assert parameters["checkpoint"]["default"] == "detail.safetensors"
    assert parameters["checkpoint"]["enum"] == ["detail.safetensors", "wai.safetensors"]


def test_restoring_the_previous_value_keeps_revision_identity(tmp_path: Path) -> None:
    changes, workflows, _store_handle = _services(tmp_path)
    base_revision = _published_revision(workflows)
    _commit(
        changes,
        workflows,
        [{"op": "set_input", "node_id": "1", "field": "text", "value": "after"}],
    )

    restored = _commit(
        changes,
        workflows,
        [{"op": "set_input", "node_id": "1", "field": "text", "value": "before"}],
    )

    assert restored["revision_id"] == base_revision


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Gateway:
    def get_object_info(self) -> dict[str, Any]:
        return OBJECT_INFO

    def get_models(self, folder: str) -> list[str]:
        return []


@pytest.mark.anyio
async def test_describe_reports_the_synced_default(tmp_path: Path) -> None:
    changes, workflows, store = _services(tmp_path)
    _commit(
        changes,
        workflows,
        [
            {
                "op": "expose_parameter",
                "node_id": "1",
                "field": "text",
                "name": "prompt_text",
                "required": True,
            }
        ],
    )
    _commit(
        changes,
        workflows,
        [{"op": "set_input", "node_id": "1", "field": "text", "value": "after"}],
    )
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "default_server": "local",
                "servers": [{"id": "local", "url": "http://127.0.0.1:8188"}],
            }
        ),
        encoding="utf-8",
    )
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
            "defaults-agent", frozenset({Scope.OBSERVE, Scope.AUTHOR}), Toolset.AUTHORING
        ),
    )

    async with Client(server) as client:
        described = await client.call_tool(
            "comfyui.workflow.describe", {"workflow_id": "portrait", "server_id": "local"}
        )

    parameters = described.structured_content["parameter_schema"]["parameters"]
    assert parameters["prompt_text"]["default"] == "after"
