"""Phase M Experiment SQLite persistence contracts."""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from comfyui_mcp_skills.application.experiments import ExperimentService
from comfyui_mcp_skills.infrastructure.persistence import control_plane as control_plane_module
from comfyui_mcp_skills.infrastructure.persistence import (
    sqlite_experiments as sqlite_experiments_module,
)
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.orchestration import (
    SQLiteOrchestrationRepository,
)
from comfyui_mcp_skills.infrastructure.persistence.sqlite_experiments import (
    SQLiteExperimentRepository,
)
from comfyui_mcp_skills.maintenance_main import run_maintenance


def _store(tmp_path: Path) -> SQLiteControlPlaneStore:
    store = SQLiteControlPlaneStore((tmp_path / "control-plane.sqlite3").resolve())
    store.initialize()
    return store


def test_phase_m_migration_creates_owner_bound_experiment_schema(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with sqlite3.connect(store.path) as connection:
        version = connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='index'"
            ).fetchall()
        }

    assert version == 8
    assert {
        "experiment_plans",
        "experiments",
        "experiment_variants",
        "experiment_variant_jobs",
        "experiment_rubric_versions",
        "experiment_rubric_dimensions",
        "experiment_ratings",
        "experiment_presets",
    } <= tables
    assert {
        "ix_experiment_variants_page",
        "ix_experiment_variants_claimable",
        "ix_experiments_owner_updated",
        "ix_experiment_ratings_variant",
    } <= indexes


