"""Tests for the comfyui.output.image export helper and its registration."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from mcp.shared.exceptions import MCPError

from comfyui_mcp_skills.adapters.mcp.output_image import (
    IMAGE_TAG_DIR_ENV,
    OutputOwnership,
    _contained_destination,
    export_output_image,
    sniff_image_mime,
)
from comfyui_mcp_skills.domain.models import Artifact

_PAYLOAD = b"\x89PNG\r\n\x1a\n-payload"
_JPEG_PAYLOAD = b"\xff\xd8\xff\xe0-payload"


class _FakeGateway:
    """Minimal stand-in for the ComfyUI gateway download surface."""

    def __init__(
        self,
        *,
        fail: bool = False,
        payload: bytes = _PAYLOAD,
        sha256: str = "0" * 64,
    ) -> None:
        self.fail = fail
        self.payload = payload
        self.receipt_sha256 = sha256
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
        return self.payload

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
        target.write_bytes(self.payload)
        return {"size_bytes": len(self.payload), "sha256": self.receipt_sha256}


class _Artifacts:
    """Owner-bound Artifact lookup stand-in."""

    def __init__(self, artifacts: list[Artifact]) -> None:
        self._artifacts = artifacts
        self.calls: list[tuple[str, str]] = []

    def list_artifacts_for_job(self, job_id: str, owner_id: str) -> Sequence[Artifact]:
        self.calls.append((job_id, owner_id))
        return tuple(
            artifact
            for artifact in self._artifacts
            if artifact.job_id == job_id and owner_id == "alice"
        )


def _artifact(
    *, filename: str = "out.png", completeness: str = "locator_only", sha256: str = ""
) -> Artifact:
    return Artifact(
        artifact_id="artifact_" + "a" * 16,
        job_id="job_" + "j" * 16,
        server_id="local",
        upstream_node_id="9",
        output_key="images",
        upstream_output_index=0,
        filename=filename,
        subfolder="",
        storage_type="output",
        media_type="image",
        digest="d" * 64,
        created_at="2026-08-03T12:00:00+00:00",
        mime_type="image/png",
        size_bytes=len(_PAYLOAD),
        sha256=sha256,
        completeness=completeness,  # type: ignore[arg-type]
    )


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
    transferred_name, transferred_target, transferred_subfolder, transferred_type = (
        gateway.transfers[0]
    )
    assert (transferred_name, transferred_subfolder, transferred_type) == ("out.png", "", "output")
    # The transfer target is re-suffixed from the sniffed MIME type, so it shares
    # the directory and stem of the final file instead of copying its extension.
    final = _written_path(tmp_path, saved_path)
    assert transferred_target.parent == final.parent
    assert transferred_target.stem == final.stem
    assert final.suffix == ".png"


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
    # Execution can fetch its own Job's image; operations stays out of scope.
    assert not tool_visible(
        "comfyui.output.image", Toolset.OPERATIONS, admitted_scopes(Toolset.OPERATIONS)
    )
    assert tool_visible(
        "comfyui.output.image", Toolset.EXECUTION, admitted_scopes(Toolset.EXECUTION)
    )


def test_owned_artifact_is_verified_and_reported(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(IMAGE_TAG_DIR_ENV, raising=False)
    artifacts = _Artifacts([_artifact()])

    exported = export_output_image(
        gateway=_FakeGateway(),
        server_id="local",
        filename="out.png",
        ownership=OutputOwnership(
            owner_id="alice", job_id="job_" + "j" * 16, artifacts=artifacts
        ),
    )

    assert exported.payload == _PAYLOAD
    assert exported.document["ownership"] == {
        "verified": True,
        "owner_id": "alice",
        "job_id": "job_" + "j" * 16,
        "artifact_ids": ["artifact_" + "a" * 16],
    }
    assert artifacts.calls == [("job_" + "j" * 16, "alice")]


def test_foreign_owner_or_unknown_job_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(IMAGE_TAG_DIR_ENV, raising=False)
    artifact = _artifact()

    with pytest.raises(MCPError, match="not owned"):
        export_output_image(
            gateway=_FakeGateway(),
            server_id="local",
            filename="out.png",
            ownership=OutputOwnership(
                owner_id="bob", job_id=artifact.job_id, artifacts=_Artifacts([artifact])
            ),
        )
    # A foreign principal cannot even learn whether the filename exists.
    with pytest.raises(MCPError, match="not owned"):
        export_output_image(
            gateway=_FakeGateway(),
            server_id="local",
            filename="someone-else.png",
            ownership=OutputOwnership(
                owner_id="alice", job_id=artifact.job_id, artifacts=_Artifacts([artifact])
            ),
        )


def test_verified_artifact_digest_mismatch_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(IMAGE_TAG_DIR_ENV, raising=False)
    verified = _artifact(completeness="verified", sha256="f" * 64)

    with pytest.raises(MCPError, match="does not match"):
        export_output_image(
            gateway=_FakeGateway(),
            server_id="local",
            filename="out.png",
            ownership=OutputOwnership(
                owner_id="alice", job_id=verified.job_id, artifacts=_Artifacts([verified])
            ),
        )

    matching = _artifact(
        completeness="verified", sha256=hashlib.sha256(_PAYLOAD).hexdigest()
    )
    exported = export_output_image(
        gateway=_FakeGateway(sha256=matching.sha256),
        server_id="local",
        filename="out.png",
        ownership=OutputOwnership(
            owner_id="alice", job_id=matching.job_id, artifacts=_Artifacts([matching])
        ),
    )
    assert exported.payload == _PAYLOAD


def test_spoofed_extension_reports_the_real_mime_type(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(IMAGE_TAG_DIR_ENV, str(tmp_path))

    exported = export_output_image(
        gateway=_FakeGateway(payload=_JPEG_PAYLOAD),
        server_id="local",
        filename="out.png",
    )

    assert exported.document["mime_type"] == "image/jpeg"
    saved = _written_path(tmp_path, exported.document["saved_path"])
    assert saved.suffix != ".png"
    assert saved.read_bytes() == _JPEG_PAYLOAD


def test_non_image_payload_is_rejected_and_not_left_on_disk(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(IMAGE_TAG_DIR_ENV, str(tmp_path))

    with pytest.raises(MCPError, match="not a recognized image"):
        export_output_image(
            gateway=_FakeGateway(payload=b"<html>not an image</html>"),
            server_id="local",
            filename="out.png",
        )

    assert list(tmp_path.rglob("img-*")) == []
    assert sniff_image_mime(b"<html>") == ""


def test_destination_must_stay_inside_the_tag_root(tmp_path: Path) -> None:
    root = tmp_path / "tag-root"
    root.mkdir()

    assert _contained_destination(root, "2026-08-03", "img.png") == (
        root / "2026-08-03" / "img.png"
    ).resolve()
    with pytest.raises(MCPError, match="Unsafe output image destination"):
        _contained_destination(root, "..", "escape.png")
    with pytest.raises(MCPError, match="Unsafe output image destination"):
        _contained_destination(root, "2026-08-03", "..", "..", "escape.png")
