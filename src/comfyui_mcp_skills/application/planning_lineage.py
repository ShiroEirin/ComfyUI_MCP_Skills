"""Owner-bound immutable lineage facts for one execution Plan."""

from __future__ import annotations

import sqlite3
from dataclasses import astuple, dataclass
from typing import Any, Literal

from comfyui_mcp_skills.domain.control_plane import (
    parse_legacy_resource_uri,
    validate_control_plane_id,
)

_MEDIA_PARAMETER_TYPES = frozenset({"image", "mask", "audio", "video"})


@dataclass(frozen=True, slots=True)
class ExecutionPlanInput:
    """One graph-proven Asset or Artifact input before its Plan ID is known."""

    owner_id: str
    revision_id: str
    deployment_id: str
    parameter_name: str
    consumer_node_id: str
    consumer_input_name: str
    consumer_class: str
    source_kind: Literal["asset", "artifact"]
    asset_id: str | None
    artifact_id: str | None
    source_job_id: str | None
    reuse_strategy: Literal["direct", "copy", "upload"]
    source_digest: str

    def digest_payload(self) -> dict[str, object]:
        return {
            "parameter_name": self.parameter_name,
            "consumer_node_id": self.consumer_node_id,
            "consumer_input_name": self.consumer_input_name,
            "consumer_class": self.consumer_class,
            "source_kind": self.source_kind,
            "asset_id": self.asset_id,
            "artifact_id": self.artifact_id,
            "source_job_id": self.source_job_id,
            "reuse_strategy": self.reuse_strategy,
            "source_digest": self.source_digest,
        }


def resolve_execution_plan_inputs(
    connection: sqlite3.Connection,
    *,
    arguments: dict[str, Any],
    graph: dict[str, Any],
    parameters: dict[str, dict[str, Any]],
    owner_id: str,
    server_id: str,
    revision_id: str,
    deployment_id: str,
) -> tuple[ExecutionPlanInput, ...]:
    """Resolve caller references against the published graph and owned SQLite facts."""
    result: list[ExecutionPlanInput] = []
    for parameter_name in sorted(arguments):
        metadata = parameters.get(parameter_name)
        if not isinstance(metadata, dict):
            continue
        parameter_type = str(metadata.get("type", "")).lower()
        if parameter_type not in _MEDIA_PARAMETER_TYPES:
            continue
        source = _source_reference(
            connection,
            arguments[parameter_name],
            owner_id=owner_id,
            server_id=server_id,
        )
        if source is None:
            continue
        node_id, input_name, consumer_class = _parameter_target(graph, metadata, parameter_name)
        result.append(
            ExecutionPlanInput(
                owner_id,
                revision_id,
                deployment_id,
                parameter_name,
                node_id,
                input_name,
                consumer_class,
                *source,
            )
        )
    return tuple(result)


