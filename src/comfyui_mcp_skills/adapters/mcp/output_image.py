"""Export one ComfyUI output image, optionally as a host-renderable tag.

Two modes share the same validation and download path:

* default (no tag directory configured): the image is fetched only so the
  caller can confirm it exists and learn its size; nothing is written to disk
  and ``image_tag`` stays ``None``.
* tag mode (``COMFYUI_MCP_IMAGE_TAG_DIR`` set, or ``tag_root`` passed): the
  image is stored under ``<root>/<YYYY-MM-DD>/`` and the result carries a
  ``@@image:upload/<date>/<name>@@`` tag for hosts that render image tags.
"""

from __future__ import annotations

import mimetypes
import os
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS

from comfyui_mcp_skills.domain.media import MAX_OUTPUT_BYTES, validate_media_locator

IMAGE_TAG_DIR_ENV = "COMFYUI_MCP_IMAGE_TAG_DIR"
_UPLOAD_PREFIX = "upload"
_SUPPORTED_STORAGE_TYPES = frozenset({"output"})


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
) -> dict[str, Any]:
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
        return {**document, "size_bytes": len(payload), "image_tag": None}

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
    return {
        **document,
        "size_bytes": int(transferred["size_bytes"]),
        "saved_path": relative,
        "image_tag": f"@@image:{relative}@@",
    }
