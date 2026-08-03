"""Asset upload service contracts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from comfyui_mcp_skills.application.assets import AssetService
from comfyui_mcp_skills.domain.errors import (
    AssetNotFound,
    PayloadTooLarge,
    UnsafePath,
    UnsupportedMediaType,
)
from comfyui_mcp_skills.infrastructure.persistence.assets import FileAssetRepository


def _png(path: Path, payload: bytes = b"") -> Path:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + payload)
    return path


def _service(tmp_path: Path, root: Path, *, max_bytes: int = 1024) -> AssetService:
    return AssetService(
        FileAssetRepository(tmp_path),
        upload_roots=[root],
        max_bytes=max_bytes,
    )


def test_uploads_authorized_image_and_returns_asset_handle(tmp_path: Path) -> None:
    upload_root = tmp_path / "allowed"
    upload_root.mkdir()
    image = _png(upload_root / "cat.png", b"pixels")
    gateway = MagicMock()
    gateway.upload_file.return_value = {
        "name": "cat.png",
        "subfolder": "agent/assets",
        "type": "input",
    }

    asset = _service(tmp_path, upload_root).upload_local(gateway, "local", image, purpose="image")

    assert asset.asset_id.startswith("asset_")
    assert asset.comfyui_ref == "agent/assets/cat.png"
    assert asset.mime_type == "image/png"
    assert asset.size_bytes == image.stat().st_size
    assert "local_path" not in asset.to_public_dict()
    uploaded_path = Path(gateway.upload_file.call_args.args[0])
    assert uploaded_path.parent != image.parent
    assert uploaded_path.name.endswith("-cat.png")
    assert not uploaded_path.exists()
    assert gateway.upload_file.call_args.kwargs == {"purpose": "image", "original_ref": ""}


def test_upload_rejects_source_replacement_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_root = tmp_path / "allowed"
    upload_root.mkdir()
    image = _png(upload_root / "cat.png", b"pixels")
    replacement = _png(tmp_path / "replacement.png", b"other")
    gateway = MagicMock()
    original_open = Path.open
    swapped = False

    def swap_before_open(path: Path, *args, **kwargs):
        nonlocal swapped
        if path == image and not swapped:
            swapped = True
            replacement.replace(image)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", swap_before_open)
    with pytest.raises(UnsafePath):
        _service(tmp_path, upload_root).upload_local(gateway, "local", image, purpose="image")
    gateway.upload_file.assert_not_called()


def test_accepts_bare_mpeg_audio_frame(tmp_path: Path) -> None:
    upload_root = tmp_path / "allowed"
    upload_root.mkdir()
    audio = upload_root / "sample.mp3"
    audio.write_bytes(b"\xff\xfb\x90\x64" + b"frame-data")
    gateway = MagicMock()
    gateway.upload_file.return_value = {"name": "sample.mp3", "subfolder": "agent", "type": "input"}

    asset = _service(tmp_path, upload_root).upload_local(gateway, "local", audio, purpose="audio")

    assert asset.media_type == "audio"
    assert asset.mime_type == "audio/mpeg"


def test_rejects_file_outside_authorized_roots(tmp_path: Path) -> None:
    upload_root = tmp_path / "allowed"
    upload_root.mkdir()
    image = _png(tmp_path / "outside.png")
    gateway = MagicMock()

    with pytest.raises(UnsafePath):
        _service(tmp_path, upload_root).upload_local(gateway, "local", image, purpose="image")

    gateway.upload_file.assert_not_called()


def test_rejects_oversized_file_before_upload(tmp_path: Path) -> None:
    upload_root = tmp_path / "allowed"
    upload_root.mkdir()
    image = _png(upload_root / "large.png", b"x" * 64)
    gateway = MagicMock()

    with pytest.raises(PayloadTooLarge):
        _service(tmp_path, upload_root, max_bytes=16).upload_local(
            gateway, "local", image, purpose="image"
        )

    gateway.upload_file.assert_not_called()


def test_rejects_extension_spoofing(tmp_path: Path) -> None:
    upload_root = tmp_path / "allowed"
    upload_root.mkdir()
    fake = upload_root / "fake.png"
    fake.write_bytes(b"not an image")

    with pytest.raises(UnsupportedMediaType):
        _service(tmp_path, upload_root).upload_local(MagicMock(), "local", fake, purpose="image")


def test_video_purpose_and_owner_are_enforced(tmp_path: Path) -> None:
    upload_root = tmp_path / "allowed"
    upload_root.mkdir()
    video = upload_root / "clip.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom")
    gateway = MagicMock()
    gateway.upload_file.return_value = {"name": "clip.mp4", "subfolder": "agent", "type": "input"}
    service = _service(tmp_path, upload_root)

    asset = service.upload_local(gateway, "local", video, purpose="video", owner_id="principal-a")

    assert asset.media_type == "video"
    assert service.get(asset.asset_id, owner_id="principal-a") == asset
    with pytest.raises(AssetNotFound):
        service.get(asset.asset_id, owner_id="principal-b")
    with pytest.raises(UnsupportedMediaType):
        service.upload_local(gateway, "local", video, purpose="audio")


def test_mask_rejects_original_asset_from_another_server(tmp_path: Path) -> None:
    upload_root = tmp_path / "allowed"
    upload_root.mkdir()
    image = _png(upload_root / "source.png", b"pixels")
    gateway = MagicMock()
    gateway.upload_file.return_value = {
        "name": "source.png",
        "subfolder": "agent",
        "type": "input",
    }
    service = _service(tmp_path, upload_root)
    original = service.upload_local(
        gateway, "server-a", image, purpose="image", owner_id="principal-a"
    )
    gateway.reset_mock()

    with pytest.raises(AssetNotFound):
        service.upload_local(
            gateway,
            "server-b",
            image,
            purpose="mask",
            original_asset_id=original.asset_id,
            owner_id="principal-a",
        )

    gateway.upload_file.assert_not_called()


def test_upload_stages_outside_source_directory(tmp_path: Path) -> None:
    upload_root = tmp_path / "read-only-source"
    staging_root = tmp_path / "staging"
    upload_root.mkdir()
    image = _png(upload_root / "cat.png", b"pixels")
    gateway = MagicMock()
    gateway.upload_file.return_value = {
        "name": "cat.png",
        "subfolder": "agent",
        "type": "input",
    }
    service = AssetService(
        FileAssetRepository(tmp_path),
        upload_roots=[upload_root],
        staging_root=staging_root,
    )

    service.upload_local(gateway, "local", image, purpose="image")

    staged_path = Path(gateway.upload_file.call_args.args[0])
    assert staged_path.parent == staging_root.resolve()
    assert not staged_path.exists()
