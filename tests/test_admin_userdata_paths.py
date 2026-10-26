"""userdata path guard: nested/space/Unicode names allowed, escapes rejected."""

from __future__ import annotations

import pytest

from comfyui_mcp_skills.adapters.mcp.admin import _safe_userdata_path

ALLOWED = [
    "workflows/a.json",
    "workflows/nooba/noobAI基础文生图工作流.json",
    "workflows/with space.json",
    "workflows/nested dir/base v1.2-final_x.json",
    "workflows/名前/ワークフロー.json",
    "workflows/A1/._legacy copy.json",
    "workflows/dots.in.the.name.JSON",
]

REJECTED = [
    # traversal / relative escapes
    "../escape.json",
    "workflows/../escape.json",
    "workflows/nested/../../escape.json",
    "workflows/./a.json",
    # absolute paths, including Windows drive letters and UNC shares
    "C:\\workflows\\a.json",
    "C:/workflows/a.json",
    "/workflows/a.json",
    "/a.json",
    "\\\\server\\share\\a.json",
    # empty segments / roots / normalization failures
    "workflows//a.json",
    "workflows/nested//a.json",
    "workflows/",
    "workflows",
    # suffix and name rules
    "workflows/not-json.txt",
    "workflows/.json",
    "workflows/a.json.bak",
    # URL / protocol prefixes
    "http://127.0.0.1:8188/workflows/a.json",
    "file:///etc/passwd.json",
    "workflows/a.json?x=1",
    # control characters and characters Windows refuses
    "workflows/bad\nname.json",
    "workflows/bad\x00name.json",
    "workflows/bad\x7fname.json",
    "workflows/bad:name.json",
    "workflows/bad|name.json",
    "workflows/ lead.json",
    "workflows/trailing-space .json ",
    # depth / length caps
    "workflows/" + "/".join("dir" for _ in range(8)) + "/a.json",
    "workflows/" + "a" * 1024 + ".json",
]


@pytest.mark.parametrize("value", ALLOWED)
def test_userdata_path_accepts_real_workflow_layouts(value: str) -> None:
    assert _safe_userdata_path(value) is True, value


@pytest.mark.parametrize("value", REJECTED)
def test_userdata_path_rejects_escapes_and_non_json(value: str) -> None:
    assert _safe_userdata_path(value) is False, value


@pytest.mark.parametrize("value", [None, 7, b"workflows/a.json", "", ["workflows/a.json"]])
def test_userdata_path_rejects_non_strings(value: object) -> None:
    assert _safe_userdata_path(value) is False
