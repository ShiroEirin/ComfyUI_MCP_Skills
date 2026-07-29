"""Workflow catalog change detection for MCP list notifications."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import anyio
from mcp.server.subscriptions import SubscriptionBus
from mcp.shared.subscriptions import ResourcesListChanged, ToolsListChanged


class WorkflowChangeMonitor:
    def __init__(
        self,
        base_dir: Path,
        bus: SubscriptionBus,
        *,
        interval_seconds: float = 2.0,
    ) -> None:
        self._root = (base_dir.resolve() / "data").resolve()
        self._config_path = self._root.parent / "config.json"
        self._bus = bus
        self._interval = interval_seconds
        self._fingerprint = self._scan()

    async def check(self) -> bool:
        current = await anyio.to_thread.run_sync(self._scan)
        if current == self._fingerprint:
            return False
        self._fingerprint = current
        await self._bus.publish(ToolsListChanged())
        await self._bus.publish(ResourcesListChanged())
        return True

    async def run(self) -> None:
        while True:
            await anyio.sleep(self._interval)
            await self.check()

    def _scan(self) -> tuple[tuple[str, str], ...]:
        entries: list[tuple[str, str]] = []
        config_digest = self._digest(self._config_path)
        if config_digest is not None:
            entries.append(("config.json", config_digest))

        if not self._root.exists():
            return tuple(entries)
        for path in self._root.rglob("*"):
            if path.name not in {"schema.json", "workflow.json"}:
                continue
            try:
                relative = path.relative_to(self._root).as_posix()
            except (ValueError, OSError):
                continue
            digest = self._digest(path)
            if digest is not None:
                entries.append((relative, digest))
        return tuple(sorted(entries))

    @staticmethod
    def _digest(path: Path) -> str | None:
        try:
            with path.open("rb") as source:
                digest = sha256()
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
                return digest.hexdigest()
        except (FileNotFoundError, OSError):
            return None
