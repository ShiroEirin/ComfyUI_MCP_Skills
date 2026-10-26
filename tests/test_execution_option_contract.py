"""Agent-facing execution options must stay in the advertised schema."""

from __future__ import annotations

from comfyui_mcp_skills.adapters.mcp.tooling import EXECUTION_PROPERTY, fixed_tools


def test_execution_options_advertise_the_risky_node_allowance() -> None:
    properties = EXECUTION_PROPERTY["properties"]

    assert properties["allow_risky_nodes"] == {
        "type": "boolean",
        "default": False,
        "description": (
            "Submit even when the graph contains code-execution, network, "
            "or paid external-API nodes. Every allowance is audited."
        ),
    }


def test_output_image_advertises_optional_job_id_ownership() -> None:
    tool = next(item for item in fixed_tools() if item.name == "comfyui.output.image")

    properties = tool.input_schema["properties"]
    assert properties["job_id"]["type"] == "string"
    assert "job_id" not in tool.input_schema["required"]
