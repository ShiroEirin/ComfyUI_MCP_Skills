"""Isolated Revision -> Plan -> Job contract harness acceptance tests."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from comfyui_mcp_skills.infrastructure.persistence.contract_harness import (
    ContractHarnessFailure,
    RevisionPlanJobContractHarness,
)
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore


def test_revision_plan_job_contract_harness_proves_minimal_slice(tmp_path: Path) -> None:
    database = tmp_path / "contract.sqlite3"
    graph = {"1": {"inputs": {"text": "cat"}}}
    inputs = {"prompt": "cat"}
    harness = RevisionPlanJobContractHarness(database)

    evidence = harness.run(graph=graph, resolved_inputs=inputs)
    graph["1"]["inputs"]["text"] = "changed"
    inputs["prompt"] = "changed"

    assert evidence.revision_immutable is True
    assert evidence.plan_immutable is True
    assert evidence.job_bound_to_plan is True
    assert evidence.legacy_alias_resolves is True
    assert evidence.legacy_alias_immutable is True
    assert evidence.production_switches_written is False
    with closing(sqlite3.connect(harness.database)) as connection:
        stored_graph, content_digest = connection.execute(
            "SELECT graph_json, content_digest FROM workflow_revisions WHERE revision_id = ?",
            (evidence.revision_id,),
        ).fetchone()
        stored_inputs, input_digest, plan_digest, deployment_id = connection.execute(
            """
            SELECT resolved_inputs_json, input_digest, plan_digest, deployment_id
            FROM execution_plans WHERE plan_id = ?
            """,
            (evidence.plan_id,),
        ).fetchone()
        assert stored_graph == '{"1":{"inputs":{"text":"cat"}}}'
        assert stored_inputs == '{"prompt":"cat"}'

        def canonical(value: object) -> bytes:
            return json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")

        assert (
            content_digest
            == hashlib.sha256(
                canonical(
                    {
                        "graph": json.loads(stored_graph),
                        "parameter_schema": {},
                        "dependency_contract": {},
                    }
                )
            ).hexdigest()
        )
        assert input_digest == hashlib.sha256(stored_inputs.encode("utf-8")).hexdigest()
        assert (
            plan_digest
            == hashlib.sha256(
                canonical([evidence.revision_id, deployment_id, "local", json.loads(stored_inputs)])
            ).hexdigest()
        )
        assert connection.execute("SELECT count(*) FROM store_migrations").fetchone() == (0,)


def test_contract_harness_failure_rolls_back_entire_slice(tmp_path: Path) -> None:
    database = tmp_path / "contract.sqlite3"
    harness = RevisionPlanJobContractHarness(database)

    with pytest.raises(ContractHarnessFailure, match="injected"):
        harness.run(
            graph={"1": {"inputs": {"text": "cat"}}},
            resolved_inputs={"prompt": "cat"},
            fail_before_commit=True,
        )

    with closing(sqlite3.connect(harness.database)) as connection:
        for table in (
            "workflows",
            "workflow_revisions",
            "workflow_deployments",
            "execution_plans",
            "jobs",
            "legacy_resource_aliases",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_contract_harness_rejects_existing_database(tmp_path: Path) -> None:
    database = tmp_path / "contract.sqlite3"
    database.touch()

    with pytest.raises(FileExistsError, match="new isolated database"):
        RevisionPlanJobContractHarness(database)


def test_contract_harness_rejects_broken_database_symlink(tmp_path: Path) -> None:
    database = tmp_path / "contract.sqlite3"
    target = tmp_path / "outside.sqlite3"
    try:
        database.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")

    with pytest.raises(FileExistsError, match="new isolated database"):
        RevisionPlanJobContractHarness(database)
    assert target.exists() is False


def test_contract_harness_rejects_replaced_database(tmp_path: Path) -> None:
    database = tmp_path / "contract.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    harness = RevisionPlanJobContractHarness(database)
    replacement.write_bytes(harness.database.read_bytes())
    replacement.replace(harness.database)

    with pytest.raises(ContractHarnessFailure, match="identity changed"):
        harness.run(graph={}, resolved_inputs={})


def test_contract_harness_initializes_staging_before_publishing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "contract.sqlite3"
    victim = tmp_path / "victim.sqlite3"
    SQLiteControlPlaneStore(victim).initialize()
    original_initialize = SQLiteControlPlaneStore.initialize

    def race_initialize(store: SQLiteControlPlaneStore) -> None:
        database.unlink()
        os.link(victim, database)
        original_initialize(store)

    monkeypatch.setattr(SQLiteControlPlaneStore, "initialize", race_initialize)
    with pytest.raises(ContractHarnessFailure, match="reserved contract database identity changed"):
        RevisionPlanJobContractHarness(database)

    with closing(sqlite3.connect(victim)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM test_migration_database_role"
        ).fetchone() == (0,)
