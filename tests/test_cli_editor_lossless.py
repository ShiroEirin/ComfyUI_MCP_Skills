"""Editor-only widget loss must never block a CLI import."""

from __future__ import annotations

from typing import Any

import pytest

from comfyui_mcp_skills.application.workflow_conversion import is_lossless_drop
from comfyui_skills_cli.commands import workflow as cli_workflow

_GRAPH: dict[str, Any] = {"1": {"class_type": "KSampler", "inputs": {}}}


def test_control_after_generate_is_a_lossless_drop() -> None:
    assert is_lossless_drop("2.seed.control_after_generate=fixed")
    assert is_lossless_drop("node_9.steps.control_after_generate=randomize")


def test_real_field_loss_stays_blocking() -> None:
    assert not is_lossless_drop("nodes[0]")
    assert not is_lossless_drop("1.widgets_values[0]")
    assert not is_lossless_drop("links[0].target")


def test_cli_import_accepts_a_lossless_control_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_workflow,
        "convert_editor_workflow",
        lambda *_args, **_kwargs: (
            _GRAPH,
            (),
            ("2.seed.control_after_generate=fixed",),
        ),
    )

    assert cli_workflow._convert_editor_to_api({}, {}) == _GRAPH


def test_cli_import_still_rejects_a_real_field_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_workflow,
        "convert_editor_workflow",
        lambda *_args, **_kwargs: (_GRAPH, (), ("nodes[0].type",)),
    )

    with pytest.raises(ValueError, match="would drop fields"):
        cli_workflow._convert_editor_to_api({}, {})
