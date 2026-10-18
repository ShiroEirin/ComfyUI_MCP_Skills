"""Export one ComfyUI output image, optionally as a host-renderable tag.

Two modes share the same validation and download path:

* default (no tag directory configured): the image bytes are handed back to the
  caller so the adapter can emit a native MCP image block -- the host renders,
  persists and de-duplicates it on its own. This server writes nothing.
* tag mode (``COMFYUI_MCP_IMAGE_TAG_DIR`` set, or ``tag_root`` passed): the
  image is stored under ``<root>/<YYYY-MM-DD>/`` and the document carries a
  ``@@image:upload/<date>/<name>@@`` tag for hosts that render image tags.
"""

from __future__ import annotations

import mimetypes
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS

from comfyui_mcp_skills.domain.media import MAX_OUTPUT_BYTES, validate_media_locator

IMAGE_TAG_DIR_ENV = "COMFYUI_MCP_IMAGE_TAG_DIR"
_UPLOAD_PREFIX = "upload"
_SUPPORTED_STORAGE_TYPES = frozenset({"output"})


@dataclass(frozen=True)
class OutputImageExport:
    """One exported output image plus the metadata document describing it.

    ``payload`` carries the image bytes in the default mode so the caller can
    emit a native image block; in tag mode the bytes were already written to
    disk and ``payload`` stays ``None``.
    """

    document: dict[str, Any]
    payload: bytes | None


def _mime_type_for(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _suffix_for(filename: str, mime_type: str) -> str:
    suffix = Path(filename).suffix
    if suffix:
        return suffix
    return mimetypes.guess_extension(mime_type) or ".bin"


def _resolve_tag_root(tag_root: str | None) -> str:
    candidate = tag_root if tag_root is not None else os.environ.get(IMAGE_TAG_DIR_ENV, "")
    return candidate.strip()


def export_output_image(
    *,
    gateway: Any,
    server_id: str,
    filename: str,
    subfolder: str = "",
    storage_type: str = "output",
    tag_root: str | None = None,
) -> OutputImageExport:
    """Download one ComfyUI output image and describe it for the caller."""
    if storage_type not in _SUPPORTED_STORAGE_TYPES:
        raise MCPError(code=INVALID_PARAMS, message="Unsupported output storage type")
    try:
        safe_filename, safe_subfolder = validate_media_locator(filename, subfolder)
    except ValueError as exc:
        raise MCPError(code=INVALID_PARAMS, message="Unsafe output media locator") from exc

    mime_type = _mime_type_for(safe_filename)
    document: dict[str, Any] = {
        "server_id": server_id,
        "filename": safe_filename,
        "subfolder": safe_subfolder,
        "storage_type": storage_type,
        "mime_type": mime_type,
    }
    resolved_root = _resolve_tag_root(tag_root)
    if not resolved_root:
        payload = gateway.download_output(
            safe_filename,
            safe_subfolder,
            storage_type,
            max_bytes=MAX_OUTPUT_BYTES,
        )
        return OutputImageExport(
            document={**document, "size_bytes": len(payload), "image_tag": None},
            payload=payload,
        )

    date_dir = datetime.now().strftime("%Y-%m-%d")
    unique = secrets.token_hex(4)
    name = f"img-{int(time.time() * 1000)}-{unique}{_suffix_for(safe_filename, mime_type)}"
    destination = Path(resolved_root) / date_dir / name
    transferred = gateway.download_output_to(
        safe_filename,
        destination,
        safe_subfolder,
        storage_type,
        max_bytes=MAX_OUTPUT_BYTES,
    )
    relative = f"{_UPLOAD_PREFIX}/{date_dir}/{name}"
    return OutputImageExport(
        document={
            **document,
            "size_bytes": int(transferred["size_bytes"]),
            "saved_path": relative,
            "image_tag": f"@@image:{relative}@@",
        },
        payload=None,
    )
