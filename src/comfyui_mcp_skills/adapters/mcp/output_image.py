"""Export one ComfyUI output image, optionally as a host-renderable tag.

Two modes share the same validation and download path:

* default (no tag directory configured): the image bytes are handed back to the
  caller so the adapter can emit a native MCP image block -- the host renders,
  persists and de-duplicates it on its own. This server writes nothing.
* tag mode (``COMFYUI_MCP_IMAGE_TAG_DIR`` set, or ``tag_root`` passed): the
  image is stored under ``<root>/<YYYY-MM-DD>/`` and the document carries a
  ``@@image:upload/<date>/<name>@@`` tag for hosts that render image tags.

Every export is content-checked: the MIME type comes from magic bytes rather
than the filename extension, so a renamed non-image (or a renamed JPEG behind a
``.png`` name) is reported as what it really is. When the caller can supply
ownership facts (``OutputOwnership``), the image must be a recorded Artifact of
that owner's Job -- a filename alone never authorizes a download.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, INVALID_REQUEST

from comfyui_mcp_skills.domain.media import MAX_OUTPUT_BYTES, validate_media_locator
from comfyui_mcp_skills.domain.models import Artifact

IMAGE_TAG_DIR_ENV = "COMFYUI_MCP_IMAGE_TAG_DIR"
_UPLOAD_PREFIX = "upload"
_SUPPORTED_STORAGE_TYPES = frozenset({"output"})
_SNIFF_BYTES = 32
_MAX_ARTIFACT_IDS = 16


@dataclass(frozen=True)
class OutputImageExport:
    """One exported output image plus the metadata document describing it.

    ``payload`` carries the image bytes in the default mode so the caller can
    emit a native image block; in tag mode the bytes were already written to
    disk and ``payload`` stays ``None``.
    """

    document: dict[str, Any]
    payload: bytes | None


class ArtifactLookup(Protocol):
    """The owner-bound Artifact lookup the export verifies against."""

    def list_artifacts_for_job(self, job_id: str, owner_id: str) -> Sequence[Artifact]: ...


@dataclass(frozen=True)
class OutputOwnership:
    """Facts that prove one output image belongs to the caller's Job.

    ``artifacts`` is the run/artifact repository: the lookup is owner-bound, so
    another owner's Job is unreachable even with a correct filename. Pass this
    whenever the caller knows the owning principal and Job.
    """

    owner_id: str
    job_id: str
    artifacts: ArtifactLookup


def _declared_mime_type(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or ""


def sniff_image_mime(payload: bytes) -> str:
    """Return the image MIME type from magic bytes, or ``""`` when unknown."""
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    if payload[:4] == b"RIFF" and payload[8:12] == b"AVIF":
        return "image/avif"
    if payload[4:8] == b"ftyp" and payload[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    if payload.startswith(b"BM") and len(payload) >= 14 and payload[6:10] == b"\x00" * 4:
        return "image/bmp"
    if payload.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    return ""


def _image_mime_type(payload: bytes) -> str:
    """Sniffed MIME type; an unrecognized payload is refused, never guessed."""
    sniffed = sniff_image_mime(payload)
    if not sniffed:
        raise MCPError(  # a renamed non-image must not be served as an image
            code=INVALID_PARAMS,
            message="Output content is not a recognized image",
        )
    return sniffed


def _suffix_for(filename: str, mime_type: str) -> str:
    suffix = Path(filename).suffix
    if suffix and _declared_mime_type(filename) == mime_type:
        return suffix
    # The name disagrees with the bytes (or has no extension): trust the bytes.
    return mimetypes.guess_extension(mime_type) or suffix or ".bin"


def _resolve_tag_root(tag_root: str | None) -> str:
    candidate = tag_root if tag_root is not None else os.environ.get(IMAGE_TAG_DIR_ENV, "")
    return candidate.strip()


def _contained_destination(root: Path, *parts: str) -> Path:
    """Resolve a destination that must stay inside ``root`` (no traversal)."""
    base = root.resolve()
    destination = base.joinpath(*parts).resolve()
    try:
        destination.relative_to(base)
    except ValueError as exc:  # escape attempt: refuse before touching the disk
        raise MCPError(
            code=INVALID_PARAMS, message="Unsafe output image destination"
        ) from exc
    return destination


def _matching_artifacts(
    ownership: OutputOwnership, *, filename: str, subfolder: str
) -> tuple[Artifact, ...]:
    """Return this owner's Artifacts that describe the requested image."""
    records = tuple(
        ownership.artifacts.list_artifacts_for_job(ownership.job_id, ownership.owner_id)
    )
    matched = tuple(
        artifact
        for artifact in records
        if str(artifact.filename).casefold() == filename.casefold()
        and str(artifact.subfolder).replace("\\", "/").casefold() == subfolder.casefold()
    )
    if not matched:
        # Unknown or foreign Job/media: the same error either way, so a caller
        # cannot use this tool to probe another owner's Job.
        raise MCPError(
            code=INVALID_REQUEST,
            message="Output image is not owned by this Job",
        )
    return matched


