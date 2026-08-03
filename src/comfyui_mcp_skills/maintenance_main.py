"""Explicit maintenance command for pruning MCP metadata."""

from __future__ import annotations

import json
import os
from pathlib import Path

from comfyui_mcp_skills.infrastructure.persistence.retention import FileRetentionService


def run_maintenance(
    base_dir: Path,
    *,
    run_days: int,
    asset_days: int,
    max_history_records: int,
) -> dict[str, int]:
    """Prune authoritative legacy stores and due SQLite lifecycle records."""
    return FileRetentionService(base_dir).prune_switch_aware(
        run_days=run_days,
        asset_days=asset_days,
        max_history_records=max_history_records,
    )


def main() -> None:
    base_dir = Path(os.environ.get("COMFYUI_MCP_DIR", os.getcwd())).resolve()
    result = run_maintenance(
        base_dir,
        run_days=int(os.environ.get("COMFYUI_MCP_RUN_RETENTION_DAYS", "30")),
        asset_days=int(os.environ.get("COMFYUI_MCP_ASSET_RETENTION_DAYS", "30")),
        max_history_records=int(os.environ.get("COMFYUI_MCP_MAX_HISTORY_RECORDS", "10000")),
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
