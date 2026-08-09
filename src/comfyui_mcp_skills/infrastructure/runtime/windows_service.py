"""Opt-in Windows Service RuntimeController adapter with a fixed, safe argv contract."""

from __future__ import annotations

import re
import subprocess
import time
from typing import Any

from comfyui_mcp_skills.infrastructure.runtime.systemd import RuntimeConfigError

# SCM service-name contract: 1-256 characters, no '/' or '\\', no control
# characters (C0 + DEL + C1); leading character unrestricted (digits/symbols
# are legal, e.g. "1Password", and names may contain spaces).
_SERVICE_PATTERN = re.compile(r"^[^/\\\x00-\x1f\x7f-\x9f]{1,256}$")

# `sc query` prints the state on its own line; anchor to the line start so a
# legal service name containing e.g. "STATE : 1" cannot be misparsed.
# State values: 1=STOPPED, 2=START_PENDING, 3=STOP_PENDING, 4=RUNNING, ...
_STATE_PATTERN = re.compile(r"^\s*STATE\s*:\s*(\d+)\b", re.MULTILINE)

_STATE_STOPPED = 1

# sc.exe reports failures as "[SC] ControlService FAILED 1062:" on the error
# line; match the error-code field exactly so a legal service name containing
# "1062" (e.g. "svc1062") can never be misread as SERVICE_NOT_ACTIVE.
_NOT_ACTIVE_TEXT = re.compile(r"FAILED\s+1062\b")


def windows_service_from_config(config: dict[str, Any]) -> str:
    """Return the validated Windows service name from a server record, or raise."""
    raw = config.get("runtime")
    if raw is None:
        raise RuntimeConfigError("no runtime binding configured")
    if not isinstance(raw, dict):
        raise RuntimeConfigError("runtime must be an object")
    if raw.get("adapter") != "windows_service":
        raise RuntimeConfigError("unsupported runtime adapter")
    service = raw.get("service")
    if not isinstance(service, str) or _SERVICE_PATTERN.fullmatch(service) is None:
        raise RuntimeConfigError("runtime.service must be a safe Windows service name")
    return service


def _run_sc(
    args: list[str], *, timeout_seconds: float, action: str
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["sc.exe", *args],
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("sc.exe executable is not available") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"sc.exe {action} timed out") from exc


class WindowsServiceController:
    """Restart exactly one validated Windows service; never accepts commands.

    ``sc stop`` only submits a stop request (the service may stay
    STOP_PENDING), so the stop outcome is polled with ``sc query`` until
    ``STATE : 1`` (STOPPED) or the service is reported not active (1062)
    before ``sc start`` runs. All argv lists are fixed and shell=False.
    """

    def __init__(
        self,
        service: str,
        *,
        stop_timeout_seconds: float = 60.0,
        start_timeout_seconds: float = 120.0,
        stop_poll_seconds: float = 1.0,
    ) -> None:
        if _SERVICE_PATTERN.fullmatch(service) is None:
            raise RuntimeConfigError("unsafe Windows service name")
        self._service = service
        self._stop_timeout = stop_timeout_seconds
        self._start_timeout = start_timeout_seconds
        self._stop_poll = stop_poll_seconds

    def restart(self, server_id: str) -> dict[str, Any]:
        self._stop()
        result = _run_sc(
            ["start", self._service],
            timeout_seconds=self._start_timeout,
            action="start",
        )
        if result.returncode != 0:
            raise RuntimeError(f"sc.exe start failed with exit code {result.returncode}")
        return {
            "server_id": server_id,
            "adapter": "windows_service",
            "service": self._service,
            "completed": True,
        }

    def _stop(self) -> None:
        result = _run_sc(
            ["stop", self._service],
            timeout_seconds=self._stop_timeout,
            action="stop",
        )
        if result.returncode == 0:
            self._wait_stopped()
            return
        if self._is_not_active(result):
            return  # service not running; nothing to stop
        raise RuntimeError(f"sc.exe stop failed with exit code {result.returncode}")

    def _wait_stopped(self) -> None:
        # The stop budget is a total deadline: every query gets the remaining
        # time as its own timeout, and after a STOP_PENDING result the
        # remaining budget is recomputed before sleeping so a slow query can
        # never push the loop past the deadline.
        deadline = time.monotonic() + self._stop_timeout
        while True:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                raise RuntimeError("sc.exe stop timed out waiting for the service to stop")
            query = _run_sc(
                ["query", self._service],
                timeout_seconds=min(remaining, self._stop_timeout),
                action="query",
            )
            if query.returncode != 0:
                # Query failures fail closed: the 1062 bypass applies to the
                # stop command only (a service that is not running needs no
                # stop), never to polling, so an odd query result can never
                # trigger an early start.
                raise RuntimeError(f"sc.exe query failed with exit code {query.returncode}")
            if self._state_value(query.stdout) == _STATE_STOPPED:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("sc.exe stop timed out waiting for the service to stop")
            time.sleep(min(self._stop_poll, remaining))

    @staticmethod
    def _state_value(output: str) -> int | None:
        match = _STATE_PATTERN.search(output)
        if match is None:
            return None
        return int(match.group(1))

    @staticmethod
    def _is_not_active(result: subprocess.CompletedProcess[str]) -> bool:
        # Prefer the numeric exit code (sc.exe exit codes are the error codes);
        # text compatibility matches the "FAILED 1062" error-code field only,
        # so a service name echoing "1062" inside a different failure can never
        # be mistaken for SERVICE_NOT_ACTIVE.
        if result.returncode == 1062:
            return True
        if result.returncode == 0:
            return False
        return (
            _NOT_ACTIVE_TEXT.search(result.stdout) is not None
            or _NOT_ACTIVE_TEXT.search(result.stderr) is not None
        )


__all__ = ["WindowsServiceController", "windows_service_from_config"]