def _workflow(store: SQLiteControlPlaneStore, workflow_id: str = "portrait") -> None:
    created_at = "2026-08-03T00:00:00+00:00"
    revision_id = "revision_" + "1" * 64
    deployment_id = "deployment_" + "2" * 64
    content_digest = "3" * 64
    graph = {
        "1": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 64, "height": 64, "batch_size": 1},
        },
        "2": {"class_type": "KSampler", "inputs": {"seed": 0}},
    }
    parameter_schema = {
        "type": "object",
        "parameters": {
            "width": {
                "type": "integer",
                "default": 64,
                "role": "width",
                "node_id": "1",
                "field": "width",
            },
            "height": {
                "type": "integer",
                "default": 64,
                "role": "height",
                "node_id": "1",
                "field": "height",
            },
            "seed": {
                "type": "integer",
                "default": 0,
                "node_id": "2",
                "field": "seed",
            },
        },
        "_output_contract": {"coverage": "complete", "outputs": ["image"]},
        "additionalProperties": True,
    }
    dependencies = {"output_cardinality": 1, "trusted_seconds_per_run": 1.0}
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO workflows(workflow_id,created_at) VALUES(?,?)",
            (workflow_id, created_at),
        )
        connection.execute(
            """INSERT INTO workflow_revisions(
                revision_id,workflow_id,graph_json,parameter_schema_json,
                dependency_contract_json,content_digest,created_at
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                revision_id,
                workflow_id,
                json.dumps(graph),
                json.dumps(parameter_schema),
                json.dumps(dependencies),
                content_digest,
                created_at,
            ),
        )
        connection.execute(
            """INSERT INTO workflow_deployments(
                deployment_id,workflow_id,revision_id,server_id,enabled,
                validation_status,published,created_at
            ) VALUES(?,?,?,'local',1,'valid',1,?)""",
            (deployment_id, workflow_id, revision_id, created_at),
        )


def _plan(
    variant_count: int, *, owner_id: str = "owner-a"
) -> tuple[dict[str, object], list[dict[str, object]]]:
    digest = "a" * 64
    experiment_id = "experiment_" + "b" * 64
    created_at = "2026-08-03T00:00:00+00:00"
    plan: dict[str, object] = {
        "plan_id": "experiment_plan_" + digest,
        "plan_digest": digest,
        "experiment_id": experiment_id,
        "owner_id": owner_id,
        "workflow_id": "portrait",
        "server_id": "local",
        "expansion": {"mode": "explicit"},
        "base_arguments": {"width": 64, "height": 64},
        "budgets": {
            "max_variants": variant_count,
            "max_concurrency": 4,
            "max_pixels": variant_count * 4096,
            "max_outputs": variant_count,
            "max_seconds": variant_count,
        },
        "budget_totals": {
            "variants": variant_count,
            "concurrency": min(4, variant_count),
            "pixels": variant_count * 4096,
            "outputs": variant_count,
            "seconds": float(variant_count),
        },
        "failure_policy": "continue",
        "concurrency": min(4, variant_count),
        "submission_window": 0,
        "variant_count": variant_count,
        "created_at": created_at,
        "status": "planned",
    }
    variants = [
        {
            "variant_id": "variant_" + f"{index:064x}",
            "experiment_id": experiment_id,
            "owner_id": owner_id,
            "ordinal": index,
            "arguments": {"seed": index, "width": 64, "height": 64},
            "parameter_digest": f"{index + 1:064x}",
            "status": "pending",
            "job_id": "",
            "created_at": created_at,
            "updated_at": created_at,
        }
        for index in range(variant_count)
    ]
    return plan, variants


def _compact_variants(
    plan: dict[str, object], variant_count: int, *, note: str = ""
) -> list[dict[str, object]]:
    base_json = json.dumps(
        plan["base_arguments"],
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    base_digest = hashlib.sha256(base_json.encode("utf-8")).hexdigest()
    variants: list[dict[str, object]] = []
    for ordinal in range(variant_count):
        overrides: dict[str, object] = {"seed": ordinal}
        if note:
            overrides["note"] = note
        parameter_digest = hashlib.sha256(
            json.dumps(
                ["resolved-variant-v2", base_digest, overrides],
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        variants.append(
            {
                "ordinal": ordinal,
                "overrides": overrides,
                "parameter_digest": parameter_digest,
            }
        )
    return variants


def _identified_plan(
    index: int, *, owner_id: str = "owner-a"
) -> tuple[dict[str, object], list[dict[str, object]]]:
    plan, variants = _plan(1, owner_id=owner_id)
    digest = f"{index:064x}"
    experiment_id = "experiment_" + digest
    plan.update(
        {
            "plan_id": "experiment_plan_" + digest,
            "plan_digest": digest,
            "experiment_id": experiment_id,
            "expires_at": "2099-01-01T00:00:00+00:00",
        }
    )
    variants[0].update(
        {
            "variant_id": "variant_" + digest,
            "experiment_id": experiment_id,
            "owner_id": owner_id,
        }
    )
    return plan, variants


def test_plan_commit_is_atomic_complete_and_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    plan, variants = _plan(3)

    assert repository.save_plan(plan, variants) == plan
    assert repository.save_plan(plan, variants) == plan
    first = repository.commit_plan(
        str(plan["plan_id"]), str(plan["plan_digest"]), str(plan["owner_id"])
    )
    second = repository.commit_plan(
        str(plan["plan_id"]), str(plan["plan_digest"]), str(plan["owner_id"])
    )

    assert first == second
    assert first["experiment_id"] == plan["experiment_id"]
    assert first["status"] == "queued"
    assert first["pending_count"] == first["variant_count"] == 3
    assert "variants" not in first
    with sqlite3.connect(store.path) as connection:
        counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "experiments",
                "experiment_variants",
                "operation_work_items",
                "domain_events",
                "outbox",
            )
        }
        stored_variants = json.loads(
            connection.execute(
                "SELECT variants_json FROM experiment_plans WHERE plan_id=?",
                (plan["plan_id"],),
            ).fetchone()[0]
        )
    assert counts == {
        "experiments": 1,
        "experiment_variants": 3,
        "operation_work_items": 1,
        "domain_events": 1,
        "outbox": 1,
    }
    assert stored_variants == []


def test_large_base_and_ten_thousand_overrides_remain_compact_after_commit(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    plan, _variants = _plan(10_000)
    base_blob = "x" * (256 * 1024)
    plan["base_arguments"] = {"blob": base_blob, "height": 64, "width": 64}
    plan["concurrency"] = 1
    plan["budget_totals"] = {**plan["budget_totals"], "concurrency": 1}
    variants = _compact_variants(plan, 10_000)

    repository.save_plan(plan, variants)
    repository.commit_plan(str(plan["plan_id"]), str(plan["plan_digest"]), str(plan["owner_id"]))

    with sqlite3.connect(store.path) as connection:
        enrollment_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(experiment_plan_variants)")
        }
        runtime_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(experiment_variants)")
        }
        retained_bytes, variants_json, variant_overrides_json = connection.execute(
            "SELECT retained_bytes,variants_json,variant_overrides_json "
            "FROM experiment_plans WHERE plan_id=?",
            (plan["plan_id"],),
        ).fetchone()
        enrollment_count, enrollment_override_bytes = connection.execute(
            "SELECT count(*),sum(length(CAST(overrides_json AS BLOB))) "
            "FROM experiment_plan_variants WHERE plan_id=?",
            (plan["plan_id"],),
        ).fetchone()
        runtime_count, runtime_override_bytes = connection.execute(
            "SELECT count(*),sum(length(CAST(overrides_json AS BLOB))) "
            "FROM experiment_variants WHERE experiment_id=?",
            (plan["experiment_id"],),
        ).fetchone()
        first_variant_id = connection.execute(
            "SELECT variant_id FROM experiment_variants WHERE experiment_id=? ORDER BY ordinal LIMIT 1",
            (plan["experiment_id"],),
        ).fetchone()[0]
    assert "arguments_json" not in enrollment_columns
    assert "arguments_json" not in runtime_columns
    assert {"overrides_json", "parameter_digest"} <= enrollment_columns
    assert "execution_input_digest" not in enrollment_columns
    assert {"overrides_json", "parameter_digest", "execution_input_digest"} <= runtime_columns
    assert variants_json == "[]"
    assert variant_overrides_json == "{}"
    assert enrollment_count == runtime_count == 10_000
    assert enrollment_override_bytes == runtime_override_bytes
    assert enrollment_override_bytes < 256 * 1024
    assert retained_bytes <= 8 * 1024 * 1024
    materialized = repository.get_variant(
        str(plan["experiment_id"]), str(first_variant_id), str(plan["owner_id"])
    )
    assert materialized is not None
    assert materialized["arguments"] == {"blob": base_blob, "height": 64, "seed": 0, "width": 64}


def test_retained_plan_ceiling_counts_enrollment_payload_before_insert(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    owner_id = "o" * 256
    plan, _variants = _plan(10_000, owner_id=owner_id)
    plan["concurrency"] = 1
    plan["budget_totals"] = {**plan["budget_totals"], "concurrency": 1}
    variants = _compact_variants(plan, 10_000, note="x" * 256)

    with pytest.raises(ValueError, match="retained payload exceeds 8 MiB"):
        repository.save_plan(plan, variants)

    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM experiment_plans").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM experiment_plan_variants").fetchone() == (
            0,
        )


def test_owner_live_plan_count_quota_rejects_before_atomic_insert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    monkeypatch.setattr(sqlite_experiments_module, "_MAX_OWNER_LIVE_PLAN_COUNT", 1)
    first_plan, first_variants = _identified_plan(1)
    second_plan, second_variants = _identified_plan(2)
    repository.save_plan(first_plan, first_variants)

    with pytest.raises(ValueError, match="owner live Experiment plan quota exceeded"):
        repository.save_plan(second_plan, second_variants)

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT plan_id FROM experiment_plans ORDER BY plan_id"
        ).fetchall() == [(first_plan["plan_id"],)]
        assert connection.execute("SELECT count(*) FROM experiment_plan_variants").fetchone() == (
            1,
        )


def test_owner_live_plan_byte_quota_rejects_accumulation_before_atomic_insert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    first_plan, first_variants = _identified_plan(1)
    second_plan, second_variants = _identified_plan(2)
    repository.save_plan(first_plan, first_variants)
    with sqlite3.connect(store.path) as connection:
        first_retained_bytes = int(
            connection.execute(
                "SELECT retained_bytes FROM experiment_plans WHERE plan_id=?",
                (first_plan["plan_id"],),
            ).fetchone()[0]
        )
    monkeypatch.setattr(
        sqlite_experiments_module, "_MAX_OWNER_LIVE_PLAN_BYTES", first_retained_bytes * 2 - 1
    )

    with pytest.raises(ValueError, match="owner live Experiment plan quota exceeded"):
        repository.save_plan(second_plan, second_variants)

    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM experiment_plans").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM experiment_plan_variants").fetchone() == (
            1,
        )


def test_cancelled_plan_holds_owner_quota_until_maintenance_prunes_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    monkeypatch.setattr(sqlite_experiments_module, "_MAX_OWNER_LIVE_PLAN_COUNT", 1)
    first_plan, first_variants = _identified_plan(1)
    second_plan, second_variants = _identified_plan(2)
    repository.save_plan(first_plan, first_variants)
    repository.commit_plan(
        str(first_plan["plan_id"]),
        str(first_plan["plan_digest"]),
        str(first_plan["owner_id"]),
    )
    cancelled = repository.cancel_experiment(
        str(first_plan["experiment_id"]), "cancel_queued", str(first_plan["owner_id"])
    )
    assert cancelled is not None and cancelled["status"] == "cancelled"

    with pytest.raises(ValueError, match="owner live Experiment plan quota exceeded"):
        repository.save_plan(second_plan, second_variants)

    before_grace = repository.apply_retention(now=datetime(2026, 8, 4, tzinfo=timezone.utc))
    assert before_grace["terminal_plans_pruned"] == 0
    with pytest.raises(ValueError, match="owner live Experiment plan quota exceeded"):
        repository.save_plan(second_plan, second_variants)
    retention = repository.apply_retention(now=datetime(2026, 8, 11, tzinfo=timezone.utc))
    assert retention["terminal_plans_pruned"] == 1
    repository.save_plan(second_plan, second_variants)
    with sqlite3.connect(store.path) as connection:
        pruned = connection.execute(
            """
            SELECT expansion_json,base_arguments_json,budgets_json,budget_totals_json,
                   variants_json,variant_overrides_json,retained_bytes,payload_pruned_at
            FROM experiment_plans WHERE plan_id=?
            """,
            (first_plan["plan_id"],),
        ).fetchone()
        assert pruned[:7] == ("{}", "{}", "{}", "{}", "[]", "{}", 12)
        assert pruned[7]
        assert connection.execute(
            "SELECT overrides_json,parameter_digest FROM experiment_plan_variants WHERE plan_id=?",
            (first_plan["plan_id"],),
        ).fetchone() == ("{}", first_variants[0]["parameter_digest"])
        assert connection.execute(
            "SELECT overrides_json,status FROM experiment_variants WHERE experiment_id=?",
            (first_plan["experiment_id"],),
        ).fetchone() == ("{}", "cancelled")


def test_plan_save_rejects_same_identity_with_changed_stored_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    plan, variants = _plan(1)
    repository.save_plan(plan, variants)

    changed = dict(plan)
    changed["concurrency"] = 2
    with pytest.raises(ValueError, match="conflict"):
        repository.save_plan(changed, variants)


def test_variant_pages_are_bounded_keysets_and_owner_bound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    plan, variants = _plan(1001)
    repository.save_plan(plan, variants)
    repository.commit_plan(str(plan["plan_id"]), str(plan["plan_digest"]), str(plan["owner_id"]))

    seen: list[str] = []
    after: tuple[str, str] | None = None
    page_sizes: list[int] = []
    while True:
        page, has_more = repository.list_variants(
            str(plan["experiment_id"]), str(plan["owner_id"]), limit=137, after=after
        )
        page_sizes.append(len(page))
        seen.extend(str(item["variant_id"]) for item in page)
        assert all("arguments" in item and "resource_uri" in item for item in page)
        if not has_more:
            break
        after = (str(page[-1]["created_at"]), str(page[-1]["variant_id"]))

    assert len(seen) == len(set(seen)) == 1001
    assert page_sizes == [137] * 7 + [42]
    assert repository.list_variants(
        str(plan["experiment_id"]), "owner-b", limit=100, after=None
    ) == ([], False)
    first = repository.get_variant(
        str(plan["experiment_id"]), str(variants[0]["variant_id"]), str(plan["owner_id"])
    )
    assert first is not None and first["job_id"] == ""
    assert (
        repository.get_variant(
            str(plan["experiment_id"]), str(variants[0]["variant_id"]), "owner-b"
        )
        is None
    )
    assert (
        repository.resource_owner_for_uri(f"comfyui://experiments/{plan['experiment_id']}")
        == plan["owner_id"]
    )
    assert (
        repository.resource_owner_for_uri(
            f"comfyui://experiments/{plan['experiment_id']}/variants/{variants[0]['variant_id']}"
        )
        == plan["owner_id"]
    )
    assert (
        repository.resource_owner_for_uri(
            f"comfyui://experiments/{plan['experiment_id']}/variants/not-a-variant/extra"
        )
        is None
    )


def test_cancel_modes_are_owner_bound_idempotent_and_escalate_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    plan, variants = _plan(2)
    repository.save_plan(plan, variants)
    repository.commit_plan(str(plan["plan_id"]), str(plan["plan_digest"]), str(plan["owner_id"]))

    assert repository.cancel_experiment(str(plan["experiment_id"]), "stop_new", "owner-b") is None
    stopped = repository.cancel_experiment(
        str(plan["experiment_id"]), "stop_new", str(plan["owner_id"])
    )
    repeated = repository.cancel_experiment(
        str(plan["experiment_id"]), "stop_new", str(plan["owner_id"])
    )
    with pytest.raises(ValueError, match="terminal"):
        repository.cancel_experiment(
            str(plan["experiment_id"]), "cancel_queued", str(plan["owner_id"])
        )
    assert stopped == repeated
    assert stopped is not None and stopped["status"] == "cancelled"
    assert stopped["cancel_mode"] == "stop_new"
    assert stopped["pending_count"] == 0
    assert stopped["cancelled_count"] == 2


def test_worker_transition_checkpoint_is_fenced_and_lost_never_resubmits(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    plan, variants = _plan(2)
    repository.save_plan(plan, variants)
    repository.commit_plan(str(plan["plan_id"]), str(plan["plan_digest"]), str(plan["owner_id"]))
    now = datetime.now(timezone.utc)
    lease = SQLiteOrchestrationRepository(store).acquire_next("worker-a", now=now, lease_seconds=30)
    assert lease is not None
    page = repository.list_for_advance(str(plan["experiment_id"]), str(plan["owner_id"]), limit=1)
    assert [item["variant_id"] for item in page] == [variants[0]["variant_id"]]
    repository.apply_transition(
        lease,
        experiment_id=str(plan["experiment_id"]),
        owner_id=str(plan["owner_id"]),
        variant_id=str(variants[0]["variant_id"]),
        status="submitted",
        job_id="",
        checkpoint={"last_variant_id": variants[0]["variant_id"]},
        now=now,
        event_type="EXPERIMENT_VARIANT_UPDATED",
        event_data={"variant_id": variants[0]["variant_id"], "status": "submitted"},
    )
    context = repository.get_experiment(str(plan["experiment_id"]), str(plan["owner_id"]))
    assert context is not None
    assert (context["pending_count"], context["submitted_count"]) == (1, 1)

    repository.finish_advance(
        lease,
        experiment_id=str(plan["experiment_id"]),
        owner_id=str(plan["owner_id"]),
        checkpoint={"last_variant_id": variants[0]["variant_id"]},
        now=now,
        completed=False,
        delay_seconds=1,
        status="running",
    )
    takeover = SQLiteOrchestrationRepository(store).acquire_next(
        "worker-b", now=now + timedelta(seconds=2), lease_seconds=30
    )
    assert takeover is not None and takeover.fencing_token == lease.fencing_token + 1
    with pytest.raises(RuntimeError, match="fenced"):
        repository.apply_transition(
            lease,
            experiment_id=str(plan["experiment_id"]),
            owner_id=str(plan["owner_id"]),
            variant_id=str(variants[0]["variant_id"]),
            status="running",
            job_id="",
            checkpoint={},
            now=now + timedelta(seconds=2),
            event_type="EXPERIMENT_VARIANT_UPDATED",
            event_data={},
        )

    repository.apply_transition(
        takeover,
        experiment_id=str(plan["experiment_id"]),
        owner_id=str(plan["owner_id"]),
        variant_id=str(variants[0]["variant_id"]),
        status="lost",
        job_id="",
        checkpoint={},
        now=now + timedelta(seconds=2),
        event_type="EXPERIMENT_VARIANT_LOST",
        event_data={},
    )
    with pytest.raises(sqlite3.IntegrityError, match="state transition"):
        repository.apply_transition(
            takeover,
            experiment_id=str(plan["experiment_id"]),
            owner_id=str(plan["owner_id"]),
            variant_id=str(variants[0]["variant_id"]),
            status="submitted",
            job_id="",
            checkpoint={},
            now=now + timedelta(seconds=3),
            event_type="EXPERIMENT_VARIANT_UPDATED",
            event_data={},
        )


def test_rating_is_owner_bound_idempotent_and_promotion_is_immutable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    plan, variants = _plan(1)
    repository.save_plan(plan, variants)
    repository.commit_plan(str(plan["plan_id"]), str(plan["plan_digest"]), str(plan["owner_id"]))
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE experiment_variants
            SET status='completed',completed_at=?,updated_at=?
            WHERE experiment_id=? AND variant_id=? AND owner_id=?
            """,
            (
                plan["created_at"],
                plan["created_at"],
                plan["experiment_id"],
                variants[0]["variant_id"],
                plan["owner_id"],
            ),
        )
    rating = {
        "rating_id": "rating_" + "d" * 64,
        "owner_id": plan["owner_id"],
        "experiment_id": plan["experiment_id"],
        "variant_id": variants[0]["variant_id"],
        "rubric_version": "v1",
        "scores": {"quality": 4, "prompt_adherence": 5, "technical_quality": 3.5},
        "created_at": plan["created_at"],
    }
    assert repository.save_rating(rating) == rating
    assert repository.save_rating(rating) == rating
    with pytest.raises(LookupError):
        repository.save_rating({**rating, "owner_id": "owner-b"})

    preset = repository.promote_variant(
        str(plan["experiment_id"]), str(variants[0]["variant_id"]), "preset", str(plan["owner_id"])
    )
    assert preset["target"] == "preset" and preset["preset_id"].startswith("preset_")
    assert (
        repository.promote_variant(
            str(plan["experiment_id"]),
            str(variants[0]["variant_id"]),
            "preset",
            str(plan["owner_id"]),
        )
        == preset
    )
    revision = repository.promote_variant(
        str(plan["experiment_id"]),
        str(variants[0]["variant_id"]),
        "revision",
        str(plan["owner_id"]),
    )
    assert revision["published"] is False
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute(
                "SELECT published FROM workflow_deployments WHERE revision_id=?",
                (revision["revision_id"],),
            ).fetchone()
            is None
        )


