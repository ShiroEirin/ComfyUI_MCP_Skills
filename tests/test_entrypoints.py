"""Process entry-point configuration contracts."""

from __future__ import annotations

import os
from pathlib import Path

from comfyui_mcp_skills.__main__ import _configured_upload_roots


def test_stdio_upload_root_defaults_to_dedicated_directory(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.delenv("COMFYUI_MCP_UPLOAD_ROOTS", raising=False)

    assert _configured_upload_roots(tmp_path) == [(tmp_path / "uploads").resolve()]


def test_stdio_upload_roots_support_explicit_multiple_roots(
    tmp_path: Path, monkeypatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv(
        "COMFYUI_MCP_UPLOAD_ROOTS", os.pathsep.join((str(first), str(second)))
    )

    assert _configured_upload_roots(tmp_path) == [first.resolve(), second.resolve()]
