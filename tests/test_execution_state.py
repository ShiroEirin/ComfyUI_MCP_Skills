"""Execution state and history persistence regressions."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer
import websocket

from comfyui_mcp_skills.application import jobs as jobs_module
from comfyui_mcp_skills.application.jobs import JobService
from comfyui_mcp_skills.domain.models import Job
from comfyui_mcp_skills.infrastructure.comfyui import jobs_client as comfyui_jobs_client_module
from comfyui_mcp_skills.infrastructure.comfyui.client import ComfyUIClient
from comfyui_mcp_skills.infrastructure.comfyui.gateway import ComfyUIGatewayAdapter
from comfyui_skills_cli.commands import history as history_command
from comfyui_skills_cli.commands import run as run_command
from comfyui_skills_cli.commands.run import (
    OutputFormat,
    _run_with_poll,
    _run_with_ws,
    _RunContext,
    classify_history,
    run_cmd,
)
from comfyui_skills_cli.history_writer import save_run_record


def _ctx() -> typer.Context:
    ctx = MagicMock(spec=typer.Context)
    ctx.obj = {"output_format": "json"}
    return ctx


class _FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


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

    run_context.save.assert_called_once_with("error", error="CUDA out of memory")


def test_error_status_wins_over_partial_outputs() -> None:
    history = {
        "status": {"status_str": "error", "completed": True},
        "outputs": {
            "1": {"images": [{"filename": "partial.png", "subfolder": "", "type": "output"}]}
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


def test_job_wait_caps_each_query_to_remaining_deadline() -> None:
    clock = _FakeClock()
    saved = Job("prompt-1", "local", "workflow", "submitted")
    runs = MagicMock()
    runs.get.return_value = saved
    registry = MagicMock()
    registry.connection.return_value = {
        "url": "http://127.0.0.1:8188",
        "timeout": 30,
    }
    query_timeouts: list[float] = []

    def request_get(url: str, **kwargs: object) -> MagicMock:
        query_timeouts.append(float(kwargs["timeout"]))
        clock.now += 0.4
        response = MagicMock(status_code=200)
        if url.endswith("/queue"):
            response.json.return_value = {
                "queue_running": [],
                "queue_pending": [[0, "prompt-1"]],
            }
        else:
            response.json.return_value = {}
        return response

    service = JobService(registry, runs, ComfyUIGatewayAdapter)
    with (
        patch.object(jobs_module.time, "monotonic", clock.monotonic),
        patch.object(jobs_module.time, "sleep", clock.sleep),
        patch(
            "comfyui_mcp_skills.infrastructure.comfyui.core_client.requests.get",
            side_effect=request_get,
        ),
    ):
        result = service.wait("local", "prompt-1", timeout_seconds=1)

    assert result == saved
    assert query_timeouts == pytest.approx([1.0, 0.6])


def test_job_wait_does_not_refresh_after_websocket_reaches_deadline() -> None:
    clock = _FakeClock()
    saved = Job(
        "prompt-1",
        "local",
        "workflow",
        "submitted",
        client_id="client-1",
    )
    latest = Job(
        "prompt-1",
        "local",
        "workflow",
        "running",
        client_id="client-1",
    )
    runs = MagicMock()
    runs.get.side_effect = [saved, latest]
    registry = MagicMock()
    registry.connection.return_value = {"url": "http://127.0.0.1:8188"}
    gateway = MagicMock()

    def websocket_events(
        _client_id: str,
        _prompt_id: str,
        timeout_seconds: float,
        _cancel_check: object,
    ) -> object:
        clock.now += timeout_seconds
        return iter(())

    gateway.ws_events.side_effect = websocket_events
    service = JobService(registry, runs, lambda _config: gateway)
    with patch.object(jobs_module.time, "monotonic", clock.monotonic):
        result = service.wait("local", "prompt-1", timeout_seconds=1)

    assert result == latest
    gateway.get_history.assert_not_called()
    gateway.get_queue.assert_not_called()


def test_websocket_io_uses_remaining_deadline_and_discards_late_event() -> None:
    clock = _FakeClock()
    socket = MagicMock()
    receive_timeouts: list[float] = []

    def set_timeout(seconds: float) -> None:
        receive_timeouts.append(seconds)

    def receive() -> tuple[int, str]:
        clock.now += 0.05
        return (
            websocket.ABNF.OPCODE_TEXT,
            '{"type":"executing","data":{"prompt_id":"prompt-1","node":null}}',
        )

    socket.settimeout.side_effect = set_timeout
    socket.recv_data.side_effect = receive
    client = ComfyUIClient("http://127.0.0.1:8188", timeout=30)
    with (
        patch.object(comfyui_jobs_client_module.time, "monotonic", clock.monotonic),
        patch("websocket.create_connection", return_value=socket) as connect,
    ):
        events = list(client.ws_events("client-1", "prompt-1", timeout_seconds=0.05))

    assert events == []
    assert connect.call_args.kwargs["timeout"] == pytest.approx(0.05)
    assert receive_timeouts == pytest.approx([0.05])


def test_job_wait_refreshes_terminal_state_before_deadline() -> None:
    clock = _FakeClock()
    saved = Job(
        "prompt-1",
        "local",
        "workflow",
        "submitted",
        client_id="client-1",
    )
    runs = MagicMock()
    runs.get.return_value = saved
    registry = MagicMock()
    registry.connection.return_value = {"url": "http://127.0.0.1:8188"}
    gateway = MagicMock()
    terminal_event = {
        "type": "executing",
        "data": {"prompt_id": "prompt-1", "node": None},
    }
    gateway.ws_events.return_value = iter([terminal_event])
    gateway.get_history.return_value = {
        "status": {"completed": True, "status_str": "success"},
        "outputs": {},
    }
    progress: list[dict[str, object]] = []
    service = JobService(registry, runs, lambda _config: gateway)

    with patch.object(jobs_module.time, "monotonic", clock.monotonic):
        result = service.wait(
            "local",
            "prompt-1",
            timeout_seconds=1,
            progress=progress.append,
        )

    assert result.status == "completed"
    assert progress == [terminal_event]
    gateway.get_history.assert_called_once_with("prompt-1", timeout_seconds=pytest.approx(1.0))
    gateway.get_queue.assert_not_called()
    runs.save.assert_called_once_with(result)