def test_commit_fault_rolls_back_aggregate_variants_work_event_and_outbox(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _workflow(store)
    plan, variants = _plan(3)
    SQLiteExperimentRepository(store).save_plan(plan, variants)

    def fail(point: str) -> None:
        if point == "variants":
            raise RuntimeError("injected commit fault")

    repository = SQLiteExperimentRepository(store, fault_injector=fail)
    with pytest.raises(RuntimeError, match="injected commit fault"):
        repository.commit_plan(
            str(plan["plan_id"]), str(plan["plan_digest"]), str(plan["owner_id"])
        )
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM experiments").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM experiment_variants").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM operation_work_items").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM domain_events").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM outbox").fetchone() == (0,)
        assert connection.execute(
            "SELECT committed_at,committed_experiment_id FROM experiment_plans"
        ).fetchone() == (None, None)

    committed = SQLiteExperimentRepository(store).commit_plan(
        str(plan["plan_id"]), str(plan["plan_digest"]), str(plan["owner_id"])
    )
    assert committed["variant_count"] == 3


def test_fresh_and_v6_upgrade_apply_phase_migration_seven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = (tmp_path / "control-plane.sqlite3").resolve()
    old_migrations = control_plane_module._MIGRATIONS
    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", old_migrations[:6])
    store = SQLiteControlPlaneStore(database)
    store.initialize()
    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", old_migrations)
    store.initialize()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone() == (8,)
        assert connection.execute("SELECT count(*) FROM experiment_plans").fetchone() == (0,)