def _verify_content(matched: Sequence[Artifact], *, size_bytes: int, sha256: str) -> None:
    """Reject content whose verified Artifact digest does not match the bytes."""
    for artifact in matched:
        if artifact.completeness != "verified" or not artifact.sha256:
            continue
        if artifact.size_bytes is not None and artifact.size_bytes != size_bytes:
            raise MCPError(
                code=INVALID_REQUEST,
                message="Output image does not match its recorded Artifact size",
            )
        if artifact.sha256.lower() != sha256.lower():
            raise MCPError(
                code=INVALID_REQUEST,
                message="Output image does not match its recorded Artifact digest",
            )
        return


def _ownership_receipt(
    ownership: OutputOwnership | None, matched: Sequence[Artifact]
) -> dict[str, Any]:
    return {
        "verified": True,
        "owner_id": ownership.owner_id if ownership is not None else "",
        "job_id": ownership.job_id if ownership is not None else "",
        "artifact_ids": [artifact.artifact_id for artifact in matched[: _MAX_ARTIFACT_IDS]],
    }


def export_output_image(
    *,
    gateway: Any,
    server_id: str,
    filename: str,
    subfolder: str = "",
    storage_type: str = "output",
    tag_root: str | None = None,
    ownership: OutputOwnership | None = None,
) -> OutputImageExport:
    """Download one ComfyUI output image and describe it for the caller."""
    if storage_type not in _SUPPORTED_STORAGE_TYPES:
        raise MCPError(code=INVALID_PARAMS, message="Unsupported output storage type")
    try:
        safe_filename, safe_subfolder = validate_media_locator(filename, subfolder)
    except ValueError as exc:
        raise MCPError(code=INVALID_PARAMS, message="Unsafe output media locator") from exc

    matched: tuple[Artifact, ...] = ()
    document: dict[str, Any] = {
        "server_id": server_id,
        "filename": safe_filename,
        "subfolder": safe_subfolder,
        "storage_type": storage_type,
    }
    if ownership is not None:
        matched = _matching_artifacts(
            ownership, filename=safe_filename, subfolder=safe_subfolder
        )
        document["ownership"] = _ownership_receipt(ownership, matched)

    resolved_root = _resolve_tag_root(tag_root)
    if not resolved_root:
        payload = gateway.download_output(
            safe_filename,
            safe_subfolder,
            storage_type,
            max_bytes=MAX_OUTPUT_BYTES,
        )
        mime_type = _image_mime_type(payload)
        content = {**document, "mime_type": mime_type}
        if matched:
            _verify_content(
                matched,
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        return OutputImageExport(
            document={**content, "size_bytes": len(payload), "image_tag": None},
            payload=payload,
        )

    # The bytes are only known after the transfer, so sniff the written header
    # and keep the true type instead of the extension guess.
    date_dir = datetime.now().strftime("%Y-%m-%d")
    unique = secrets.token_hex(4)
    provisional = f"img-{int(time.time() * 1000)}-{unique}"
    destination = _contained_destination(Path(resolved_root), date_dir, provisional)
    transferred = gateway.download_output_to(
        safe_filename,
        destination,
        safe_subfolder,
        storage_type,
        max_bytes=MAX_OUTPUT_BYTES,
    )
    try:
        with destination.open("rb") as handle:
            header = handle.read(_SNIFF_BYTES)
        mime_type = _image_mime_type(header)
    except BaseException:
        # Never leave an unusable payload behind under a renderable tag.
        destination.unlink(missing_ok=True)
        raise
    size_bytes = int(transferred["size_bytes"])
    if matched:
        digest = str(transferred.get("sha256", ""))
        if digest:
            _verify_content(matched, size_bytes=size_bytes, sha256=digest)
    final = _contained_destination(
        Path(resolved_root), date_dir, f"{provisional}{_suffix_for(safe_filename, mime_type)}"
    )
    destination.replace(final)
    relative = f"{_UPLOAD_PREFIX}/{date_dir}/{final.name}"
    return OutputImageExport(
        document={
            **document,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "saved_path": relative,
            "image_tag": f"@@image:{relative}@@",
        },
        payload=None,
    )
