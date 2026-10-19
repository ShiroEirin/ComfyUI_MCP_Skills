"""Tests for model file metadata (`comfyui.model.list` include_metadata)."""

from __future__ import annotations

import json
import struct
from pathlib import Path

from comfyui_mcp_skills.application.discovery import (
    DiscoveryService,
    _safetensors_metadata,
)


class _Servers:
    def __init__(self, connection: dict) -> None:
        self._connection = connection

    def connection(self, _server_id: str) -> dict:
        return self._connection


class _Gateway:
    def get_models(self, _kind: str) -> list[str]:
        return ["alpha.safetensors", "sub/beta.safetensors", "missing.safetensors"]

    def get_model_folders(self) -> list[str]:
        return ["loras"]


def _write_safetensors(path: Path, metadata: dict[str, str]) -> None:
    header = json.dumps({"__metadata__": metadata}).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<Q", len(header)) + header + b"\0" * 4)


def test_safetensors_metadata_reads_the_header_block(tmp_path: Path) -> None:
    path = tmp_path / "lora.safetensors"
    _write_safetensors(path, {"ss_tag_frequency": '{"1_girl": 12}'})

    assert _safetensors_metadata(path) == {"ss_tag_frequency": '{"1_girl": 12}'}


def test_safetensors_metadata_ignores_non_safetensors_files(tmp_path: Path) -> None:
    path = tmp_path / "model.ckpt"
    path.write_bytes(b"not-safetensors")

    assert _safetensors_metadata(path) == {}


def test_safetensors_metadata_tolerates_truncated_files(tmp_path: Path) -> None:
    path = tmp_path / "broken.safetensors"
    path.write_bytes(b"\x01\x02")

    assert _safetensors_metadata(path) == {}


def test_models_without_local_root_reports_unavailable() -> None:
    service = DiscoveryService(_Servers({}), lambda _config: _Gateway())

    page = service.models("local", kind="loras", include_metadata=True)

    assert page["items"] == [
        "alpha.safetensors",
        "missing.safetensors",
        "sub/beta.safetensors",
    ]
    assert page["metadata"] == {"available": False, "reason": "no_local_root"}


def test_models_metadata_matches_files_and_reports_missing(tmp_path: Path) -> None:
    _write_safetensors(tmp_path / "models" / "loras" / "alpha.safetensors", {"a": "1"})
    _write_safetensors(
        tmp_path / "models" / "loras" / "sub" / "beta.safetensors", {"b": "2"}
    )
    service = DiscoveryService(
        _Servers({"local_root": str(tmp_path)}), lambda _config: _Gateway()
    )

    page = service.models("local", kind="loras", include_metadata=True)

    metadata = page["metadata"]
    assert metadata["available"] is True
    assert [item["name"] for item in metadata["files"]] == [
        "alpha.safetensors",
        "sub/beta.safetensors",
    ]
    assert metadata["files"][0]["metadata"] == {"a": "1"}
    assert metadata["files"][0]["size_bytes"] > 0
    assert metadata["missing"] == ["missing.safetensors"]


def test_models_without_metadata_flag_keeps_the_plain_page() -> None:
    service = DiscoveryService(_Servers({}), lambda _config: _Gateway())

    page = service.models("local", kind="loras")

    assert "metadata" not in page
