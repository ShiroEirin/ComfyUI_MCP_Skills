"""Transactional Phase L reconstruction of historical execution Plan inputs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import astuple, dataclass
from datetime import datetime, timezone
from typing import Literal

from comfyui_mcp_skills.domain.control_plane import (
    parse_legacy_resource_uri,
    validate_control_plane_id,
)
from comfyui_mcp_skills.domain.workflow_schema import normalize_parameters

_MEDIA_PARAMETER_TYPES = frozenset({"image", "mask", "audio", "video"})
_FAILURE_CODE = "execution_plan_inputs_unreconstructable"


@dataclass(frozen=True, slots=True)
class _BackfilledPlanInput:
    plan_id: str
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
    created_at: str


def backfill_execution_plan_inputs(connection: sqlite3.Connection) -> None:
    """Reconstruct every historical Plan or durably record a retryable failure."""
    state = connection.execute(
        """SELECT status FROM phase_l_backfill_state
           WHERE backfill_name='execution_plan_inputs'"""
    ).fetchone()
    if state is None:
        raise RuntimeError("execution Plan input backfill state is missing")
    if str(state[0]) == "complete":
        return

    plan_count = int(connection.execute("SELECT count(*) FROM execution_plans").fetchone()[0])
    if plan_count == 0:
        connection.execute(
            """UPDATE phase_l_backfill_state
               SET status='complete',incomplete_count=0,completed_at=?,failure_code=NULL
               WHERE backfill_name='execution_plan_inputs'""",
            (_utc_now(),),
        )
        return

    connection.execute(
        """UPDATE phase_l_backfill_state
           SET status='running',incomplete_count=?,completed_at=NULL,failure_code=NULL
           WHERE backfill_name='execution_plan_inputs'""",
        (plan_count,),
    )
    connection.execute("SAVEPOINT execution_plan_inputs_backfill")
    try:
        expected = _reconstruct_all(connection)
        _persist_and_verify(connection, expected)
    except (
        LookupError,
        RuntimeError,
        TypeError,
        ValueError,
        sqlite3.DatabaseError,
    ):
        connection.execute("ROLLBACK TO execution_plan_inputs_backfill")
        connection.execute("RELEASE execution_plan_inputs_backfill")
        connection.execute(
            """UPDATE phase_l_backfill_state
               SET status='failed',incomplete_count=?,completed_at=NULL,failure_code=?
               WHERE backfill_name='execution_plan_inputs'""",
            (plan_count, _FAILURE_CODE),
        )
        return
    connection.execute("RELEASE execution_plan_inputs_backfill")
    connection.execute(
        """UPDATE phase_l_backfill_state
           SET status='complete',incomplete_count=0,completed_at=?,failure_code=NULL
           WHERE backfill_name='execution_plan_inputs'""",
        (_utc_now(),),
    )


def _reconstruct_all(connection: sqlite3.Connection) -> tuple[_BackfilledPlanInput, ...]:
    rows = connection.execute(
        """SELECT plans.plan_id,plans.revision_id,plans.deployment_id,plans.server_id,
                  plans.resolved_inputs_json,plans.input_digest,plans.created_at,
                  owners.owner_id,revisions.graph_json,revisions.parameter_schema_json,
                  observed_owners.owner_count,observed_owners.owner_id
           FROM execution_plans AS plans
           LEFT JOIN execution_plan_owners AS owners ON owners.plan_id=plans.plan_id
           LEFT JOIN workflow_revisions AS revisions
             ON revisions.workflow_id=plans.workflow_id
            AND revisions.revision_id=plans.revision_id
           LEFT JOIN (
               SELECT plan_id,count(DISTINCT owner_id) AS owner_count,
                      min(owner_id) AS owner_id
               FROM jobs WHERE plan_id IS NOT NULL GROUP BY plan_id
           ) AS observed_owners ON observed_owners.plan_id=plans.plan_id
           ORDER BY plans.plan_id"""
    ).fetchall()
    result: list[_BackfilledPlanInput] = []
    for row in rows:
        if (
            row[7] is None
            or row[8] is None
            or row[9] is None
            or row[10] is None
            or int(row[10]) != 1
            or str(row[11]) != str(row[7])
        ):
            raise LookupError("historical execution Plan lacks unambiguous owner or revision facts")
        snapshot_json = str(row[4])
        if hashlib.sha256(snapshot_json.encode()).hexdigest() != str(row[5]):
            raise ValueError("historical execution Plan input snapshot digest does not match")
        graph = json.loads(str(row[8]))
        if not isinstance(graph, dict):
            raise ValueError("historical workflow graph must be an object")
        parameters = normalize_parameters(json.loads(str(row[9])))
        arguments = _snapshot_arguments(snapshot_json)
        for parameter_name in sorted(arguments):
            metadata = parameters.get(parameter_name)
            if not isinstance(metadata, dict):
                continue
            parameter_type = str(metadata.get("type", "")).lower()
            if parameter_type not in _MEDIA_PARAMETER_TYPES:
                continue
            node_id, input_name, consumer_class = _parameter_target(graph, metadata, parameter_name)
            source = _source_fact(
                connection,
                arguments[parameter_name],
                owner_id=str(row[7]),
                server_id=str(row[3]),
                parameter_type=parameter_type,
            )
            result.append(
                _BackfilledPlanInput(
                    str(row[0]),
                    str(row[7]),
                    str(row[1]),
                    str(row[2]),
                    parameter_name,
                    node_id,
                    input_name,
                    consumer_class,
                    *source,
                    str(row[6]),
                )
            )
    return tuple(result)


def _snapshot_arguments(snapshot_json: str) -> dict[str, object]:
    snapshot = json.loads(snapshot_json)
    if not isinstance(snapshot, dict):
        raise ValueError("historical execution Plan snapshot must be an object")
    if "arguments" in snapshot:
        arguments = snapshot["arguments"]
        if not isinstance(arguments, dict):
            raise ValueError("historical execution Plan arguments must be an object")
        return arguments
    if "resolved_inputs" in snapshot:
        raise ValueError("historical execution Plan omitted its caller arguments")
    return snapshot


def _parameter_target(
    graph: dict[str, object], metadata: dict[str, object], parameter_name: str
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


def _source_fact(
    connection: sqlite3.Connection,
    value: object,
    *,
    owner_id: str,
    server_id: str,
    parameter_type: str,
) -> tuple[
    Literal["asset", "artifact"],
    str | None,
    str | None,
    str | None,
    Literal["direct", "copy", "upload"],
    str,
]:
    if not isinstance(value, str):
        raise ValueError("historical media input is not a resource reference")
    if value.startswith("asset_"):
        asset_id = validate_control_plane_id("asset", value)
        row = connection.execute(
            """SELECT asset_id,server_id,media_type,sha256 FROM assets
               WHERE asset_id=? AND owner_id=?""",
            (asset_id, owner_id),
        ).fetchone()
        if row is None or str(row[1]) != server_id:
            raise LookupError("historical owned Asset input was not found")
        _verify_media_type(str(row[2]), parameter_type)
        return (
            "asset",
            str(row[0]),
            None,
            None,
            _asset_reuse_strategy(connection, asset_id, owner_id),
            str(row[3]),
        )

    artifact_id = _artifact_id(connection, value, server_id=server_id)
    if artifact_id is None:
        raise ValueError("historical media input is not a reconstructable resource reference")
    row = connection.execute(
        """SELECT artifacts.artifact_id,artifacts.job_id,artifacts.server_id,
                  artifacts.media_type,artifacts.digest
           FROM artifacts JOIN jobs ON jobs.job_id=artifacts.job_id
           WHERE artifacts.artifact_id=? AND jobs.owner_id=?""",
        (artifact_id, owner_id),
    ).fetchone()
    if row is None or str(row[2]) != server_id:
        raise LookupError("historical owned Artifact input was not found")
    _verify_media_type(str(row[3]), parameter_type)
    return "artifact", None, str(row[0]), str(row[1]), "direct", str(row[4])


def _artifact_id(connection: sqlite3.Connection, value: str, *, server_id: str) -> str | None:
    prefix = "comfyui://artifacts/"
    if value.startswith(prefix):
        return validate_control_plane_id("artifact", value.removeprefix(prefix))
    legacy = parse_legacy_resource_uri(value)
    if legacy is None or legacy.kind != "output":
        return None
    if legacy.server_id != server_id:
        raise LookupError("historical Artifact input belongs to another server")
    row = connection.execute(
        """SELECT artifact_id FROM legacy_resource_aliases
           WHERE alias_uri=? AND object_kind='output' AND artifact_id IS NOT NULL""",
        (value,),
    ).fetchone()
    if row is None:
        raise LookupError("historical Artifact input alias was not found")
    return validate_control_plane_id("artifact", str(row[0]))


def _asset_reuse_strategy(
    connection: sqlite3.Connection, asset_id: str, owner_id: str
) -> Literal["direct", "copy", "upload"]:
    rows = connection.execute(
        """SELECT DISTINCT strategy FROM artifact_transfers
           WHERE result_asset_id=? AND owner_id=? AND state='completed'""",
        (asset_id, owner_id),
    ).fetchall()
    if not rows:
        return "direct"
    strategy = str(rows[0][0]) if len(rows) == 1 else ""
    if strategy not in {"copy", "upload"}:
        raise RuntimeError("historical Asset input has conflicting transfer strategies")
    return "copy" if strategy == "copy" else "upload"


def _verify_media_type(actual: str, parameter_type: str) -> None:
    expected = "image" if parameter_type == "mask" else parameter_type
    if actual != expected:
        raise ValueError("historical media input type does not match its published schema")


def _persist_and_verify(
    connection: sqlite3.Connection, expected: tuple[_BackfilledPlanInput, ...]
) -> None:
    for item in expected:
        connection.execute(
            """INSERT OR IGNORE INTO execution_plan_inputs(
                   plan_id,owner_id,revision_id,deployment_id,parameter_name,
                   consumer_node_id,consumer_input_name,consumer_class,source_kind,
                   asset_id,artifact_id,source_job_id,reuse_strategy,source_digest,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            astuple(item),
        )
    actual = connection.execute(
        """SELECT plan_id,owner_id,revision_id,deployment_id,parameter_name,
                  consumer_node_id,consumer_input_name,consumer_class,source_kind,
                  asset_id,artifact_id,source_job_id,reuse_strategy,source_digest,created_at
           FROM execution_plan_inputs ORDER BY plan_id,parameter_name"""
    ).fetchall()
    expected_rows = sorted((astuple(item) for item in expected), key=lambda row: (row[0], row[4]))
    if [tuple(row) for row in actual] != expected_rows:
        raise RuntimeError("historical execution Plan input identity conflicts with stored facts")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
