"""SQLite Phase M rating and promotion round-trip contracts."""
# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from comfyui_mcp_skills.application.experiments import ExperimentService
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.sqlite_experiments import (
    SQLiteExperimentRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_workflows import _revision_digest


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_rating_promotion_and_preset_are_durable_and_executable(tmp_path: Path) -> None:
    store = SQLiteControlPlaneStore((tmp_path / "control-plane.sqlite3").resolve())
    store.initialize()
    graph = {"1": {"class_type": "Inputs", "inputs": {"width": 64, "height": 64, "seed": 1}}}
    schema = {
        "parameters": {
            name: {
                "type": "integer",
                "required": True,
                "node_id": "1",
                "field": name,
                **({"semantic_role": name} if name in {"width", "height"} else {}),
                **({"role": name} if name in {"width", "height"} else {}),
            }
            for name in ("width", "height", "seed")
        },
        "_output_contract": {
            "coverage": "complete",
            "outputs": [{"node_id": "1", "output_key": "images"}],
        },
        "_revision": {"source": "promotion-regression"},
    }
    dependencies = {
        "nodes": ["Inputs"],
        "models": [],
        "output_cardinality": 1,
        "trusted_seconds_per_run": 2.0,
    }
    digest = _revision_digest(graph, schema, dependencies)
    source_revision = "revision_" + "1" * 64
    deployment = "deployment_" + "2" * 64
    created = "2026-08-03T12:00:00+00:00"
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO workflows(workflow_id,created_at) VALUES('portrait',?)", (created,)
        )
        connection.execute(
            "INSERT INTO workflow_revisions(revision_id,workflow_id,graph_json,parameter_schema_json,dependency_contract_json,content_digest,created_at) VALUES(?,'portrait',?,?,?,?,?)",
            (
                source_revision,
                _canonical(graph),
                _canonical(schema),
                _canonical(dependencies),
                digest,
                created,
            ),
        )
        connection.execute(
            "INSERT INTO workflow_deployments(deployment_id,workflow_id,revision_id,server_id,enabled,validation_status,published,created_at) VALUES(?,'portrait',?,'local',1,'valid',1,?)",
            (deployment, source_revision, created),
        )
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    repository = SQLiteExperimentRepository(store)
    service = ExperimentService(
        repository, rubrics={"quality-v1": {"quality": (0.0, 5.0)}}, clock=lambda: now
    )
    plan = service.plan(
        "owner-a",
        "portrait",
        "local",
        {"mode": "explicit", "variants": [{"seed": 7}]},
        {"width": 64, "height": 64},
        {
            "max_variants": 1,
            "max_concurrency": 1,
            "max_pixels": 4096,
            "max_outputs": 1,
            "max_seconds": 2,
        },
        "continue",
        1,
        0,
    )
    experiment = service.commit(plan["plan_id"], plan["plan_digest"], "owner-a")
    variant_id = service.list_variants(experiment["experiment_id"], "owner-a", 1, None)["items"][0][
        "variant_id"
    ]
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE experiment_variants SET status='completed',completed_at=?,updated_at=?,measured_pixels=4096,measured_outputs=1,measured_seconds=2.0 WHERE variant_id=?",
            (created, created, variant_id),
        )
        connection.execute(
            """UPDATE experiments SET status='completed',pending_count=0,
                      completed_count=1,updated_at=?,completed_at=?
               WHERE experiment_id=?""",
            (created, created, experiment["experiment_id"]),
        )
    retention = repository.apply_retention(now=now + timedelta(days=1))
    assert retention["terminal_plans_pruned"] == 0
    assert repository.get_variant(experiment["experiment_id"], variant_id, "owner-a") is not None
    rating = service.rate(
        experiment["experiment_id"], variant_id, "quality-v1", {"quality": 4.5}, "owner-a"
    )
    preset = service.promote(experiment["experiment_id"], variant_id, "preset", "owner-a")
    revision = service.promote(experiment["experiment_id"], variant_id, "revision", "owner-a")
    assert (
        service.promote(experiment["experiment_id"], variant_id, "revision", "owner-a") == revision
    )
    refetched = repository.get_variant(experiment["experiment_id"], variant_id, "owner-a")
    assert refetched is not None
    assert refetched["ratings"][0]["rubric_definition"] == rating["rubric_definition"]
    assert {item["target"] for item in refetched["promotions"]} == {"preset", "revision"}
    expected_arguments = {"height": 64, "seed": 7, "width": 64}
    assert repository.get_preset(preset["preset_id"], "owner-a")["arguments"] == expected_arguments
    assert repository.get_preset(preset["preset_id"], "owner-b") is None
    assert (
        repository.consume_preset(preset["preset_id"], "owner-a", "portrait", "local")
        == expected_arguments
    )
    with sqlite3.connect(store.path) as connection:
        promoted = connection.execute(
            "SELECT graph_json,parameter_schema_json,dependency_contract_json,content_digest FROM workflow_revisions WHERE revision_id=?",
            (revision["revision_id"],),
        ).fetchone()
        assert promoted is not None
        promoted_graph, promoted_schema, promoted_dependencies = map(json.loads, promoted[:3])
        assert promoted_graph == graph
        assert promoted_dependencies == dependencies
        assert promoted_schema["parameters"]["seed"]["default"] == 7
        assert promoted_schema["_output_contract"] == schema["_output_contract"]
        assert promoted_schema["_revision"] == schema["_revision"]
        assert promoted[3] == _revision_digest(
            promoted_graph, promoted_schema, promoted_dependencies
        )
        assert connection.execute(
            "SELECT count(*) FROM workflow_deployments WHERE revision_id=?",
            (revision["revision_id"],),
        ).fetchone() == (0,)
        uris = {
            json.loads(row[0])["uri"]
            for row in connection.execute(
                "SELECT payload_json FROM outbox WHERE topic='resources.updated'"
            )
        }
        assert f"comfyui://experiments/{experiment['experiment_id']}" in uris
        assert f"comfyui://experiments/{experiment['experiment_id']}/variants/{variant_id}" in uris
