"""G6 catalog, evaluation, and compatibility acceptance contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from comfyui_mcp_skills.adapters.mcp.tooling import fixed_tools
from comfyui_mcp_skills.application.authorization import (
    AuthorizationContext,
    Scope,
    Toolset,
    admitted_scopes,
    tool_visible,
)
from comfyui_mcp_skills.application.capabilities import (
    CapabilityCatalog,
    ToolInventory,
)
from comfyui_mcp_skills.application.compatibility import (
    COMPATIBILITY_MATRIX,
    HostCapabilities,
    host_fallbacks,
)
from comfyui_mcp_skills.application.evaluation import (
    EvalCase,
    EvalTrial,
    evaluate_trials,
)
from comfyui_mcp_skills.application.jobs import JobService
from comfyui_mcp_skills.deepseek_eval_main import run_deepseek_baseline, selected_tool
from comfyui_mcp_skills.eval_main import run


def test_fixed_tool_inventory_has_uniform_metadata_and_stays_within_budget() -> None:
    tools = fixed_tools()
    inventory = ToolInventory(tools)

    assert inventory.fixed_count <= 16
    assert all(tool.title for tool in tools)
    assert all(tool.icons and tool.icons[0].src.startswith("data:image/svg+xml,") for tool in tools)
    assert all(tool.annotations is not None for tool in tools)
    assert all(
        tool.meta and tool.meta.get("comfyui/risk") in {"low", "medium", "high"} for tool in tools
    )

    by_name = {tool.name: tool for tool in tools}
    for name in ("comfyui.asset.upload", "comfyui.job.get", "comfyui.job.cancel"):
        assert by_name[name].annotations is not None
        assert by_name[name].annotations.open_world_hint is True


def test_dynamic_inventory_is_deterministic_and_capped_without_mutating_catalog() -> None:
    inventory = ToolInventory(fixed_tools())
    names = [f"comfyui.run.local.workflow-{index:02d}" for index in range(20, -1, -1)]

    first = inventory.select_dynamic(names)
    second = inventory.select_dynamic(list(reversed(names)))

    assert first == second == tuple(sorted(names)[:8])
    assert inventory.fixed_names == tuple(tool.name for tool in fixed_tools())


def test_dynamic_inventory_accepts_an_explicit_bounded_budget() -> None:
    names = [f"comfyui.run.local.workflow-{index:03d}" for index in range(200)]

    inventory = ToolInventory(fixed_tools(), max_dynamic_limit=128)

    assert inventory.select_dynamic(names) == tuple(sorted(names)[:128])
    assert ToolInventory.DYNAMIC_LIMIT == 8
    with pytest.raises(ValueError, match="max_dynamic_limit must be between 1 and 128"):
        ToolInventory(fixed_tools(), max_dynamic_limit=129)


def test_each_authorized_fixed_toolset_stays_within_default_budget() -> None:
    tools = fixed_tools()

    for toolset in Toolset:
        granted = admitted_scopes(toolset)
        visible = [tool for tool in tools if tool_visible(tool.name, toolset, granted)]
        assert len(visible) <= ToolInventory.DEFAULT_FIXED_LIMIT

    with pytest.raises(ValueError, match="limit of 16"):
        ToolInventory(SimpleNamespace(name=str(index)) for index in range(17))
    extended = ToolInventory(
        (SimpleNamespace(name=str(index)) for index in range(20)),
        max_fixed_limit=20,
    )
    assert extended.fixed_count == 20
    with pytest.raises(ValueError, match="limit of 20"):
        ToolInventory(
            (SimpleNamespace(name=str(index)) for index in range(21)),
            max_fixed_limit=20,
        )


def test_capability_catalog_filters_by_scope_and_returns_host_fallbacks() -> None:
    catalog = CapabilityCatalog.default()
    execution = AuthorizationContext("agent", frozenset({Scope.EXECUTE}), Toolset.EXECUTION)

    before = catalog.visible_names(execution)
    result = catalog.search("job status", execution, limit=10)
    described = catalog.describe("comfyui.job.get", execution)

    assert result["items"][0]["name"] == "comfyui.job.get"
    assert described["required_scopes"] == ["comfyui:execute"]
    assert described["fallbacks"]["subscriptions"] == "resource_refetch"
    assert described["fallbacks"]["tasks"] == "submitted_job_resource"
    assert catalog.visible_names(execution) == before
    assert "comfyui.node.list" not in before


def test_compatibility_matrix_covers_required_axes_and_evidence_levels() -> None:
    cells = {cell.cell_id: cell for cell in COMPATIBILITY_MATRIX}

    assert {
        "comfyui-minimum",
        "comfyui-latest",
        "manager-absent",
        "manager-supported",
        "prompt-legacy",
        "jobs-api",
        "mcp-optional-supported",
        "mcp-fallback-client",
        "sqlite-stdio",
        "postgres-two-worker",
    } <= cells.keys()
    assert all(cell.version and cell.scenarios and cell.evidence_level for cell in cells.values())
    assert all(cell.status in {"planned", "implemented", "verified"} for cell in cells.values())
    assert all(cell.evidence_id for cell in cells.values())
    assert cells["mcp-optional-supported"].status == "planned"


def test_host_fallbacks_are_explicit_for_every_optional_feature() -> None:
    fallbacks = host_fallbacks(HostCapabilities())

    assert fallbacks == {
        "elicitation": "approval_resource",
        "subscriptions": "resource_refetch",
        "tasks": "submitted_job_resource",
        "apps": "resource_link",
    }


def test_eval_harness_records_selection_tokens_calls_and_success() -> None:
    cases = (
        EvalCase("generate", "Generate an image", "comfyui.run.local.txt2img", False),
        EvalCase("cancel", "Cancel a queued job", "comfyui.job.cancel", True),
    )
    trials = (
        EvalTrial(
            "medium-model", "medium", "generate", "comfyui.run.local.txt2img", 1, 320, 44, True
        ),
        EvalTrial("medium-model", "medium", "cancel", "comfyui.job.cancel", 2, 360, 51, True),
        EvalTrial(
            "small-local-model", "small-local", "generate", "comfyui.job.get", 2, 300, 38, False
        ),
        EvalTrial(
            "small-local-model", "small-local", "cancel", "comfyui.job.cancel", 1, 295, 36, True
        ),
    )

    report = evaluate_trials(cases, trials, active_tool_count=12)

    assert report["models"] == ["medium-model", "small-local-model"]
    assert report["metrics"]["task_success_rate"] == 0.75
    assert report["metrics"]["first_tool_accuracy"] == 0.75
    assert report["metrics"]["average_tool_calls"] == 1.5
    assert report["metrics"]["schema_tokens"] == 1275
    assert report["metrics"]["result_tokens"] == 169
    assert report["metrics"]["dangerous_mistrigger_rate"] == 0.0
    assert report["budgets"]["active_tool_count"] == {"value": 12, "limit": 16, "passed": True}

    with pytest.raises(ValueError, match="unique"):
        evaluate_trials((cases[0], cases[0]), trials[:2], active_tool_count=12)
    with pytest.raises(TypeError, match="success"):
        EvalTrial("model", "tier", "generate", "tool", 1, 1, 1, 2)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="invalid_retries"):
        EvalTrial("model", "tier", "generate", "tool", 1, 1, 1, True, invalid_retries=-1)
    with pytest.raises(TypeError, match="schema_tokens"):
        EvalTrial("model", "tier", "generate", "tool", 1, None, 1, True)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="result_tokens"):
        EvalTrial("model", "tier", "generate", "tool", 1, 1, None, True)  # type: ignore[arg-type]


def test_eval_command_writes_reproducible_report(tmp_path: Path) -> None:
    input_path = tmp_path / "trials.json"
    output_path = tmp_path / "report.json"
    input_path.write_text(
        json.dumps(
            {
                "active_tool_count": 6,
                "cases": [
                    {
                        "case_id": "generate",
                        "task": "Generate an image",
                        "expected_first_tool": "comfyui.run.local.txt2img",
                        "dangerous_operation": False,
                    }
                ],
                "trials": [
                    {
                        "model": "local-small",
                        "model_tier": "small-local",
                        "case_id": "generate",
                        "selected_first_tool": "comfyui.run.local.txt2img",
                        "tool_calls": 1,
                        "schema_tokens": 200,
                        "result_tokens": 30,
                        "success": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = run(input_path, output_path)

    assert report["metrics"]["task_success_rate"] == 1.0
    assert json.loads(output_path.read_text(encoding="utf-8")) == report


def test_job_errors_are_structured_bounded_and_redacted() -> None:
    unsafe_message = [
        "execution_error",
        {
            "node_id": "42",
            "node_type": "LoadImage",
            "exception_type": "RuntimeError",
            "exception_message": (
                "C:/private/path with spaces/input.png secret=hunter2 "
                'Authorization: Bearer sk-live {"token":"abc","api_key":"xyz"}'
            ),
            "traceback": ["private stack"],
        },
    ]
    error = JobService._format_errors(
        {"status": {"messages": [unsafe_message for _index in range(100)]}}
    )

    assert "node_id=42" in error
    assert "message=redacted_upstream_error" in error
    for secret in (
        "hunter2",
        "sk-live",
        '"abc"',
        '"xyz"',
        "C:/private",
        "private stack",
    ):
        assert secret not in error
    assert error.count("execution_error") == 8
    assert len(error) <= 2048


def test_deepseek_runner_records_provider_usage_without_exposing_key(tmp_path: Path) -> None:
    definition_path = tmp_path / "definition.json"
    output_path = tmp_path / "baseline.json"
    definition_path.write_text(
        json.dumps(
            {
                "eval_id": "test-baseline",
                "tools": [
                    {"name": "comfyui.job.get", "summary": "Read job status"},
                    {"name": "comfyui.job.cancel", "summary": "Cancel a queued job"},
                ],
                "cases": [
                    {
                        "case_id": "status",
                        "task": "Check job status",
                        "expected_first_tool": "comfyui.job.get",
                        "dangerous_operation": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [{"message": {"content": "comfyui.job.get"}}],
                "usage": {"prompt_tokens": 123, "completion_tokens": 9},
            }

    class Session:
        trust_env = True

        def post(self, url: str, **kwargs: object) -> Response:
            captured["url"] = url
            captured.update(kwargs)
            captured["trust_env"] = self.trust_env
            return Response()

        def close(self) -> None:
            captured["closed"] = True

    report = run_deepseek_baseline(
        definition_path,
        output_path,
        api_key="secret-value",
        session_factory=Session,  # type: ignore[arg-type]
    )

    assert report["model"] == "deepseek-v4-flash"
    assert report["metrics"]["first_tool_accuracy"] == 1.0
    assert report["metrics"]["task_success_rate"] is None
    assert report["metrics"]["average_tool_calls"] is None
    assert report["metrics"]["schema_tokens"] == 0
    assert report["metrics"]["result_tokens"] == 0
    assert report["metrics"]["provider_input_tokens"] == 123
    assert report["metrics"]["provider_output_tokens"] == 9
    assert report["metrics"]["median_selection_latency_ms"] is not None
    assert "selection_catalog_sha256" in report
    assert captured["url"] == "http://127.0.0.1:3000/v1/chat/completions"
    assert captured["allow_redirects"] is False
    assert captured["trust_env"] is False
    assert captured["closed"] is True
    assert "secret-value" not in output_path.read_text(encoding="utf-8")
    assert selected_tool("`comfyui.job.cancel`", {"comfyui.job.cancel"}) == "comfyui.job.cancel"
    assert selected_tool("Do not call comfyui.job.cancel", {"comfyui.job.cancel"}) == ""
    assert selected_tool("", {"comfyui.job.cancel"}) == ""
