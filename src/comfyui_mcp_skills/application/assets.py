"""Authorize, inspect, stream, and record ComfyUI input assets."""

from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Literal, Protocol

from comfyui_mcp_skills.application.ports import AssetRepository
from comfyui_mcp_skills.domain.errors import (
    AssetNotFound,
    ComfyUISkillsError,
    PayloadTooLarge,
    UnsafePath,
    UnsupportedMediaType,
    UploadFailed,
)
from comfyui_mcp_skills.domain.media import validate_media_locator
from comfyui_mcp_skills.domain.models import Asset


class AssetGateway(Protocol):
    def upload_file(self, path: str, *, purpose: str, original_ref: str) -> dict[str, Any]: ...


MediaType = Literal["image", "audio", "video"]

_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"ID3", "audio/mpeg"),
    (b"OggS", "audio/ogg"),
    (b"fLaC", "audio/flac"),
    (b"\x1aE\xdf\xa3", "video/webm"),
)
_EXTENSION_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}


def classify_media_type(mime_type: str, fallback: MediaType = "image") -> MediaType:
    """Map a public MIME representation to its canonical media family."""
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("audio/"):
        return "audio"
    if mime_type.startswith("video/"):
        return "video"
    return fallback


def detect_media(path: Path, prefix: bytes) -> tuple[str, MediaType]:
    """Identify supported media from bounded signature bytes and its extension."""
    detected_mime: str | None
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        detected_mime = "image/webp"
    elif prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE":
        detected_mime = "audio/wav"
    elif len(prefix) >= 8 and prefix[4:8] == b"ftyp":
        detected_mime = "video/mp4"
    elif _is_mpeg_audio_frame(prefix):
        detected_mime = "audio/mpeg"
    else:
        detected_mime = next(
            (mime for signature, mime in _SIGNATURES if prefix.startswith(signature)),
            None,
        )
    expected = _EXTENSION_MIME.get(path.suffix.lower())
    if detected_mime is None or expected != detected_mime:
        raise UnsupportedMediaType("File content does not match a supported media extension")
    return detected_mime, classify_media_type(detected_mime)


def _is_mpeg_audio_frame(prefix: bytes) -> bool:
    if len(prefix) < 4 or prefix[0] != 0xFF or prefix[1] & 0xE0 != 0xE0:
        return False
    version = (prefix[1] >> 3) & 0x03
    layer = (prefix[1] >> 1) & 0x03
    bitrate = (prefix[2] >> 4) & 0x0F
    sample_rate = (prefix[2] >> 2) & 0x03
    return version != 0x01 and layer != 0 and bitrate not in {0, 0x0F} and sample_rate != 0x03


def same_file_stat(first: os.stat_result, second: os.stat_result) -> bool:
    """Two stat results refer to the same unmodified file (dev, inode, size)."""
    return (
        first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_size == second.st_size
    )


def configured_upload_roots(base_dir: Path) -> list[Path]:
    """Resolve authorized local upload roots from the deployment environment.

    Without ``COMFYUI_MCP_UPLOAD_ROOTS`` the default is ``<base_dir>/uploads``;
    with it configured, only the configured roots are authorized.
    """
    import os

    configured = os.environ.get("COMFYUI_MCP_UPLOAD_ROOTS", "")
    if not configured:
        return [(base_dir / "uploads").resolve()]
    roots: list[Path] = []
    for value in configured.split(os.pathsep):
        if not value.strip():
            continue
        root = Path(value.strip()).expanduser()
        if not root.is_absolute():
            root = base_dir / root
        roots.append(root.resolve())
    if not roots:
        raise ValueError("COMFYUI_MCP_UPLOAD_ROOTS must contain at least one path")
    return roots


