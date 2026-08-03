"""Concurrent configuration persistence contracts."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from comfyui_skills_cli.config import save_config


def test_save_config_serializes_concurrent_atomic_writes(tmp_path: Path) -> None:
    workers = 8
    barrier = Barrier(workers)

    def write_config(index: int) -> None:
        barrier.wait()
        save_config(tmp_path, {"writer": index, "payload": "x" * 4096})

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(write_config, index) for index in range(workers)]
        for future in futures:
            future.result()

    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved in [{"writer": index, "payload": "x" * 4096} for index in range(workers)]
    assert not (tmp_path / "config.tmp").exists()
