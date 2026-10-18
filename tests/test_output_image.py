"""Tests for the comfyui.output.image export helper and its registration."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from mcp.shared.exceptions import MCPError

from comfyui_mcp_skills.adapters.mcp.output_image import (
    IMAGE_TAG_DIR_ENV,
    export_output_image,
)

_PAYLOAD = b"\x89PNG\r\n\x1a\n-payload"


class _FakeGateway:
    """Minimal stand-in for the ComfyUI gateway download surface."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.downloads: list[tuple[str, str, str]] = []
        self.transfers: list[tuple[str, Path, str, str]] = []

    def download_output(
        self,
        filename: str,
        subfolder: str = "",
        output_type: str = "output",
        *,
        max_bytes: int,
    ) -> bytes:
        self.downloads.append((filename, subfolder, output_type))
        if self.fail:
            raise ValueError(f"Output exceeds {max_bytes} byte limit")
        return _PAYLOAD

    def download_output_to(
        self,
        filename: str,
        destination: str | Path,
        subfolder: str = "",
        storage_type: str = "output",
        *,
        max_bytes: int,
    ) -> dict[str, int | str]:
        target = Path(destination)
        self.transfers.append((filename, target, subfolder, storage_type))
        if self.fail:
            raise ValueError(f"Output exceeds {max_bytes} byte limit")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_PAYLOAD)
        return {"size_bytes": len(_PAYLOAD), "sha256": "0" * 64}


def _written_path(root: Path, saved_path: str) -> Path:
    return root / Path(saved_path).relative_to("upload")


def test_default_mode_returns_payload_without_writing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(IMAGE_TAG_DIR_ENV, raising=False)
    gateway = _FakeGateway()

    exported = export_output_image(gateway=gateway, server_id="local", filename="out.png")

    assert exported.document == {
        "server_id": "local",
        "filename": "out.png",
        "subfolder": "",
        "storage_type": "output",
        "mime_type": "image/png",
        "size_bytes": len(_PAYLOAD),
        "image_tag": None,
    }
    assert exported.payload == _PAYLOAD
    assert gateway.downloads == [("out.png", "", "output")]
    assert gateway.transfers == []
    assert list(tmp_path.iterdir()) == []


def test_tag_mode_writes_file_and_returns_renderable_tag(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(IMAGE_TAG_DIR_ENV, str(tmp_path))
    gateway = _FakeGateway()

    exported = export_output_image(gateway=gateway, server_id="local", filename="out.png")

    saved_path = exported.document["saved_path"]
    assert saved_path.startswith("upload/")
    assert exported.document["image_tag"] == f"@@image:{saved_path}@@"
    assert exported.document["size_bytes"] == len(_PAYLOAD)
    assert exported.payload is None
    assert _written_path(tmp_path, saved_path).read_bytes() == _PAYLOAD
    assert gateway.downloads == []
    assert gateway.transfers == [("out.png", _written_path(tmp_path, saved_path), "", "output")]


def test_explicit_tag_root_overrides_environment(tmp_path: Path, monkeypatch) -> None:
    env_root = tmp_path / "from-env"
    arg_root = tmp_path / "from-arg"
    monkeypatch.setenv(IMAGE_TAG_DIR_ENV, str(env_root))

    exported = export_output_image(
        gateway=_FakeGateway(),
        server_id="local",
        filename="out.png",
        tag_root=str(arg_root),
    )

    assert _written_path(arg_root, exported.document["saved_path"]).exists()
    assert not env_root.exists()


def test_blank_tag_root_keeps_default_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(IMAGE_TAG_DIR_ENV, "   ")
    gateway = _FakeGateway()

    exported = export_output_image(gateway=gateway, server_id="local", filename="out.png")

    assert exported.document["image_tag"] is None
    assert exported.payload == _PAYLOAD
    assert gateway.transfers == []
    assert list(tmp_path.iterdir()) == []


def test_filename_without_extension_still_yields_a_saved_path(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(IMAGE_TAG_DIR_ENV, str(tmp_path))

    exported = export_output_image(gateway=_FakeGateway(), server_id="local", filename="out")

    assert exported.document["saved_path"].startswith("upload/")
    assert "." in Path(exported.document["saved_path"]).name


def test_subfolder_is_validated_and_forwarded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(IMAGE_TAG_DIR_ENV, raising=False)
    gateway = _FakeGateway()

    exported = export_output_image(
        gateway=gateway,
        server_id="local",
        filename="out.png",
        subfolder="sub/dir",
    )

    assert exported.document["subfolder"] == "sub/dir"
    assert gateway.downloads == [("out.png", "sub/dir", "output")]


def test_unsafe_locator_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(IMAGE_TAG_DIR_ENV, str(tmp_path))

    with pytest.raises(MCPError):
        export_output_image(gateway=_FakeGateway(), server_id="local", filename="../evil.png")


def test_unsupported_storage_type_is_rejected() -> None:
    with pytest.raises(MCPError):
        export_output_image(
            gateway=_FakeGateway(),
            server_id="local",
            filename="out.png",
            storage_type="temp",
        )


def test_oversized_output_is_not_silently_truncated(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(IMAGE_TAG_DIR_ENV, str(tmp_path))

    with pytest.raises(ValueError):
        export_output_image(
            gateway=_FakeGateway(fail=True),
            server_id="local",
            filename="out.png",
        )


def test_tool_result_with_image_emits_native_image_block() -> None:
    from comfyui_mcp_skills.adapters.mcp.tooling import tool_result_with_image

    document = {"server_id": "local", "filename": "out.png", "mime_type": "image/png"}
    result = tool_result_with_image(document, payload=_PAYLOAD, mime_type="image/png")

    assert result.is_error is False
    assert len(result.content) == 2
    text_block, image_block = result.content
    assert text_block.type == "text"
    assert json.loads(text_block.text) == document
    assert image_block.type == "image"
    assert image_block.mime_type == "image/png"
    assert base64.b64decode(image_block.data) == _PAYLOAD
    assert result.structured_content == document


def test_tool_is_registered_and_scoped_to_authoring() -> None:
    from comfyui_mcp_skills.adapters.mcp.tooling import fixed_tools
    from comfyui_mcp_skills.application.authorization import (
        Toolset,
        admitted_scopes,
        tool_visible,
    )

    tools = {tool.name: tool for tool in fixed_tools()}

    assert "comfyui.output.image" in tools
    schema = tools["comfyui.output.image"].input_schema
    assert schema["required"] == ["server_id", "filename"]
    assert schema["additionalProperties"] is False
    assert tool_visible(
        "comfyui.output.image", Toolset.AUTHORING, admitted_scopes(Toolset.AUTHORING)
    )
    # The export stays off the operations and execution surfaces so their
    # fixed Tool budgets are unchanged.
    assert not tool_visible(
        "comfyui.output.image", Toolset.OPERATIONS, admitted_scopes(Toolset.OPERATIONS)
    )
    assert not tool_visible(
        "comfyui.output.image", Toolset.EXECUTION, admitted_scopes(Toolset.EXECUTION)
    )
