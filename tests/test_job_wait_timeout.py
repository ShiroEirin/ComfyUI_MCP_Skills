"""A wait deadline is a distinct outcome, never a normal-looking Job."""

from __future__ import annotations

from typing import Any, cast

from comfyui_mcp_skills.application.jobs import JobWaitTimeout


class _Job:
    status = "running"


def test_wait_timeout_is_marked_and_keeps_the_last_handle() -> None:
    job = cast(Any, _Job())
    exc = JobWaitTimeout("prompt-1", 12.345, job)

    assert exc.timed_out is True
    assert exc.waited_seconds == 12.345
    assert exc.last_status == "running"
    assert exc.job is job
    assert "prompt-1" in str(exc)
