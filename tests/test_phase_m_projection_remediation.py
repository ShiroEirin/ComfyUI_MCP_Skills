"""Focused Phase M projection, rating, and promotion regressions."""

from __future__ import annotations

import pytest
from mcp.types import GetPromptRequestParams

from comfyui_mcp_skills.adapters.mcp.prompts import create_prompt_handlers
from comfyui_mcp_skills.adapters.mcp.tooling import variant_dict
from comfyui_mcp_skills.application.experiment_projections import _public_variant


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_variant_projection_round_trips_bounded_terminal_results_ratings_and_promotions() -> None:
    experiment_id = "experiment_" + "a" * 64
    variant_id = "variant_" + "b" * 64
    job_id = "job_" + "c" * 64
    artifact_uri = "comfyui://artifacts/artifact_" + "d" * 64
    source = {
        "experiment_id": experiment_id,
        "variant_id": variant_id,
        "ordinal": 7,
        "parameter_digest": "e" * 64,
        "status": "completed",
        "job_id": job_id,
        "job_uri": f"comfyui://jobs/{job_id}",
        "artifact_uris": [artifact_uri],
        "measured_pixels": 4096,
        "measured_outputs": 1,
        "measured_seconds": 2.5,
        "error_code": "",
        "ratings": [
            {
                "rating_id": "rating_" + "f" * 64,
                "experiment_id": experiment_id,
                "variant_id": variant_id,
                "rubric_version": "quality-v1",
                "rubric_definition": {
                    "version": "quality-v1",
                    "dimensions": {"quality": {"minimum": 0.0, "maximum": 5.0}},
                },
                "scores": {"quality": 4.5},
                "created_at": "2026-08-03T12:00:01+00:00",
                "updated_at": "2026-08-03T12:00:01+00:00",
            }
        ],
        "promotions": [
            {
                "promotion_id": "promotion_" + "1" * 64,
                "experiment_id": experiment_id,
                "variant_id": variant_id,
                "target": "preset",
                "preset_id": "preset_" + "2" * 64,
                "created_at": "2026-08-03T12:00:02+00:00",
            },
            {
                "promotion_id": "promotion_" + "3" * 64,
                "experiment_id": experiment_id,
                "variant_id": variant_id,
                "target": "revision",
                "revision_id": "revision_" + "4" * 64,
                "published": False,
                "created_at": "2026-08-03T12:00:03+00:00",
            },
        ],
        "created_at": "2026-08-03T12:00:00+00:00",
        "updated_at": "2026-08-03T12:00:03+00:00",
        "completed_at": "2026-08-03T12:00:03+00:00",
        "arguments": {"prompt": "secret"},
        "client_id": "secret-client",
    }

    projected = variant_dict(_public_variant(source))

    assert projected["job_uri"] == f"comfyui://jobs/{job_id}"
    assert projected["artifact_uris"] == [artifact_uri]
    assert projected["measured_pixels"] == 4096
    assert projected["measured_outputs"] == 1
    assert projected["measured_seconds"] == 2.5
    assert projected["ratings"][0]["rubric_definition"]["version"] == "quality-v1"
    assert [item["target"] for item in projected["promotions"]] == ["preset", "revision"]
    assert "arguments" not in projected
    assert "client_id" not in projected


@pytest.mark.anyio
async def test_compare_prompt_names_every_round_trip_field_it_requires() -> None:
    experiment_id = "experiment_" + "a" * 64
    handlers = create_prompt_handlers()

    rendered = await handlers.get_prompt(
        None,
        GetPromptRequestParams(
            name="compare-experiment-results",
            arguments={"experiment_id": experiment_id},
        ),
    )
    text = rendered.messages[0].content.text

    for field in (
        "measured_pixels",
        "measured_outputs",
        "measured_seconds",
        "error_code",
        "job_uri",
        "artifact_uris",
        "ratings",
        "promotions",
        "resource_uri",
    ):
        assert field in text
