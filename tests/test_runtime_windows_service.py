"""Windows Service RuntimeController adapter contracts (subprocess fully mocked)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from comfyui_mcp_skills.application.runtime_restart import RuntimeRestartService
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.runtime_restart import (
    SQLiteRuntimeRestartRepository,
)
from comfyui_mcp_skills.infrastructure.runtime.systemd import (
    RuntimeConfigError,
    controller_from_config,
)
from comfyui_mcp_skills.infrastructure.runtime.windows_service import (
    WindowsServiceController,
    windows_service_from_config,
)

_NOW = __import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc)


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class _ScRecorder:
    """Records argv calls and replays a scripted sequence of results."""

    def __init__(self, *results: subprocess.CompletedProcess) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self._results = list(results)

    def __call__(self, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        assert kwargs.get("shell") is False
        timeout = kwargs.get("timeout")
        assert isinstance(timeout, (int, float)) and timeout > 0, "subprocess timeout required"
        self.calls.append(list(args[0]))
        self.timeouts.append(float(timeout))
        if not self._results:
            raise AssertionError("unexpected sc.exe call")
        return self._results.pop(0)


# ---------------------------------------------------------------------------
# name validation
# ---------------------------------------------------------------------------


def test_service_name_validation_accepts_scm_legal_names() -> None:
    for name in (
        "ComfyUI",
        "1Password",
        "My Service With Spaces",
        "svc_STOPPED_state",
        "STATE : 1 in the middle",
        "x" * 256,
    ):
        assert WindowsServiceController(name) is not None
        assert (
            windows_service_from_config(
                {"runtime": {"adapter": "windows_service", "service": name}}
            )
            == name
        )


def test_service_name_validation_rejects_illegal_names() -> None:
    for name in (
        "",
        "/slash",
        "back\\slash",
        "x" * 257,
        "nul\x00byte",
        "del\x7fchar",
        "c1\x9fchar",
    ):
        with pytest.raises(RuntimeConfigError):
            WindowsServiceController(name)
        with pytest.raises(RuntimeConfigError):
            windows_service_from_config(
                {"runtime": {"adapter": "windows_service", "service": name}}
            )


def test_windows_service_from_config_rejects_bad_bindings() -> None:
    for config in (
        {},
        {"runtime": {"adapter": "docker", "container": "x"}},
        {"runtime": {"adapter": "windows_service"}},
        {"runtime": {"adapter": "windows_service", "service": 5}},
    ):
        with pytest.raises(RuntimeConfigError):
            windows_service_from_config(config)


# ---------------------------------------------------------------------------
# restart flow
# ---------------------------------------------------------------------------


def test_restart_stops_waits_and_starts(tmp_path: Path) -> None:
    recorder = _ScRecorder(
        _completed(0),  # stop accepted
        _completed(0, stdout="STATE : 3\n"),  # still STOP_PENDING
        _completed(0, stdout="STATE : 1\n"),  # STOPPED
        _completed(0),  # start accepted
    )
    with patch(
        "comfyui_mcp_skills.infrastructure.runtime.windows_service.subprocess.run", recorder
    ):
        result = WindowsServiceController("ComfyUI").restart("local")

    assert recorder.calls == [
        ["sc.exe", "stop", "ComfyUI"],
        ["sc.exe", "query", "ComfyUI"],
        ["sc.exe", "query", "ComfyUI"],
        ["sc.exe", "start", "ComfyUI"],
    ]
    assert result == {
        "server_id": "local",
        "adapter": "windows_service",
        "service": "ComfyUI",
        "completed": True,
    }


def test_restart_skips_query_when_stop_returns_1062_exit_code() -> None:
    recorder = _ScRecorder(
        _completed(1062),  # SERVICE_NOT_ACTIVE
        _completed(0),
    )
    with patch(
        "comfyui_mcp_skills.infrastructure.runtime.windows_service.subprocess.run", recorder
    ):
        WindowsServiceController("ComfyUI").restart("local")

    assert recorder.calls == [
        ["sc.exe", "stop", "ComfyUI"],
        ["sc.exe", "start", "ComfyUI"],
    ]


def test_restart_tolerates_1062_text_on_stdout_and_stderr() -> None:
    for channel in ("stdout", "stderr"):
        kwargs = {channel: "[SC] ControlService FAILED 1062:"}
        recorder = _ScRecorder(_completed(5, **kwargs), _completed(0))
        with patch(
            "comfyui_mcp_skills.infrastructure.runtime.windows_service.subprocess.run", recorder
        ):
            WindowsServiceController("ComfyUI").restart("local")
        assert len(recorder.calls) == 2


def test_restart_stop_failure_echoing_service_name_with_1062_stays_fail_closed() -> None:
    # The service name itself contains "1062"; a different stop failure (exit 5,
    # access denied) that echoes the command must NOT be treated as inactive.
    recorder = _ScRecorder(
        _completed(5, stderr="[SC] ControlService FAILED 5: Access is denied. svc1062"),
    )
    with patch(
        "comfyui_mcp_skills.infrastructure.runtime.windows_service.subprocess.run", recorder
    ):
        with pytest.raises(RuntimeError, match="stop failed"):
            WindowsServiceController("svc1062").restart("local")
    assert [call[1] for call in recorder.calls] == ["stop"]


def test_restart_fails_on_stop_error_without_1062() -> None:
    recorder = _ScRecorder(_completed(5, stderr="access denied"))
    with patch(
        "comfyui_mcp_skills.infrastructure.runtime.windows_service.subprocess.run", recorder
    ):
        with pytest.raises(RuntimeError, match="stop failed"):
            WindowsServiceController("ComfyUI").restart("local")
    assert len(recorder.calls) == 1  # no query/start after a real stop failure


def test_restart_does_not_misparse_service_name_containing_1062() -> None:
    # Successful query output echoes the service name containing "1062";
    # STATE : 3 means STOP_PENDING, so start must NOT run yet.
    recorder = _ScRecorder(
        _completed(0),
        _completed(0, stdout="SERVICE_NAME: svc1062\nSTATE : 3\n"),
        _completed(0, stdout="STATE : 1\n"),
        _completed(0),
    )
    with patch(
        "comfyui_mcp_skills.infrastructure.runtime.windows_service.subprocess.run", recorder
    ):
        WindowsServiceController("svc1062").restart("local")

    assert [call[1] for call in recorder.calls] == ["stop", "query", "query", "start"]


def test_restart_fails_on_non_1062_query_error() -> None:
    recorder = _ScRecorder(
        _completed(0),
        _completed(5, stderr="query denied"),
    )
    with patch(
        "comfyui_mcp_skills.infrastructure.runtime.windows_service.subprocess.run", recorder
    ):
        with pytest.raises(RuntimeError, match="query failed"):
            WindowsServiceController("ComfyUI").restart("local")
    assert [call[1] for call in recorder.calls] == ["stop", "query"]


def test_restart_query_1062_fails_closed_without_start() -> None:
    # The 1062 bypass belongs to the stop command only; a polling query that
    # returns 1062 must fail closed and never trigger an early start.
    recorder = _ScRecorder(
        _completed(0),
        _completed(1062),
    )
    with patch(
        "comfyui_mcp_skills.infrastructure.runtime.windows_service.subprocess.run", recorder
    ):
        with pytest.raises(RuntimeError, match="query failed"):
            WindowsServiceController("ComfyUI").restart("local")
    assert [call[1] for call in recorder.calls] == ["stop", "query"]


def test_restart_passes_remaining_budget_to_poll_queries(tmp_path: Path, monkeypatch) -> None:
    recorder = _ScRecorder(
        _completed(0),
        _completed(0, stdout="STATE : 3\n"),
        _completed(0, stdout="STATE : 1\n"),
        _completed(0),
    )
    # monotonic: deadline calc, loop-1 now, post-query recompute, loop-2 now.
    times = iter([100.0, 100.5, 101.0, 101.0])
    monkeypatch.setattr(
        "comfyui_mcp_skills.infrastructure.runtime.windows_service.time.monotonic",
        lambda: next(times),
    )
    sleep_calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(
        "comfyui_mcp_skills.infrastructure.runtime.windows_service.time.sleep", fake_sleep
    )
    with patch(
        "comfyui_mcp_skills.infrastructure.runtime.windows_service.subprocess.run", recorder
    ):
        WindowsServiceController("ComfyUI", stop_timeout_seconds=10.0).restart("local")

    assert recorder.timeouts[1] < 10.0  # query 1 bounded by remaining budget
    assert recorder.timeouts[2] < recorder.timeouts[1]
    assert sleep_calls and sleep_calls[0] <= recorder.timeouts[1]


def test_restart_does_not_misparse_service_name_containing_state_markers() -> None:
    # Service name contains "STATE : 1" and "STOPPED" markers; the actual
    # state line says 3 (STOP_PENDING) so start must NOT run yet.
    recorder = _ScRecorder(
        _completed(0),
        _completed(0, stdout="SERVICE_NAME: My STATE : 1 STOPPED Service\nSTATE : 3\n"),
        _completed(0, stdout="STATE : 1\n"),
        _completed(0),
    )
    with patch(
        "comfyui_mcp_skills.infrastructure.runtime.windows_service.subprocess.run", recorder
    ):
        WindowsServiceController("My STATE : 1 STOPPED Service").restart("local")

    assert recorder.calls == [
        ["sc.exe", "stop", "My STATE : 1 STOPPED Service"],
        ["sc.exe", "query", "My STATE : 1 STOPPED Service"],
        ["sc.exe", "query", "My STATE : 1 STOPPED Service"],
        ["sc.exe", "start", "My STATE : 1 STOPPED Service"],
    ]


def test_restart_never_sleeps_past_deadline_after_slow_query(
    tmp_path: Path, monkeypatch
) -> None:
    recorder = _ScRecorder(
        _completed(0),
        _completed(0, stdout="STATE : 3\n"),  # slow query: 9.9s into a 10s budget
        _completed(0, stdout="STATE : 1\n"),
        _completed(0),
    )
    # monotonic: deadline calc (t=0), loop-1 now (t=0), post-query recompute
    # (t=9.9, the query consumed the budget), loop-2 now (t=9.95).
    times = iter([0.0, 0.0, 9.9, 9.95])
    monkeypatch.setattr(
        "comfyui_mcp_skills.infrastructure.runtime.windows_service.time.monotonic",
        lambda: next(times),
    )
    sleep_calls: list[float] = []
    monkeypatch.setattr(
        "comfyui_mcp_skills.infrastructure.runtime.windows_service.time.sleep",
        lambda seconds: sleep_calls.append(seconds),
    )
    with patch(
        "comfyui_mcp_skills.infrastructure.runtime.windows_service.subprocess.run", recorder
    ):
        WindowsServiceController("ComfyUI", stop_timeout_seconds=10.0).restart("local")

    # The post-query remaining (0.1s) bounds the sleep; the loop never issues a
    # query beyond the deadline and start still runs.
    assert sleep_calls and sleep_calls[0] <= 0.1
    assert recorder.calls[-1][1] == "start"


def test_restart_stop_timeout_fails_without_start(tmp_path: Path, monkeypatch) -> None:
    recorder = _ScRecorder(
        _completed(0),
        _completed(0, stdout="STATE : 3\n"),
    )
    times = iter([0.0, 0.0, 61.0])  # deadline calc, loop-1 now, loop-2 now
    monkeypatch.setattr(
        "comfyui_mcp_skills.infrastructure.runtime.windows_service.time.monotonic",
        lambda: next(times),
    )
    monkeypatch.setattr(
        "comfyui_mcp_skills.infrastructure.runtime.windows_service.time.sleep", lambda _s: None
    )
    with patch(
        "comfyui_mcp_skills.infrastructure.runtime.windows_service.subprocess.run", recorder
    ):
        with pytest.raises(RuntimeError, match="timed out"):
            WindowsServiceController("ComfyUI").restart("local")
    assert [call[1] for call in recorder.calls] == ["stop", "query"]


def test_restart_fails_when_start_fails() -> None:
    recorder = _ScRecorder(
        _completed(1062),
        _completed(5, stderr="service not found"),
    )
    with patch(
        "comfyui_mcp_skills.infrastructure.runtime.windows_service.subprocess.run", recorder
    ):
        with pytest.raises(RuntimeError, match="start failed"):
            WindowsServiceController("ComfyUI").restart("local")


def test_restart_maps_missing_executable_and_timeouts() -> None:
    class MissingExecutable:
        def __call__(self, *args: Any, **kwargs: Any) -> None:
            raise FileNotFoundError("sc.exe")

    with patch(
        "comfyui_mcp_skills.infrastructure.runtime.windows_service.subprocess.run",
        MissingExecutable(),
    ):
        with pytest.raises(RuntimeError, match="not available"):
            WindowsServiceController("ComfyUI").restart("local")

    class SlowExecutable:
        def __call__(self, *args: Any, **kwargs: Any) -> None:
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=1.0)

    with patch(
        "comfyui_mcp_skills.infrastructure.runtime.windows_service.subprocess.run", SlowExecutable()
    ):
        with pytest.raises(RuntimeError, match="timed out"):
            WindowsServiceController("ComfyUI").restart("local")


# ---------------------------------------------------------------------------
# wiring and binding pinning
# ---------------------------------------------------------------------------


def test_controller_from_config_wires_windows_service() -> None:
    controller = controller_from_config(
        {"runtime": {"adapter": "windows_service", "service": "ComfyUI"}}
    )
    assert isinstance(controller, WindowsServiceController)
    assert controller_from_config({"runtime": {"adapter": "windows_service"}}) is None
    assert controller_from_config({"runtime": {"adapter": "k8s"}}) is None


def test_controller_binding_normalization_and_g2_flow(tmp_path: Path) -> None:
    base = tmp_path / "proj"
    base.mkdir(parents=True)
    (base / "config.json").write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "id": "local",
                        "url": "http://127.0.0.1:8188",
                        "runtime": {"adapter": "windows_service", "service": "ComfyUI"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    store = SQLiteControlPlaneStore((base / "data" / "control-plane.sqlite3").resolve())
    store.initialize()
    with patch("comfyui_mcp_skills.infrastructure.runtime.windows_service.subprocess.run") as run:
        run.return_value = _completed(0, stdout="STATE : 1\n")
        service = RuntimeRestartService(
            ServerRegistry(base),
            SQLiteRuntimeRestartRepository(store),
            controller_provider=lambda _sid: WindowsServiceController("ComfyUI"),
        )
        plan = service.plan("local", "owner-a")
        assert plan["controller_binding"] == {
            "adapter": "windows_service",
            "service": "ComfyUI",
        }
        service.approve(plan["plan_id"], "approved", "owner-a", "")
        result = service.commit(
            plan["plan_id"], plan["plan_digest"], plan["approval_id"], "owner-a", "request-1"
        )
        assert result["status"] == "completed"
        assert result["commit_result"]["controller_outcome"]["adapter"] == "windows_service"
    assert run.call_count == 3  # stop, query (STOPPED), start