def persist_execution_plan_inputs(
    connection: sqlite3.Connection,
    *,
    plan_id: str,
    inputs: tuple[ExecutionPlanInput, ...],
    created_at: str,
) -> None:
    """Insert the immutable normalized inputs and reject any identity collision."""
    for item in inputs:
        connection.execute(
            """
            INSERT OR IGNORE INTO execution_plan_inputs(
                plan_id, owner_id, revision_id, deployment_id, parameter_name,
                consumer_node_id, consumer_input_name, consumer_class, source_kind,
                asset_id, artifact_id, source_job_id, reuse_strategy, source_digest,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (plan_id, *astuple(item), created_at),
        )
    rows = connection.execute(
        """
        SELECT owner_id, revision_id, deployment_id, parameter_name,
               consumer_node_id, consumer_input_name, consumer_class, source_kind,
               asset_id, artifact_id, source_job_id, reuse_strategy, source_digest
        FROM execution_plan_inputs WHERE plan_id = ? ORDER BY parameter_name
        """,
        (plan_id,),
    ).fetchall()
    expected = [astuple(item) for item in inputs]
    if [tuple(row) for row in rows] != expected:
        raise RuntimeError("execution Plan input identity conflicts with existing facts")


def _parameter_target(
    graph: dict[str, Any], metadata: dict[str, Any], parameter_name: str
) -> tuple[str, str, str]:
    node_id = str(metadata.get("node_id", ""))
    input_name = str(metadata.get("field", ""))
    node = graph.get(node_id)
    inputs = node.get("inputs") if isinstance(node, dict) else None
    consumer_class = str(node.get("class_type", "")) if isinstance(node, dict) else ""
    if (
        not node_id
        or not input_name
        or not consumer_class
        or not isinstance(inputs, dict)
        or input_name not in inputs
    ):
        raise RuntimeError(f'published parameter "{parameter_name}" has an invalid target')
    return node_id, input_name, consumer_class


def _source_reference(
    connection: sqlite3.Connection,
    value: object,
    *,
    owner_id: str,
    server_id: str,
) -> (
    tuple[
        Literal["asset", "artifact"],
        str | None,
        str | None,
        str | None,
        Literal["direct", "copy", "upload"],
        str,
    ]
    | None
):
    if not isinstance(value, str):
        return None
    if value.startswith("asset_"):
        asset_id = validate_control_plane_id("asset", value)
        row = connection.execute(
            """SELECT asset_id, server_id, sha256 FROM assets
               WHERE asset_id = ? AND owner_id = ? AND deleted_at IS NULL""",
            (asset_id, owner_id),
        ).fetchone()
        if row is None or str(row[1]) != server_id:
            raise LookupError("owned Asset input was not found on the target server")
        reuse_strategy = _asset_reuse_strategy(connection, asset_id, owner_id)
        return "asset", str(row[0]), None, None, reuse_strategy, str(row[2])

    artifact_id = _artifact_id(connection, value)
    if artifact_id is None:
        return None
    backfill = connection.execute(
        "SELECT status FROM phase_l_backfill_state WHERE backfill_name='artifact_outputs'"
    ).fetchone()
    if backfill is None or str(backfill[0]) != "complete":
        raise LookupError("Artifact lineage backfill is incomplete")
    row = connection.execute(
        """SELECT artifacts.artifact_id, artifacts.job_id, artifacts.server_id,
                  artifacts.digest
           FROM artifacts JOIN jobs ON jobs.job_id = artifacts.job_id
           WHERE artifacts.artifact_id = ? AND jobs.owner_id = ?
             AND EXISTS (
                 SELECT 1 FROM media_locations
                 WHERE media_locations.owner_id = jobs.owner_id
                   AND media_locations.artifact_id = artifacts.artifact_id
                   AND media_locations.source_job_id = artifacts.job_id
                   AND media_locations.server_id = ?
                   AND media_locations.state = 'available'
             )""",
        (artifact_id, owner_id, server_id),
    ).fetchone()
    if row is None or str(row[2]) != server_id:
        raise LookupError("owned Artifact input was not found on the target server")
    return "artifact", None, str(row[0]), str(row[1]), "direct", str(row[3])


def _asset_reuse_strategy(
    connection: sqlite3.Connection, asset_id: str, owner_id: str
) -> Literal["direct", "copy", "upload"]:
    rows = connection.execute(
        """SELECT DISTINCT strategy FROM artifact_transfers
           WHERE result_asset_id = ? AND owner_id = ? AND state = 'completed'""",
        (asset_id, owner_id),
    ).fetchall()
    if not rows:
        return "direct"
    strategy = str(rows[0][0]) if len(rows) == 1 else ""
    if strategy not in {"copy", "upload"}:
        raise RuntimeError("Asset input has conflicting transfer strategies")
    return "copy" if strategy == "copy" else "upload"


def _artifact_id(connection: sqlite3.Connection, value: str) -> str | None:
    prefix = "comfyui://artifacts/"
    if value.startswith(prefix):
        artifact_id = value.removeprefix(prefix)
        return validate_control_plane_id("artifact", artifact_id)
    legacy = parse_legacy_resource_uri(value)
    if legacy is None or legacy.kind != "output":
        return None
    row = connection.execute(
        """SELECT artifact_id FROM legacy_resource_aliases
           WHERE alias_uri = ? AND object_kind = 'output' AND artifact_id IS NOT NULL""",
        (value,),
    ).fetchone()
    if row is None:
        raise LookupError("owned Artifact input alias was not found")
    return validate_control_plane_id("artifact", str(row[0]))
