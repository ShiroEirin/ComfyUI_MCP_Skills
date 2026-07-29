"""Execution state and history persistence regressions."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer

from comfyui_skills_cli.commands import history as history_command
from comfyui_skills_cli.commands import run as run_command

from comfyui_skills_cli.commands.run import (
    OutputFormat,
    _RunContext,
    _run_with_poll,
    _run_with_ws,
    run_cmd,
    classify_history,
)
from comfyui_skills_cli.history_writer import save_run_record


def _ctx() -> typer.Context:
    ctx = MagicMock(spec=typer.Context)
    ctx.obj = {"output_format": "json"}
    return ctx


def test_websocket_failure_is_persisted_before_exit(tmp_path: Path) -> None:
    client = MagicMock()
    client.ws_events.return_value = iter(
        [
            {
                "type": "execution_error",
                "data": {
                    "prompt_id": "prompt-1",
                    "exception_message": "CUDA out of memory",
                },
            }
        ]
    )
    run_context = MagicMock(spec=_RunContext)

    with pytest.raises(typer.Exit):
        _run_with_ws(
            _ctx(),
            client,
            {},
            "prompt-1",
            "client-1",
            tmp_path,
            {"output_dir": "outputs"},
            OutputFormat.JSON,
            run_context,
        )

    run_context.save.assert_called_once_with(
        "error", error="CUDA out of memory"
    )


def test_error_status_wins_over_partial_outputs() -> None:
    history = {
        "status": {"status_str": "error", "completed": True},
        "outputs": {
            "1": {
                "images": [
                    {"filename": "partial.png", "subfolder": "", "type": "output"}
                ]
            }
        },
    }

    state, _outputs, error = classify_history(history)

    assert state == "error"
    assert error == "Workflow execution failed"


def test_polling_deadline_returns_durable_job_handle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client = MagicMock()

    _run_with_poll(
        _ctx(),
        client,
        "prompt-1",
        tmp_path,
        {"output_dir": "outputs"},
        OutputFormat.JSON,
        deadline=time.monotonic() - 1,
    )

    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload == {
        "status": "submitted",
        "prompt_id": "prompt-1",
        "reason": "wait_timeout",
    }
    client.get_history.assert_not_called()


def test_run_cmd_does_not_poll_after_websocket_terminal_exit(
    tmp_path: Path,
) -> None:
    client = MagicMock()
    client.queue_prompt.return_value = {"prompt_id": "prompt-1"}
    run_context = MagicMock(spec=_RunContext)

    with (
        patch.object(
            run_command,
            "_resolve_skill",
            return_value=(tmp_path, "local", "flow"),
        ),
        patch.object(
            run_command,
            "_prepare",
            return_value=(client, {}, {"1": {"inputs": {}}}, {}),
        ),
        patch.object(run_command, "_RunContext", return_value=run_context),
        patch.object(run_command, "_ws_available", return_value=True),
        patch.object(run_command, "_run_with_ws", side_effect=typer.Exit(1)),
        patch.object(run_command, "_run_with_poll") as polling,
        pytest.raises(typer.Exit),
    ):
        run_cmd(_ctx(), "local/flow", "{}", "", 0, False, "", 10)

    polling.assert_not_called()


def test_history_show_reads_hashed_local_prompt_record(tmp_path: Path) -> None:
    save_run_record(
        tmp_path,
        "local",
        "flow",
        "prompt-1",
        {"prompt": "cat"},
        "success",
    )
    ctx = _ctx()
    ctx.obj["base_dir"] = str(tmp_path)

    with (
        patch.object(history_command, "_show_from_server", return_value=None),
        patch.object(history_command, "output_result") as output,
    ):
        history_command.history_show(ctx, "local/flow", "prompt-1")

    record = output.call_args.args[1]
    assert record["prompt_id"] == "prompt-1"
    assert record["status"] == "success"
