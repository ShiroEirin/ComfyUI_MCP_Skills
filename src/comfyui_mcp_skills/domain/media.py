"""Validation for untrusted ComfyUI server-relative media locators."""

from __future__ import annotations


def validate_media_locator(name: object, subfolder: object = "") -> tuple[str, str]:
    """Return a normalized safe filename and relative subfolder."""
    if not isinstance(name, str) or not name or len(name) > 255:
        raise ValueError("media filename is invalid")
    if name in {".", ".."} or any(character in name for character in "/\\:"):
        raise ValueError("media filename is invalid")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in name):
        raise ValueError("media filename is invalid")
    if not isinstance(subfolder, str) or len(subfolder) > 1024:
        raise ValueError("media subfolder is invalid")
    normalized = subfolder.replace("\\", "/")
    if normalized.startswith("/") or ":" in normalized:
        raise ValueError("media subfolder is invalid")
    parts = normalized.split("/") if normalized else []
    if any(
        not part
        or part in {".", ".."}
        or len(part) > 255
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in part)
        for part in parts
    ):
        raise ValueError("media subfolder is invalid")
    return name, "/".join(parts)
