"""File-backed asset metadata repository."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict
from pathlib import Path

from comfyui_mcp_skills.domain.models import Asset


class FileAssetRepository:
    def __init__(self, base_dir: Path) -> None:
        self._root = (base_dir / "data" / "assets").resolve()

    def save(self, asset: Asset) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(asset.asset_id)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as file:
                json.dump(asdict(asset), file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, asset_id: str) -> Asset | None:
        path = self._path(asset_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        fields = {
            key: value
            for key, value in data.items()
            if key in Asset.__dataclass_fields__
        }
        return Asset(**fields)

    def _path(self, asset_id: str) -> Path:
        if not asset_id.startswith("asset_") or not asset_id[6:].isalnum():
            raise ValueError("Invalid asset ID")
        return self._root / f"{asset_id}.json"