def test_populated_v6_upgrade_backfills_remote_job_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = (tmp_path / "populated-v6.sqlite3").resolve()
    migrations = control_plane_module._MIGRATIONS
    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", migrations[:6])
    store = SQLiteControlPlaneStore(database)
    store.initialize()
    _workflow(store)
    created_at = "2026-08-03T00:00:00+00:00"
    revision_id = "revision_" + "1" * 64
    deployment_id = "deployment_" + "4" * 64
    execution_plan_id = "plan_" + "5" * 64
    job_id = "job_" + "6" * 64
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """INSERT INTO workflow_deployments(
                deployment_id,workflow_id,revision_id,server_id,enabled,
                validation_status,published,created_at
            ) VALUES(?,'portrait',?,'remote',1,'valid',0,?)""",
            (deployment_id, revision_id, created_at),
        )
        connection.execute(
            """INSERT INTO execution_plans(
                plan_id,workflow_id,revision_id,deployment_id,server_id,
                resolved_inputs_json,input_digest,plan_digest,created_at
            ) VALUES(?,'portrait',?,?,'remote','{}',?,?,?)""",
            (execution_plan_id, revision_id, deployment_id, "7" * 64, "8" * 64, created_at),
        )
        connection.execute(
            """INSERT INTO jobs(
                job_id,workflow_id,plan_id,revision_id,deployment_id,owner_id,
                status,created_at,created_at_source,legacy_migrated,execution_origin
            ) VALUES(?,'portrait',?,?,?,'owner-a','submitted',?,'test',0,'planned')""",
            (job_id, execution_plan_id, revision_id, deployment_id, created_at),
        )
        connection.execute(
            """INSERT INTO execution_attempts(
                attempt_id,job_id,attempt,server_id,upstream_prompt_id,
                client_id,submission_state,created_at
            ) VALUES(?,?,1,'remote','prompt-remote','client-remote','submitted',?)""",
            ("attempt_" + "9" * 64, job_id, created_at),
        )
    monkeypatch.setattr(control_plane_module, "_MIGRATIONS", migrations)
    store.initialize()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT server_id FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone() == ("remote",)


