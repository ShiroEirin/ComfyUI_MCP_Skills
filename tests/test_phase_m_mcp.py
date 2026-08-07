"""Focused MCP Phase M experiment surface contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError

from comfyui_mcp_skills.adapters.mcp.orchestration import OrchestrationRuntime
from comfyui_mcp_skills.adapters.mcp.prompts import create_prompt_handlers
from comfyui_mcp_skills.adapters.mcp.server import create_server
from comfyui_mcp_skills.adapters.mcp.tooling import phase_m_tools
from comfyui_mcp_skills.application.auth_context import reset_authorization, set_authorization
from comfyui_mcp_skills.application.authorization import (
    AuthorizationContext,
    Scope,
    Toolset,
    admitted_scopes,
    scopes_for_resource,
    scopes_for_tool,
    tool_visible,
)
from comfyui_mcp_skills.application.experiments import ExperimentService
from comfyui_mcp_skills.infrastructure.persistence.assets import FileAssetRepository
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.runs import FileRunRepository
from comfyui_mcp_skills.infrastructure.persistence.workflows import FileWorkflowRepository

PHASE_M_NAMES = {
    "comfyui.experiment.plan",
    "comfyui.experiment.commit",
    "comfyui.experiment.get",
    "comfyui.experiment.cancel",
    "comfyui.experiment.variant.list",
    "comfyui.experiment.variant.rate",
    "comfyui.experiment.variant.promote",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_phase_m_declares_exact_bounded_execute_tools() -> None:
    tools = {tool.name: tool for tool in phase_m_tools()}

    assert set(tools) == PHASE_M_NAMES
    assert len(tools) == 7
    assert all(tool.input_schema["additionalProperties"] is False for tool in tools.values())
    assert tools["comfyui.experiment.plan"].input_schema["properties"]["expansion"][
        "type"
    ] == "object"
    assert (
        tools["comfyui.experiment.plan"].input_schema["properties"]["budgets"][
            "additionalProperties"
        ]
        is False
    )
    plan_schema = tools["comfyui.experiment.plan"].input_schema
    assert plan_schema["properties"]["concurrency"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 64,
        "default": 1,
    }
    assert plan_schema["properties"]["submission_window"]["default"] == 0
    assert "concurrency" not in plan_schema["required"]
    assert "submission_window" not in plan_schema["required"]
    assert tools["comfyui.experiment.variant.list"].input_schema["properties"]["limit"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
        "default": 50,
    }
    # propertyNames/minProperties/maxProperties are not harness-compatible;
    # score-name and count bounds are enforced by validate_rating_scores
    # against the registered rubric instead.
    assert (
        tools["comfyui.experiment.variant.rate"].input_schema["properties"]["scores"][
            "additionalProperties"
        ]
        == {"type": "number", "minimum": -1_000_000, "maximum": 1_000_000}
    )
    assert tools["comfyui.experiment.variant.promote"].input_schema["properties"]["target"] == {
        "type": "string",
        "enum": ["preset", "revision"],
    }
    assert tools["comfyui.experiment.cancel"].annotations.destructive_hint is True
    assert tools["comfyui.experiment.get"].annotations.read_only_hint is True
    assert tools["comfyui.experiment.variant.list"].annotations.read_only_hint is True


def test_phase_m_authorization_is_execute_only() -> None:
    assert all(scopes_for_tool(name) == frozenset({Scope.EXECUTE}) for name in PHASE_M_NAMES)
    assert all(
        tool_visible(name, Toolset.EXECUTION, admitted_scopes(Toolset.EXECUTION))
        for name in PHASE_M_NAMES
    )
    assert all(
        not tool_visible(name, Toolset.AUTHORING, admitted_scopes(Toolset.AUTHORING))
        for name in PHASE_M_NAMES
    )
    assert scopes_for_resource("experiment") == frozenset({Scope.EXECUTE})
    assert scopes_for_resource("variant") == frozenset({Scope.EXECUTE})


def test_experiment_projections_never_inline_variants_arguments_or_graphs() -> None:
    from comfyui_mcp_skills.adapters.mcp.tooling import experiment_dict, variant_page_dict

    experiment = experiment_dict(
        {
            "experiment_id": "experiment_" + "a" * 32,
            "status": "running",
            "variant_count": 1_000,
            "resource_uri": "comfyui://experiments/experiment_" + "a" * 32,
            "variants": [{"arguments": {"prompt": "secret"}}],
            "base_arguments": {"input_path": "C:/private/input.png"},
            "graph": {"nodes": []},
        }
    )
    variants = variant_page_dict(
        {
            "items": [
                {
                    "experiment_id": "experiment_" + "a" * 32,
                    "variant_id": "variant_" + "b" * 32,
                    "ordinal": 0,
                    "parameter_digest": "digest",
                    "status": "completed",
                    "resource_uri": "comfyui://experiments/experiment_"
                    + "a" * 32
                    + "/variants/variant_"
                    + "b" * 32,
                    "arguments": {"prompt": "secret"},
                    "graph": {"nodes": []},
                    "host_path": "C:/private/output.png",
                }
            ],
            "next_cursor": "cursor",
        }
    )

    assert set(experiment) == {"experiment_id", "status", "variant_count", "resource_uri"}
    assert set(variants) == {"items", "next_cursor"}
    assert set(variants["items"][0]) == {
        "experiment_id",
        "variant_id",
        "ordinal",
        "parameter_digest",
        "status",
        "resource_uri",
    }


def test_experiment_tool_results_include_stable_resource_link() -> None:
    from comfyui_mcp_skills.adapters.mcp.tooling import tool_result

    uri = "comfyui://experiments/experiment_" + "a" * 32
    result = tool_result({"experiment_id": "experiment_" + "a" * 32, "resource_uri": uri})

    assert result.structured_content["resource_uri"] == uri
    assert [block.uri for block in result.content if block.type == "resource_link"] == [uri]


@pytest.mark.anyio
async def test_experiment_outbox_publishes_owned_resource_update() -> None:
    uri = "comfyui://experiments/experiment_" + "a" * 32

    class Repository:
        def __init__(self) -> None:
            self.delivered: list[str] = []

        def pending_outbox(self) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(
                    outbox_id="outbox-1",
                    payload={"uri": uri, "owner_id": "owner-a"},
                )
            ]

        def mark_outbox_delivered(self, outbox_id: str, *, now: object) -> None:
            self.delivered.append(outbox_id)

        def job_owner_for_uri(self, _uri: str) -> str | None:
            raise AssertionError("Experiment updates must not use Job owner lookup")

    class Bus:
        def __init__(self) -> None:
            self.events: list[object] = []

        async def publish(self, event: object) -> None:
            self.events.append(event)

    repository = Repository()
    bus = Bus()
    runtime = OrchestrationRuntime(
        SimpleNamespace(run_once=lambda *_args, **_kwargs: False),
        repository,
        bus,
        worker_id="worker",
        owner_for_uri=lambda value: "owner-a" if value == uri else None,
    )

    assert await runtime.dispatch_outbox_once() == 1
    assert [event.uri for event in bus.events] == [uri]
    assert repository.delivered == ["outbox-1"]


@pytest.mark.anyio
async def test_compare_experiment_prompt_is_bounded_and_non_looping() -> None:
    handlers = create_prompt_handlers()
    listed = await handlers.list_prompts(None, None)
    assert "compare-experiment-results" in {prompt.name for prompt in listed.prompts}

    rendered = await handlers.get_prompt(
        None,
        type(
            "Params",
            (),
            {
                "name": "compare-experiment-results",
                "arguments": {"experiment_id": "experiment_" + "a" * 32},
            },
        )(),
    )
    text = rendered.messages[0].content.text
    assert "comfyui.experiment.get" in text
    assert "comfyui.experiment.variant.list" in text
    assert "limit" in text and "100" in text
    assert "one page" in text
    assert "Do not loop" in text


@pytest.mark.anyio
async def test_sqlite_experiment_tools_dispatch_owner_bound_calls_and_resource_links(
    tmp_path: Path,
) -> None:
    (tmp_path / "config.json").write_text('{"servers":[]}', encoding="utf-8")
    store = SQLiteControlPlaneStore(tmp_path / "data" / "control-plane.sqlite3")
    store.initialize()
    repository = SimpleNamespace(resource_owner_for_uri=lambda _uri: "local-stdio")
    repositories = SimpleNamespace(
        workflows=FileWorkflowRepository(tmp_path),
        runs=FileRunRepository(tmp_path),
        assets=FileAssetRepository(tmp_path),
        workflow_store="sqlite",
        run_store="sqlite",
        asset_store="file",
        store=store,
        experiments=repository,
    )
    experiment_id = "experiment_" + "a" * 32
    variant_id = "variant_" + "b" * 32
    experiment_uri = f"comfyui://experiments/{experiment_id}"
    variant_uri = f"{experiment_uri}/variants/{variant_id}"
    calls: list[tuple[str, tuple[object, ...]]] = []

    def record(name: str, result: dict[str, object]):
        def operation(_self: object, *arguments: object) -> dict[str, object]:
            calls.append((name, arguments))
            return dict(result)

        return operation

    experiment = {
        "experiment_id": experiment_id,
        "status": "running",
        "variant_count": 2,
        "resource_uri": experiment_uri,
    }
    variant = {
        "experiment_id": experiment_id,
        "variant_id": variant_id,
        "ordinal": 0,
        "parameter_digest": "c" * 64,
        "status": "completed",
        "resource_uri": variant_uri,
    }
    page = {"items": [variant], "next_cursor": ""}
    rating = {
        "rating_id": "rating_" + "e" * 64,
        "experiment_id": experiment_id,
        "variant_id": variant_id,
        "rubric_version": "visual-v1",
        "scores": {"composition": 0.8},
        "created_at": "2026-08-03T00:00:00Z",
    }
    promotion = {
        "promotion_id": "promotion_" + "f" * 64,
        "experiment_id": experiment_id,
        "variant_id": variant_id,
        "target": "preset",
        "preset_id": "preset_" + "f" * 64,
        "created_at": "2026-08-03T00:00:00Z",
    }
    patches = (
        patch.object(ExperimentService, "plan", record("plan", experiment)),
        patch.object(ExperimentService, "commit", record("commit", experiment)),
        patch.object(ExperimentService, "get", record("get", experiment)),
        patch.object(ExperimentService, "cancel", record("cancel", experiment)),
        patch.object(ExperimentService, "list_variants", record("list", page)),
        patch.object(ExperimentService, "rate", record("rate", rating)),
        patch.object(ExperimentService, "promote", record("promote", promotion)),
        patch(
            "comfyui_mcp_skills.adapters.mcp.server.get_experiment_variant",
            lambda _service, _experiment_id, _variant_id, _owner_id: dict(variant),
        ),
    )
    for context in patches:
        context.start()
    try:
        server = create_server(tmp_path, repositories=repositories)
        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert PHASE_M_NAMES <= names
            template_uris = {
                template.uri_template
                for template in (await client.list_resource_templates()).resource_templates
            }
            assert {
                "comfyui://experiments/{experiment_id}",
                "comfyui://experiments/{experiment_id}/variants/{variant_id}",
            } <= template_uris
            experiment_resource = await client.read_resource(experiment_uri)
            variant_resource = await client.read_resource(variant_uri)
            planned = await client.call_tool(
                "comfyui.experiment.plan",
                {
                    "workflow_id": "portrait",
                    "server_id": "local",
                    "expansion": {"mode": "matrix", "parameters": {"seed": [1, 2]}},
                    "base_arguments": {"width": 64, "height": 64},
                    "budgets": {
                        "max_variants": 2,
                        "max_concurrency": 1,
                        "max_pixels": 8192,
                        "max_outputs": 2,
                        "max_seconds": 60,
                    },
                    "failure_policy": "continue",
                },
            )
            committed = await client.call_tool(
                "comfyui.experiment.commit",
                {"plan_id": "experiment_plan_" + "d" * 64, "plan_digest": "d" * 64},
            )
            fetched = await client.call_tool(
                "comfyui.experiment.get", {"experiment_id": experiment_id}
            )
            await client.call_tool(
                "comfyui.experiment.cancel",
                {"experiment_id": experiment_id, "mode": "stop_new"},
            )
            listed = await client.call_tool(
                "comfyui.experiment.variant.list",
                {"experiment_id": experiment_id, "limit": 1, "cursor": ""},
            )
            rated = await client.call_tool(
                "comfyui.experiment.variant.rate",
                {
                    "experiment_id": experiment_id,
                    "variant_id": variant_id,
                    "rubric_version": "visual-v1",
                    "scores": {"composition": 0.8},
                },
            )
            promoted = await client.call_tool(
                "comfyui.experiment.variant.promote",
                {
                    "experiment_id": experiment_id,
                    "variant_id": variant_id,
                    "target": "preset",
                },
            )
            invalid = await client.call_tool(
                "comfyui.experiment.variant.list",
                {"experiment_id": experiment_id, "limit": 101},
            )
    finally:
        for context in reversed(patches):
            context.stop()

    assert [name for name, _arguments in calls] == [
        "get",
        "plan",
        "commit",
        "get",
        "cancel",
        "list",
        "rate",
        "promote",
    ]
    assert json.loads(experiment_resource.contents[0].text) == experiment
    assert json.loads(variant_resource.contents[0].text) == variant
    assert calls[1][1][0] == "local-stdio"
    assert calls[1][1][-2:] == (1, 0)
    assert calls[3][1] == (experiment_id, "local-stdio")
    assert listed.structured_content == page
    assert invalid.is_error is True
    assert rated.structured_content == rating
    assert promoted.structured_content == promotion
    for result in (planned, committed, fetched):
        assert [block.uri for block in result.content if block.type == "resource_link"] == [
            experiment_uri
        ]


@pytest.mark.anyio
async def test_experiment_subscription_rejects_another_owner(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text('{"servers":[]}', encoding="utf-8")
    store = SQLiteControlPlaneStore(tmp_path / "data" / "control-plane.sqlite3")
    store.initialize()
    repository = SimpleNamespace(resource_owner_for_uri=lambda _uri: "victim")
    repositories = SimpleNamespace(
        workflows=FileWorkflowRepository(tmp_path),
        runs=FileRunRepository(tmp_path),
        assets=FileAssetRepository(tmp_path),
        workflow_store="sqlite",
        run_store="sqlite",
        asset_store="file",
        store=store,
        experiments=repository,
    )
    authorization = AuthorizationContext("attacker", frozenset({Scope.EXECUTE}), Toolset.EXECUTION)
    server = create_server(
        tmp_path,
        repositories=repositories,
        authorization=authorization,
    )
    token = set_authorization(authorization)
    try:
        async with Client(server) as client:
            with pytest.raises(MCPError, match="Resource unavailable"):
                async with client.listen(
                    resource_subscriptions=["comfyui://experiments/experiment_" + "a" * 32]
                ):
                    pass
    finally:
        reset_authorization(token)


@pytest.mark.anyio
async def test_file_backend_hides_phase_m_tools_and_rejects_calls(tmp_path: Path) -> None:
    server = create_server(tmp_path)

    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert not (names & PHASE_M_NAMES)
        prompt_names = {prompt.name for prompt in (await client.list_prompts()).prompts}
        template_uris = {
            template.uri_template
            for template in (await client.list_resource_templates()).resource_templates
        }
        assert "compare-experiment-results" not in prompt_names
        assert not any(uri.startswith("comfyui://experiments/") for uri in template_uris)
        capabilities = await client.call_tool(
            "comfyui.capability.search", {"query": "experiment", "limit": 50}
        )
        discovered = {item["name"] for item in capabilities.structured_content.get("items", [])}
        assert not (discovered & PHASE_M_NAMES)
        with pytest.raises(MCPError, match="Unknown tool"):
            await client.call_tool("comfyui.experiment.get", {"experiment_id": "experiment_x"})