class AssetService:
    def __init__(
        self,
        repository: AssetRepository,
        *,
        upload_roots: list[Path],
        max_bytes: int = 100 * 1024 * 1024,
        staging_root: Path | None = None,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._repository = repository
        self._roots = tuple(root.resolve() for root in upload_roots)
        self._max_bytes = max_bytes
        self._staging_root = (
            staging_root.resolve()
            if staging_root is not None
            else Path(tempfile.gettempdir()).resolve() / "comfyui-mcp-skills"
        )
        self._staging_root.mkdir(parents=True, exist_ok=True)

    def upload_local(
        self,
        gateway: AssetGateway,
        server_id: str,
        path: str | Path,
        *,
        purpose: str,
        original_asset_id: str = "",
        owner_id: str = "",
    ) -> Asset:
        try:
            source = Path(path).expanduser().resolve(strict=True)
            if not source.is_file() or not self._is_authorized(source):
                raise UnsafePath("File is outside authorized upload roots")
            expected = source.stat()
        except UnsafePath:
            raise
        except FileNotFoundError as exc:
            raise UnsafePath("Upload file does not exist") from exc
        except (OSError, RuntimeError) as exc:
            raise UnsafePath("Upload file could not be inspected") from exc
        size = expected.st_size
        if size > self._max_bytes:
            raise PayloadTooLarge(
                f"File exceeds {self._max_bytes} byte upload limit",
                details={"size_bytes": size, "max_bytes": self._max_bytes},
            )
        expected_media = "image" if purpose == "mask" else purpose
        if expected_media not in {"image", "audio", "video"}:
            raise UnsupportedMediaType("Unsupported upload purpose")
        original_ref = ""
        if original_asset_id:
            original = self._repository.get(original_asset_id)
            if (
                original is None
                or original.server_id != server_id
                or original.media_type != "image"
                or (owner_id and original.owner_id != owner_id)
            ):
                raise AssetNotFound("Original Asset was not found")
            original_ref = original.comfyui_ref
        staged = self._staging_root / f"{uuid.uuid4().hex}-{source.name}"
        digest = hashlib.sha256()
        try:
            try:
                with source.open("rb") as source_file:
                    opened = os.fstat(source_file.fileno())
                    if not self._same_file(expected, opened):
                        raise UnsafePath("Upload file was replaced before it could be read")
                    prefix = source_file.read(16)
                    mime_type, media_type = detect_media(source, prefix)
                    if media_type != expected_media:
                        raise UnsupportedMediaType(f"{purpose} requires a {expected_media} file")
                    with staged.open("xb") as staged_file:
                        staged_file.write(prefix)
                        digest.update(prefix)
                        copied = len(prefix)
                        while chunk := source_file.read(1024 * 1024):
                            copied += len(chunk)
                            if copied > self._max_bytes:
                                raise PayloadTooLarge(
                                    f"File exceeds {self._max_bytes} byte upload limit"
                                )
                            staged_file.write(chunk)
                            digest.update(chunk)
                    after_read = os.fstat(source_file.fileno())
                try:
                    current = source.stat()
                except FileNotFoundError as exc:
                    raise UnsafePath("Upload file was replaced while being read") from exc
                if (
                    copied != size
                    or not self._same_file(opened, after_read)
                    or not self._same_file(opened, current)
                ):
                    raise UnsafePath("Upload file was replaced while being read")
            except ComfyUISkillsError:
                raise
            except Exception as exc:
                raise UploadFailed("Upload file could not be prepared") from exc
            try:
                uploaded = gateway.upload_file(
                    str(staged), purpose=purpose, original_ref=original_ref
                )
            except Exception as exc:
                raise UploadFailed("ComfyUI upload failed") from exc
        finally:
            try:
                staged.unlink(missing_ok=True)
            except OSError as exc:
                raise UploadFailed("Upload staging cleanup failed") from exc
        try:
            name, subfolder = validate_media_locator(
                uploaded.get("name"), uploaded.get("subfolder", "")
            )
        except (AttributeError, ValueError) as exc:
            raise UploadFailed("ComfyUI upload returned an unsafe media locator") from exc
        comfyui_ref = f"{subfolder}/{name}" if subfolder else name
        asset = Asset(
            asset_id=f"asset_{uuid.uuid4().hex}",
            server_id=server_id,
            comfyui_ref=comfyui_ref,
            name=name,
            subfolder=subfolder,
            media_type=media_type,
            mime_type=mime_type,
            size_bytes=size,
            owner_id=owner_id,
            sha256=digest.hexdigest(),
        )
        self._repository.save(asset)
        return asset

    def get(self, asset_id: str, *, owner_id: str = "") -> Asset:
        asset = self._repository.get(asset_id)
        if asset is None or (owner_id and asset.owner_id != owner_id):
            raise AssetNotFound(f"Asset not found: {asset_id}")
        try:
            name, subfolder = validate_media_locator(asset.name, asset.subfolder)
        except ValueError as exc:
            raise AssetNotFound(f"Asset has an unsafe media locator: {asset_id}") from exc
        expected_ref = f"{subfolder}/{name}" if subfolder else name
        if asset.comfyui_ref != expected_ref:
            raise AssetNotFound(f"Asset has an unsafe media locator: {asset_id}")
        return asset

    def _is_authorized(self, path: Path) -> bool:
        for root in self._roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
        return same_file_stat(first, second)