def test_variant_page_batches_related_projection_queries(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    plan, variants = _plan(100)
    repository.save_plan(plan, variants)
    repository.commit_plan(str(plan["plan_id"]), str(plan["plan_digest"]), str(plan["owner_id"]))
    statements: list[str] = []
    original_connect = repository._connect

    def traced_connect() -> sqlite3.Connection:
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    repository._connect = traced_connect  # type: ignore[method-assign]
    page, has_more = repository.list_variants(
        str(plan["experiment_id"]), str(plan["owner_id"]), limit=100, after=None
    )
    assert len(page) == 100 and has_more is False
    assert (
        3 <= sum(statement.lstrip().upper().startswith("SELECT") for statement in statements) <= 4
    )


def test_first_commit_evidence_write_requires_complete_aggregate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    plan, variants = _plan(1)
    repository.save_plan(plan, variants)
    with (
        sqlite3.connect(store.path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="commit evidence"),
    ):
        connection.execute(
            """UPDATE experiment_plans SET committed_at=?,committed_experiment_id=?
               WHERE plan_id=? AND owner_id=?""",
            (
                plan["created_at"],
                plan["experiment_id"],
                plan["plan_id"],
                plan["owner_id"],
            ),
        )


def test_commit_evidence_rejects_terminal_aggregate_with_pending_variant(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    plan, variants = _plan(1)
    repository.save_plan(plan, variants)
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """INSERT INTO experiments(
                experiment_id,owner_id,plan_id,workflow_id,server_id,
                pinned_revision_id,pinned_deployment_id,pinned_content_digest,
                status,failure_policy,concurrency,execution_slots,submission_window,
                variant_count,pending_count,submitted_count,running_count,
                completed_count,failed_count,cancelled_count,lost_count,
                created_at,updated_at,completed_at
            ) SELECT experiment_id,owner_id,plan_id,workflow_id,server_id,
                     pinned_revision_id,pinned_deployment_id,pinned_content_digest,
                     'completed',failure_policy,concurrency,execution_slots,submission_window,
                     variant_count,0,0,0,variant_count,0,0,0,created_at,created_at,created_at
              FROM experiment_plans WHERE plan_id=?""",
            (plan["plan_id"],),
        )
        connection.execute(
            """INSERT INTO experiment_variants(
                variant_id,experiment_id,owner_id,ordinal,overrides_json,
                parameter_digest,client_id,idempotency_key,status,created_at,updated_at
            ) SELECT variant_id,experiment_id,owner_id,ordinal,overrides_json,
                     parameter_digest,'forged-client','forged-key','pending',created_at,created_at
              FROM experiment_plan_variants WHERE plan_id=?""",
            (plan["plan_id"],),
        )
        with pytest.raises(sqlite3.IntegrityError, match="physical Variant facts"):
            connection.execute(
                """UPDATE experiment_plans SET committed_at=?,committed_experiment_id=?
                   WHERE plan_id=?""",
                (plan["created_at"], plan["experiment_id"], plan["plan_id"]),
            )


def test_experiment_owner_and_cross_experiment_job_bindings_fail(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    first_plan, first_variants = _plan(1)
    repository.save_plan(first_plan, first_variants)
    repository.commit_plan(
        str(first_plan["plan_id"]), str(first_plan["plan_digest"]), str(first_plan["owner_id"])
    )
    second_plan, second_variants = _plan(1)
    second_plan.update(
        {
            "plan_id": "experiment_plan_" + "e" * 64,
            "plan_digest": "e" * 64,
            "experiment_id": "experiment_" + "f" * 64,
        }
    )
    second_variants = [
        {
            **second_variants[0],
            "experiment_id": second_plan["experiment_id"],
            "variant_id": "variant_" + "f" * 64,
        }
    ]
    repository.save_plan(second_plan, second_variants)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO experiment_server_capacities(server_id,execution_slots,subject_submission_quota,updated_at) VALUES('local',2,0,?)",
            (second_plan["created_at"],),
        )
    repository.commit_plan(
        str(second_plan["plan_id"]), str(second_plan["plan_digest"]), str(second_plan["owner_id"])
    )
    job_id = "job_" + "9" * 64
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO jobs(job_id,workflow_id,owner_id,status,created_at,created_at_source,legacy_migrated,execution_origin)
            VALUES(?,?,?,'submitted',?,'test',1,'legacy_migrated')
            """,
            (job_id, "portrait", "owner-b", first_plan["created_at"]),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO experiment_variant_jobs(experiment_id,variant_id,owner_id,job_id,linked_at) VALUES(?,?,?,?,?)",
                (
                    first_plan["experiment_id"],
                    first_variants[0]["variant_id"],
                    "owner-a",
                    job_id,
                    first_plan["created_at"],
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO experiment_variant_jobs(experiment_id,variant_id,owner_id,job_id,linked_at) VALUES(?,?,?,?,?)",
                (
                    first_plan["experiment_id"],
                    second_variants[0]["variant_id"],
                    "owner-a",
                    job_id,
                    first_plan["created_at"],
                ),
            )
        connection.execute(
            "UPDATE experiment_variants SET status='lost',completed_at=?,updated_at=? WHERE experiment_id=?",
            (first_plan["created_at"], first_plan["created_at"], first_plan["experiment_id"]),
        )
        with pytest.raises(sqlite3.IntegrityError, match="state transition"):
            connection.execute(
                "UPDATE experiment_variants SET status='submitted',completed_at=NULL WHERE experiment_id=?",
                (first_plan["experiment_id"],),
            )


def test_experiment_service_round_trip_uses_sqlite_repository(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _workflow(store)
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    repository = SQLiteExperimentRepository(store)
    service = ExperimentService(
        repository,
        rubrics={"custom-v1": {"quality": (0.0, 5.0)}},
        clock=lambda: now,
    )
    planned = service.plan(
        "owner-a",
        "portrait",
        "local",
        {"mode": "explicit", "variants": [{"seed": 7}]},
        {"width": 64, "height": 64},
        {
            "max_variants": 2,
            "max_concurrency": 1,
            "max_pixels": 4096,
            "max_outputs": 1,
            "max_seconds": 10,
        },
        "continue",
        1,
        0,
    )
    committed = service.commit(planned["plan_id"], planned["plan_digest"], "owner-a")
    page = service.list_variants(committed["experiment_id"], "owner-a", 10, None)
    assert committed["status"] == "queued"
    assert len(page["items"]) == 1
    assert "arguments" not in page["items"][0]
    variant_id = page["items"][0]["variant_id"]
    rating = service.rate(
        committed["experiment_id"], variant_id, "custom-v1", {"quality": 4.5}, "owner-a"
    )
    assert rating["scores"] == {"quality": 4.5}
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE experiment_variants SET status='completed',completed_at=?,updated_at=? WHERE variant_id=?",
            (now.isoformat(), now.isoformat(), variant_id),
        )
    assert committed["plan_digest"] == planned["plan_digest"]
    promotion = service.promote(committed["experiment_id"], variant_id, "preset", "owner-a")
    assert promotion["target"] == "preset"


def test_phase_m_schema_pins_publication_and_bounds_with_compact_retention(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        plan_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(experiment_plans)")
        }
        experiment_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(experiments)")
        }
        assert {
            "pinned_revision_id",
            "pinned_deployment_id",
            "pinned_content_digest",
            "expires_at",
            "retained_bytes",
            "variant_overrides_json",
        } <= plan_columns
        assert {
            "pinned_revision_id",
            "pinned_deployment_id",
            "pinned_content_digest",
        } <= experiment_columns
        table_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_schema WHERE name='experiment_plans'"
            ).fetchone()[0]
        )
        assert "8388608" in table_sql


def test_commit_rejects_published_pin_drift_and_variant_enrollment_tampering(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    plan, variants = _plan(2)
    repository.save_plan(plan, variants)
    with sqlite3.connect(store.path) as connection:
        pin = connection.execute(
            "SELECT pinned_revision_id,pinned_deployment_id FROM experiment_plans WHERE plan_id=?",
            (plan["plan_id"],),
        ).fetchone()
        assert pin is not None
        connection.execute(
            "UPDATE workflow_deployments SET published=0 WHERE deployment_id=?",
            (pin[1],),
        )
    with pytest.raises(ValueError, match="published"):
        repository.commit_plan(
            str(plan["plan_id"]), str(plan["plan_digest"]), str(plan["owner_id"])
        )

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE workflow_deployments SET published=1 WHERE deployment_id=?",
            (pin[1],),
        )
        with pytest.raises(sqlite3.IntegrityError, match="enrollment is immutable"):
            connection.execute(
                "DELETE FROM experiment_plan_variants WHERE plan_id=?",
                (plan["plan_id"],),
            )
    committed = repository.commit_plan(
        str(plan["plan_id"]), str(plan["plan_digest"]), str(plan["owner_id"])
    )
    assert committed["variant_count"] == 2


def test_expired_uncommitted_plans_are_cleaned_without_removing_audit_facts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    plan, variants = _plan(1)
    expired_plan = {
        **plan,
        "created_at": "2019-01-01T00:00:00+00:00",
        "expires_at": "2020-01-01T00:00:00+00:00",
    }
    expired_variants = [
        {
            **variants[0],
            "created_at": expired_plan["created_at"],
            "updated_at": expired_plan["created_at"],
        }
    ]
    repository.save_plan(expired_plan, expired_variants)
    result = repository.cleanup_expired_plans(
        now=datetime(2026, 8, 3, tzinfo=timezone.utc), owner_id=str(plan["owner_id"])
    )
    assert result["plans_deleted"] == 1
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM experiment_plans").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM domain_events").fetchone() == (0,)


def test_production_maintenance_cleans_expired_experiment_plans_and_reports_counts(
    tmp_path: Path,
) -> None:
    store = SQLiteControlPlaneStore((tmp_path / "data" / "control-plane.sqlite3").resolve())
    store.initialize()
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    plan, variants = _plan(1)
    expired_plan = {
        **plan,
        "created_at": "2019-01-01T00:00:00+00:00",
        "expires_at": "2020-01-01T00:00:00+00:00",
    }
    expired_variants = [
        {
            **variants[0],
            "created_at": expired_plan["created_at"],
            "updated_at": expired_plan["created_at"],
        }
    ]
    repository.save_plan(expired_plan, expired_variants)

    result = run_maintenance(
        tmp_path,
        run_days=0,
        asset_days=0,
        max_history_records=0,
    )

    assert result == {
        "runs_deleted": 0,
        "assets_deleted": 0,
        "experiment_plans_deleted": 1,
        "experiment_terminal_plans_pruned": 0,
        "experiment_terminal_payloads_compacted": 0,
    }
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM experiment_plans").fetchone() == (0,)


def test_claim_variant_requires_live_lease_and_cancel_queued_persists_intent(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    plan, variants = _plan(1)
    repository.save_plan(plan, variants)
    repository.commit_plan(str(plan["plan_id"]), str(plan["plan_digest"]), str(plan["owner_id"]))
    now = datetime.now(timezone.utc)
    lease = SQLiteOrchestrationRepository(store).acquire_next("worker-a", now=now, lease_seconds=30)
    assert lease is not None
    claim = repository.claim_variant_for_submission(
        lease,
        experiment_id=str(plan["experiment_id"]),
        owner_id=str(plan["owner_id"]),
        variant_id=str(variants[0]["variant_id"]),
        now=now,
    )
    assert claim["status"] == "submitted"
    cancellation = repository.cancel_experiment(
        str(plan["experiment_id"]), "cancel_queued", str(plan["owner_id"])
    )
    assert cancellation is not None and cancellation["cancel_mode"] == "cancel_queued"
    repository.finish_advance(
        lease,
        experiment_id=str(plan["experiment_id"]),
        owner_id=str(plan["owner_id"]),
        checkpoint={},
        now=now,
        completed=False,
        delay_seconds=1,
        status="running",
    )
    with (
        sqlite3.connect(store.path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="lease is expired or fenced"),
    ):
        connection.execute(
            "UPDATE experiment_variants SET checkpoint_json='{"
            + '"forged":true'
            + "}' WHERE variant_id=?",
            (variants[0]["variant_id"],),
        )


def test_direct_sql_cannot_forge_variant_counts_or_commit_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    plan, variants = _plan(1)
    repository.save_plan(plan, variants)
    repository.commit_plan(str(plan["plan_id"]), str(plan["plan_digest"]), str(plan["owner_id"]))
    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="commit evidence"):
            connection.execute(
                "UPDATE experiment_plans SET committed_at='2099-01-01T00:00:00+00:00' WHERE plan_id=?",
                (plan["plan_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="aggregate counts"):
            connection.execute(
                "UPDATE experiments SET pending_count=0,failed_count=1 WHERE experiment_id=?",
                (plan["experiment_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="enrollment conflict"):
            connection.execute(
                """
                INSERT INTO experiment_variants(
                    variant_id,experiment_id,owner_id,ordinal,overrides_json,
                    parameter_digest,client_id,idempotency_key,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "variant_" + "f" * 64,
                    plan["experiment_id"],
                    plan["owner_id"],
                    1,
                    "{}",
                    "f" * 64,
                    "forged-client",
                    "forged-key",
                    "pending",
                    plan["created_at"],
                    plan["created_at"],
                ),
            )


