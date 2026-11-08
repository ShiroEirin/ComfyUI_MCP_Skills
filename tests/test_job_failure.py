"""Failure facts must survive the engine response, stay bounded, and be readable."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from comfyui_mcp_skills.domain.control_plane import derive_legacy_job_id
from comfyui_mcp_skills.domain.job_failure import (
    MAX_FAILURE_BYTES,
    failure_from_json,
    failure_to_json,
    summarize_failure,
)
from comfyui_mcp_skills.domain.models import Job
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.sqlite_diagnostics import (
    SQLiteDiagnosticRetryRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_runs import SQLiteRunRepository


def _history() -> dict[str, Any]:
    return {
        "status": {
            "status_str": "error",
            "completed": False,
            "messages": [
                ["execution_start", {"prompt_id": "prompt-1"}],
                [
                    "execution_error",
                    {
                        "prompt_id": "prompt-1",
                        "node_id": "4",
                        "node_type": "CheckpointLoaderSimple",
                        "exception_message": (
                            "Value not in list: ckpt_name: 'missing.safetensors' not in "
                            "['a.safetensors', 'b.safetensors']"
                        ),
                        "exception_type": "invalid_input",
                        "current_inputs": {"ckpt_name": ["missing.safetensors"]},
                        "traceback": ["C:\\Users\\dev\\workflow.py", "TracebackFrameMarker"],
                    },
                ],
            ],
        },
        "outputs": {},
    }


def test_summarize_failure_keeps_only_actionable_fields() -> None:
    failure = summarize_failure(_history())

    assert failure == {
        "node_id": "4",
        "class_type": "CheckpointLoaderSimple",
        "input_name": "ckpt_name",
        "error_type": "invalid_input",
        "message": (
            "Value not in list: ckpt_name: 'missing.safetensors' not in "
            "['a.safetensors', 'b.safetensors']"
        ),
        "requested": ["missing.safetensors"],
        "missing_models": [],
        "candidates": ["a.safetensors", "b.safetensors"],
    }
    encoded = json.dumps(failure)
    assert "traceback" not in encoded
    assert "TracebackFrameMarker" not in encoded
    assert "Users" not in encoded


def test_structured_missing_models_win_over_the_printed_options() -> None:
    history = _history()
    payload = history["status"]["messages"][1][1]
    payload["missing_models"] = ["flux1-dev.safetensors"]

    failure = summarize_failure(history)

    assert failure is not None
    assert failure["missing_models"] == ["flux1-dev.safetensors"]
    assert failure["candidates"] == []


def test_failure_document_stays_bounded() -> None:
    history = _history()
    payload = history["status"]["messages"][1][1]
    payload["exception_message"] = "x" * 100_000
    payload["current_inputs"] = {"ckpt_name": ["m" * 5000]}

    failure = summarize_failure(history)

    assert failure is not None
    assert len(failure["message"]) <= 1024
    assert all(len(item) <= 128 for item in failure["requested"])
    assert len(failure_to_json(failure).encode("utf-8")) <= MAX_FAILURE_BYTES


def test_non_failure_history_is_reported_as_none() -> None:
    assert summarize_failure(None) is None
    assert summarize_failure({}) is None
    assert summarize_failure({"status": {"messages": [["execution_start", {}]]}}) is None
    assert (
        summarize_failure({"status": {"messages": [["execution_error", "not a mapping"]]}})
        is None
    )
    assert failure_from_json("not json") is None
    assert failure_from_json('{"node_id": "1"}') == {"node_id": "1"}


def test_failure_survives_a_sqlite_round_trip(tmp_path: Path) -> None:
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()
    runs = SQLiteRunRepository(store)
    failure = summarize_failure(_history())
    job_id = derive_legacy_job_id("local", "prompt-1")
    runs.save(
        Job(
            prompt_id="prompt-1",
            server_id="local",
            workflow_id="portrait",
            status="error",
            error="Workflow execution failed",
            owner_id="owner-a",
            job_id=job_id,
            failure=failure,
        )
    )

    stored = runs.get("local", "prompt-1")

    assert stored is not None
    assert stored.failure == failure
    assert failure_from_json(failure_to_json(stored.failure)) == failure


def test_diagnostic_context_reads_the_persisted_failure(tmp_path: Path) -> None:
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store.initialize()
    runs = SQLiteRunRepository(store)
    job_id = derive_legacy_job_id("local", "prompt-1")
    runs.save(
        Job(
            prompt_id="prompt-1",
            server_id="local",
            workflow_id="portrait",
            status="error",
            error="Workflow execution failed",
            owner_id="owner-a",
            job_id=job_id,
            failure=summarize_failure(_history()),
        )
    )

    context = SQLiteDiagnosticRetryRepository(store).get_job_diagnostic_context(
        job_id,
        "owner-a",
        event_limit=8,
        log_line_limit=8,
    )

    assert context is not None
    assert context["failed_node"] == {
        "node_id": "4",
        "class_type": "CheckpointLoaderSimple",
        "error_type": "invalid_input",
        "message": (
            "Value not in list: ckpt_name: 'missing.safetensors' not in "
            "['a.safetensors', 'b.safetensors']"
        ),
    }
    assert context["missing_models"] == ["missing.safetensors"]
    assert "CheckpointLoaderSimple" in context["classification_text"]
