"""SQLite-backed Asset repository used after the G1 asset cutover."""

from __future__ import annotations

import sqlite3

from comfyui_mcp_skills.domain.models import Asset
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore


class SQLiteAssetRepository:
    """Persist immutable input-asset metadata without touching legacy files."""

    def __init__(self, store: SQLiteControlPlaneStore) -> None:
        self._store = store

    def save(self, asset: Asset) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO assets(
                    asset_id, owner_id, server_id, name, subfolder, media_type,
                    mime_type, size_bytes, sha256, source_type, comfyui_ref,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'runtime_upload', ?, ?, NULL)
                """,
                (
                    asset.asset_id,
                    asset.owner_id,
                    asset.server_id,
                    asset.name,
                    asset.subfolder,
                    asset.media_type,
                    asset.mime_type,
                    asset.size_bytes,
                    asset.sha256,
                    asset.comfyui_ref,
                    asset.created_at,
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, asset_id: str) -> Asset | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT asset_id, server_id, comfyui_ref, name, subfolder,
                       media_type, mime_type, size_bytes, sha256, owner_id, created_at
                FROM assets WHERE asset_id = ?
                """,
                (asset_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return Asset(*tuple(row))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._store.path, isolation_level=None, timeout=5.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA trusted_schema = OFF")
        return connection