def test_ten_thousand_variant_worker_query_uses_bounded_index(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    plan, variants = _plan(10_000)
    plan["concurrency"] = 1
    plan["budget_totals"] = {**plan["budget_totals"], "concurrency": 1}
    repository.save_plan(plan, variants)
    repository.commit_plan(str(plan["plan_id"]), str(plan["plan_digest"]), str(plan["owner_id"]))
    with sqlite3.connect(store.path) as connection:
        query_plan = " ".join(
            str(row[3])
            for row in connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT variant_id FROM experiment_variants INDEXED BY ix_experiment_variants_worker
                WHERE experiment_id=? AND owner_id=?
                  AND status IN ('pending','submitted','running')
                ORDER BY ordinal,variant_id LIMIT 100
                """,
                (plan["experiment_id"], plan["owner_id"]),
            ).fetchall()
        )
    assert "ix_experiment_variants_worker" in query_plan


def test_measured_budget_overflow_commits_terminal_fact_and_pauses_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _workflow(store)
    repository = SQLiteExperimentRepository(store)
    plan, variants = _plan(1)
    repository.save_plan(plan, variants)
    repository.commit_plan(str(plan["plan_id"]), str(plan["plan_digest"]), str(plan["owner_id"]))
    now = datetime.now(timezone.utc)
    lease = SQLiteOrchestrationRepository(store).acquire_next("worker-a", now=now, lease_seconds=30)
    assert lease is not None
    repository.claim_variant_for_submission(
        lease,
        experiment_id=str(plan["experiment_id"]),
        owner_id=str(plan["owner_id"]),
        variant_id=str(variants[0]["variant_id"]),
        now=now,
    )
    checkpoint: dict[str, object] = {}
    repository.apply_transition(
        lease,
        experiment_id=str(plan["experiment_id"]),
        owner_id=str(plan["owner_id"]),
        variant_id=str(variants[0]["variant_id"]),
        status="completed",
        job_id="",
        checkpoint=checkpoint,
        now=now + timedelta(seconds=1),
        event_type="EXPERIMENT_VARIANT_UPDATED",
        event_data={
            "measured_pixels": 4097,
            "measured_outputs": 1,
            "measured_seconds": 1.0,
        },
    )
    assert checkpoint["pause_reason"] == "MEASURED_BUDGET_EXCEEDED"
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT status,measured_pixels,measured_outputs,measured_seconds FROM experiment_variants"
        ).fetchone() == ("completed", 4097, 1, 1.0)
        stored_checkpoint = json.loads(
            connection.execute(
                "SELECT checkpoint_json FROM operation_work_items WHERE work_item_id=?",
                (lease.work_item_id,),
            ).fetchone()[0]
        )
    assert stored_checkpoint["pause_reason"] == "MEASURED_BUDGET_EXCEEDED"
