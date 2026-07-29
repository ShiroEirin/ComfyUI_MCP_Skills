"""Explicit maintenance command for pruning MCP metadata."""

from __future__ import annotations

import json
import os
from pathlib import Path

from comfyui_mcp_skills.infrastructure.persistence.retention import FileRetentionService


def main() -> None:
    base_dir = Path(os.environ.get("COMFYUI_MCP_DIR", os.getcwd())).resolve()
    result = FileRetentionService(base_dir).prune(
        run_days=int(os.environ.get("COMFYUI_MCP_RUN_RETENTION_DAYS", "30")),
        asset_days=int(os.environ.get("COMFYUI_MCP_ASSET_RETENTION_DAYS", "30")),
        max_history_records=int(os.environ.get("COMFYUI_MCP_MAX_HISTORY_RECORDS", "10000")),
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
