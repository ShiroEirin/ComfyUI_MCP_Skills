"""Owner-bound SQLite persistence for durable Experiments and Variants."""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from comfyui_mcp_skills.domain.control_plane import derived_control_plane_id
from comfyui_mcp_skills.domain.orchestration import WorkLease
from comfyui_mcp_skills.domain.workflow_schema import normalize_parameters, validate_arguments
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.sqlite_workflows import _revision_digest

_TERMINAL_EXPERIMENT_STATUSES = frozenset({"completed", "completed_with_errors", "cancelled"})
_TERMINAL_VARIANT_STATUSES = frozenset({"completed", "failed", "cancelled", "lost"})

_MAX_RETAINED_PLAN_BYTES = 8 * 1024 * 1024
_MAX_OWNER_LIVE_PLAN_COUNT = 32
_MAX_OWNER_LIVE_PLAN_BYTES = 32 * 1024 * 1024
_MAX_EXECUTION_INPUT_BYTES = 1024 * 1024
_DEFAULT_PLAN_TTL = timedelta(days=1)
_PROMOTION_GRACE = timedelta(days=7)


class SQLiteExperimentRepository:
    """Persist Experiment plans, aggregate progress, ratings, and promotions."""

    def __init__(
        self,
        store: SQLiteControlPlaneStore,
        *,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._fault_injector = fault_injector

    def resolve_planning_context(
        self, owner_id: str, workflow_id: str, server_id: str
    ) -> dict[str, Any]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            context = _ensure_published_context(
                connection,
                owner_id=owner_id,
                workflow_id=workflow_id,
                server_id=server_id,
                created_at=_time(datetime.now(timezone.utc)),
            )
            connection.commit()
            return context
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save_plan(self, plan: dict[str, Any], variants: Sequence[dict[str, Any]]) -> dict[str, Any]:
        variant_count = int(plan["variant_count"])
        concurrency = int(plan["concurrency"])
        submission_window = int(plan["submission_window"])
        execution_slots = int(plan.get("execution_slots", 1))
        output_cardinality = int(
            plan.get(
                "output_cardinality",
                max(1, int(plan["budget_totals"]["outputs"]) // variant_count),
            )
        )
        trusted_seconds_per_run = float(
            plan.get(
                "trusted_seconds_per_run",
                float(plan["budget_totals"]["seconds"]) / variant_count,
            )
        )
        if variant_count != len(variants) or not 1 <= variant_count <= 10_000:
            raise ValueError("Experiment plan variant count conflict")
        if not 1 <= concurrency <= 64 or not 1 <= execution_slots <= 64:
            raise ValueError("Experiment plan concurrency exceeds server ceiling")
        if not 0 <= submission_window <= 10_000:
            raise ValueError("Experiment plan submission window exceeds server ceiling")
        if not 1 <= output_cardinality <= 100_000 or not 0 < trusted_seconds_per_run <= 31_536_000:
            raise ValueError("Experiment plan trusted estimates exceed server ceiling")
        encoded, enrollments = _encode_plan_payload(plan, variants)
        created_at = str(plan["created_at"])
        expires_at = str(
            plan.get("expires_at")
            or _time(
                _parse_time(created_at, field="Experiment plan created_at") + _DEFAULT_PLAN_TTL
            )
        )
        if _parse_time(expires_at, field="Experiment plan expires_at") <= _parse_time(
            created_at, field="Experiment plan created_at"
        ):
            raise ValueError("Experiment plan expiry must follow creation")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            context = _ensure_published_context(
                connection,
                owner_id=str(plan["owner_id"]),
                workflow_id=str(plan["workflow_id"]),
                server_id=str(plan["server_id"]),
                created_at=created_at,
                expected_revision_id=plan.get("pinned_revision_id") or plan.get("revision_id"),
                expected_deployment_id=plan.get("pinned_deployment_id")
                or plan.get("deployment_id"),
                expected_content_digest=plan.get("pinned_content_digest")
                or plan.get("content_digest"),
            )
            retained_bytes = _retained_plan_bytes(
                plan,
                encoded=encoded,
                enrollments=enrollments,
                context=context,
                expires_at=expires_at,
            )
            if retained_bytes > _MAX_RETAINED_PLAN_BYTES:
                raise ValueError("Experiment plan retained payload exceeds 8 MiB")
            existing = connection.execute(
                """
                SELECT experiment_id,owner_id,workflow_id,server_id,plan_digest,
                       pinned_revision_id,pinned_deployment_id,pinned_content_digest,
                       expansion_json,base_arguments_json,budgets_json,
                       budget_totals_json,failure_policy,concurrency,execution_slots,
                       submission_window,variant_count,output_cardinality,
                       trusted_seconds_per_run,variants_json,variant_overrides_json,
                       retained_bytes,created_at,expires_at
                FROM experiment_plans WHERE plan_id=?
                """,
                (plan["plan_id"],),
            ).fetchone()
            expected = (
                plan["experiment_id"],
                plan["owner_id"],
                plan["workflow_id"],
                plan["server_id"],
                plan["plan_digest"],
                context["revision_id"],
                context["deployment_id"],
                context["content_digest"],
                encoded["expansion"],
                encoded["base_arguments"],
                encoded["budgets"],
                encoded["budget_totals"],
                plan["failure_policy"],
                concurrency,
                execution_slots,
                submission_window,
                variant_count,
                output_cardinality,
                trusted_seconds_per_run,
                encoded["variants"],
                encoded["variant_overrides"],
                retained_bytes,
                created_at,
                expires_at,
            )
            if existing is not None:
                if tuple(existing) != expected:
                    raise ValueError("Experiment plan identity conflict")
                stored_enrollments = connection.execute(
                    """
                    SELECT plan_id,owner_id,experiment_id,variant_id,ordinal,
                           overrides_json,parameter_digest,created_at
                    FROM experiment_plan_variants WHERE plan_id=? ORDER BY ordinal,variant_id
                    """,
                    (plan["plan_id"],),
                ).fetchall()
                if [tuple(row) for row in stored_enrollments] != enrollments:
                    raise ValueError("Experiment plan Variant enrollment conflict")
            else:
                _enforce_owner_plan_quota(
                    connection,
                    owner_id=str(plan["owner_id"]),
                    retained_bytes=retained_bytes,
                )
                connection.execute(
                    """
                    INSERT INTO experiment_plans(
                        plan_id,experiment_id,owner_id,workflow_id,server_id,
                        plan_digest,pinned_revision_id,pinned_deployment_id,
                        pinned_content_digest,expansion_json,base_arguments_json,
                        budgets_json,budget_totals_json,failure_policy,concurrency,
                        execution_slots,submission_window,variant_count,
                        output_cardinality,trusted_seconds_per_run,variants_json,
                        variant_overrides_json,retained_bytes,created_at,expires_at,
                        committed_at,committed_experiment_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)
                    """,
                    (plan["plan_id"], *expected),
                )
                connection.executemany(
                    """
                    INSERT INTO experiment_plan_variants(
                        plan_id,owner_id,experiment_id,variant_id,ordinal,
                        overrides_json,parameter_digest,created_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    enrollments,
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return _json_copy(plan)

    def commit_plan(
        self,
        plan_id: str,
        plan_digest: str,
        owner_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            plan = connection.execute(
                """
                SELECT experiment_id,workflow_id,server_id,plan_digest,
                       pinned_revision_id,pinned_deployment_id,pinned_content_digest,
                       failure_policy,concurrency,execution_slots,submission_window,
                       variant_count,base_arguments_json,created_at,expires_at,committed_at
                FROM experiment_plans WHERE plan_id=? AND owner_id=?
                """,
                (plan_id, owner_id),
            ).fetchone()
            if plan is None:
                raise LookupError("Experiment plan was not found")
            if str(plan["plan_digest"]) != plan_digest:
                raise ValueError("Experiment plan digest conflict")
            experiment_id = str(plan["experiment_id"])
            if plan["committed_at"] is not None:
                result = self._get_experiment(connection, experiment_id, owner_id)
                if result is None:
                    raise RuntimeError("committed Experiment aggregate is missing")
                connection.commit()
                return result
            committed_at = _time(now or datetime.now(timezone.utc))
            if _parse_time(
                str(plan["expires_at"]), field="Experiment plan expires_at"
            ) <= _parse_time(committed_at, field="Experiment commit time"):
                raise ValueError("Experiment plan has expired")
            publication = connection.execute(
                """SELECT 1 FROM workflow_deployments AS deployments
                JOIN workflow_revisions AS revisions
                  ON revisions.workflow_id=deployments.workflow_id
                 AND revisions.revision_id=deployments.revision_id
                WHERE deployments.deployment_id=? AND deployments.workflow_id=?
                  AND deployments.revision_id=? AND deployments.server_id=?
                  AND deployments.enabled=1 AND deployments.validation_status='valid'
                  AND revisions.content_digest=?
                  AND (
                    (NOT EXISTS (SELECT 1 FROM config_workflow_snapshots WHERE owner_id=?)
                     AND deployments.published=1)
                    OR EXISTS (
                        SELECT 1 FROM config_workflow_deployments AS bindings
                        JOIN config_workflow_states AS states ON states.owner_id=bindings.owner_id
                         AND states.server_id=bindings.server_id
                         AND states.workflow_id=bindings.workflow_id
                        JOIN managed_servers AS servers ON servers.owner_id=bindings.owner_id
                         AND servers.server_id=bindings.server_id
                         AND servers.lifecycle_status='active'
                        WHERE bindings.owner_id=?
                         AND bindings.server_id=deployments.server_id
                         AND bindings.workflow_id=deployments.workflow_id
                         AND bindings.deployment_id=deployments.deployment_id
                         AND states.enabled=1
                    )
                  )""",
                (
                    plan["pinned_deployment_id"],
                    plan["workflow_id"],
                    plan["pinned_revision_id"],
                    plan["server_id"],
                    plan["pinned_content_digest"],
                    owner_id,
                    owner_id,
                ),
            ).fetchone()
            if publication is None:
                raise ValueError("Experiment plan pinned deployment is no longer published")
            enrollment_rows = connection.execute(
                """
                SELECT variant_id,ordinal,overrides_json,parameter_digest,created_at
                FROM experiment_plan_variants
                WHERE plan_id=? AND owner_id=? AND experiment_id=?
                ORDER BY ordinal,variant_id
                """,
                (plan_id, owner_id, experiment_id),
            ).fetchall()
            expected_count = int(plan["variant_count"])
            if len(enrollment_rows) != expected_count or [
                int(row["ordinal"]) for row in enrollment_rows
            ] != list(range(expected_count)):
                raise sqlite3.IntegrityError("Experiment plan physical Variant enrollment conflict")
            connection.execute(
                """
                INSERT INTO experiments(
                    experiment_id,owner_id,plan_id,workflow_id,server_id,
                    pinned_revision_id,pinned_deployment_id,pinned_content_digest,
                    status,failure_policy,concurrency,execution_slots,
                    submission_window,variant_count,pending_count,submitted_count,
                    running_count,completed_count,failed_count,cancelled_count,lost_count,
                    cancel_mode,created_at,updated_at,completed_at
                ) VALUES(?,?,?,?,?,?,?,?,'queued',?,?,?,?,?,?,0,0,0,0,0,0,NULL,?,?,NULL)
                """,
                (
                    experiment_id,
                    owner_id,
                    plan_id,
                    str(plan["workflow_id"]),
                    str(plan["server_id"]),
                    str(plan["pinned_revision_id"]),
                    str(plan["pinned_deployment_id"]),
                    str(plan["pinned_content_digest"]),
                    str(plan["failure_policy"]),
                    int(plan["concurrency"]),
                    int(plan["execution_slots"]),
                    int(plan["submission_window"]),
                    expected_count,
                    expected_count,
                    str(plan["created_at"]),
                    committed_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO experiment_capacity_reservations(
                    experiment_id,owner_id,server_id,execution_slots,reserved_at,released_at
                ) VALUES(?,?,?,?,?,NULL)
                """,
                (
                    experiment_id,
                    owner_id,
                    str(plan["server_id"]),
                    int(plan["execution_slots"]),
                    committed_at,
                ),
            )
            rows = [
                _variant_runtime_row(
                    row,
                    experiment_id=experiment_id,
                    owner_id=owner_id,
                )
                for row in enrollment_rows
            ]
            connection.executemany(
                """
                INSERT INTO experiment_variants(
                    variant_id,experiment_id,owner_id,ordinal,overrides_json,
                    parameter_digest,execution_input_digest,client_id,
                    idempotency_key,status,checkpoint_json,created_at,updated_at,
                    completed_at
                ) VALUES(?,?,?,?,?,?,NULL,?,?,?,'{}',?,?,NULL)
                """,
                rows,
            )
            if (
                int(
                    connection.execute(
                        "SELECT count(*) FROM experiment_variants WHERE experiment_id=? AND owner_id=?",
                        (experiment_id, owner_id),
                    ).fetchone()[0]
                )
                != expected_count
            ):
                raise sqlite3.IntegrityError("Experiment commit physical Variant count conflict")
            self._fault("variants")
            subject_uri = f"comfyui://experiments/{experiment_id}"
            work_item_id = _stable_id("work", experiment_id, "advance")
            payload = {"experiment_id": experiment_id, "owner_id": owner_id}
            connection.execute(
                """
                INSERT INTO operation_work_items(
                    work_item_id,subject_uri,work_type,payload_json,checkpoint_json,
                    status,next_attempt_at,created_at,updated_at
                ) VALUES(?,?, 'experiment.advance',?, '{}','pending',?,?,?)
                """,
                (
                    work_item_id,
                    subject_uri,
                    _json(payload),
                    str(plan["created_at"]),
                    str(plan["created_at"]),
                    committed_at,
                ),
            )
            self._append_event_and_outbox(
                connection,
                event_type="EXPERIMENT_COMMITTED",
                subject_uri=subject_uri,
                correlation_id=plan_id,
                principal_id=owner_id,
                data={"experiment_id": experiment_id, "status": "queued"},
                occurred_at=committed_at,
            )
            connection.execute(
                """
                UPDATE experiment_plans
                SET committed_at=?,committed_experiment_id=?
                WHERE plan_id=? AND owner_id=? AND committed_at IS NULL
                """,
                (committed_at, experiment_id, plan_id, owner_id),
            )
            self._fault("commit")
            result = self._get_experiment(connection, experiment_id, owner_id)
            if result is None:
                raise RuntimeError("Experiment commit did not create its aggregate")
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def cleanup_expired_plans(
        self, *, now: datetime, owner_id: str | None = None, limit: int = 1000
    ) -> dict[str, int]:
        if isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("Experiment plan retention limit must be between 1 and 1000")
        now_text = _time(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            parameters: list[object] = [now_text]
            owner_filter = ""
            if owner_id is not None:
                owner_filter = "AND owner_id=?"
                parameters.append(owner_id)
            parameters.append(limit)
            plan_ids = [
                str(row[0])
                for row in connection.execute(
                    f"""
                    SELECT plan_id FROM experiment_plans
                    WHERE committed_at IS NULL AND expires_at<=?
                      {owner_filter}
                    ORDER BY expires_at,plan_id LIMIT ?
                    """,
                    tuple(parameters),
                ).fetchall()
            ]
            for expired_plan_id in plan_ids:
                connection.execute(
                    "DELETE FROM experiment_plans WHERE plan_id=? AND committed_at IS NULL",
                    (expired_plan_id,),
                )
            connection.commit()
            return {"plans_deleted": len(plan_ids)}
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def apply_retention(
        self, *, now: datetime, owner_id: str | None = None, limit: int = 1000
    ) -> dict[str, int]:
        expired = self.cleanup_expired_plans(now=now, owner_id=owner_id, limit=limit)
        connection = self._connect()
        now_text = _time(now)
        try:
            connection.execute("BEGIN IMMEDIATE")
            plan_parameters: list[object] = [_time(now - _PROMOTION_GRACE)]
            plan_owner_filter = ""
            if owner_id is not None:
                plan_owner_filter = "AND plans.owner_id=?"
                plan_parameters.append(owner_id)
            plan_parameters.append(limit)
            plan_ids = [
                str(row[0])
                for row in connection.execute(
                    f"""
                    SELECT plans.plan_id
                    FROM experiment_plans AS plans
                    JOIN experiments
                      ON experiments.plan_id=plans.plan_id
                     AND experiments.owner_id=plans.owner_id
                     AND experiments.experiment_id=plans.experiment_id
                    WHERE plans.committed_at IS NOT NULL
                      AND plans.payload_pruned_at IS NULL
                      AND experiments.status IN ('completed','completed_with_errors','cancelled')
                      AND experiments.completed_at<=?
                      {plan_owner_filter}
                    ORDER BY experiments.completed_at,plans.plan_id LIMIT ?
                    """,
                    tuple(plan_parameters),
                ).fetchall()
            ]
            terminal_payloads_compacted = 0
            for terminal_plan_id in plan_ids:
                connection.execute(
                    """
                    UPDATE experiment_plans
                    SET expansion_json='{}',base_arguments_json='{}',budgets_json='{}',
                        budget_totals_json='{}',variants_json='[]',variant_overrides_json='{}',
                        retained_bytes=12,payload_pruned_at=?
                    WHERE plan_id=? AND committed_at IS NOT NULL AND payload_pruned_at IS NULL
                    """,
                    (now_text, terminal_plan_id),
                )
                connection.execute(
                    """
                    UPDATE experiment_plan_variants SET overrides_json='{}'
                    WHERE plan_id=? AND overrides_json!='{}'
                    """,
                    (terminal_plan_id,),
                )
                terminal_payloads_compacted += connection.execute(
                    """
                    UPDATE experiment_variants
                    SET overrides_json='{}',checkpoint_json='{}',payload_compacted_at=?
                    WHERE experiment_id=(
                        SELECT experiment_id FROM experiment_plans WHERE plan_id=?
                    ) AND status IN ('completed','failed','cancelled','lost')
                      AND overrides_json!='{}'
                    """,
                    (now_text, terminal_plan_id),
                ).rowcount
            variant_parameters: list[object] = [now_text]
            variant_owner_filter = ""
            if owner_id is not None:
                variant_owner_filter = "AND owner_id=?"
                variant_parameters.append(owner_id)
            variant_parameters.append(limit)
            variant_ids = [
                str(row[0])
                for row in connection.execute(
                    f"""
                    SELECT variant_id FROM experiment_variants
                    WHERE status IN ('completed','failed','cancelled','lost')
                      AND checkpoint_json!='{{}}' AND completed_at<=?
                      {variant_owner_filter}
                    ORDER BY completed_at,variant_id LIMIT ?
                    """,
                    tuple(variant_parameters),
                ).fetchall()
            ]
            for variant_id in variant_ids:
                connection.execute(
                    """
                    UPDATE experiment_variants
                    SET checkpoint_json='{}',payload_compacted_at=?
                    WHERE variant_id=? AND status IN ('completed','failed','cancelled','lost')
                    """,
                    (now_text, variant_id),
                )
            terminal_payloads_compacted += len(variant_ids)
            connection.commit()
            return {
                **expired,
                "terminal_plans_pruned": len(plan_ids),
                "terminal_payloads_compacted": terminal_payloads_compacted,
            }
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_experiment(self, experiment_id: str, owner_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            return self._get_experiment(connection, experiment_id, owner_id)
        finally:
            connection.close()

    def cancel_experiment(
        self, experiment_id: str, mode: str, owner_id: str
    ) -> dict[str, Any] | None:
        if mode not in {"stop_new", "cancel_queued"}:
            raise ValueError("Experiment cancel mode is invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status,cancel_mode,submitted_count,running_count FROM experiments WHERE experiment_id=? AND owner_id=?",
                (experiment_id, owner_id),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            status = str(row[0])
            current_mode = "" if row[1] is None else str(row[1])
            if current_mode == mode:
                result = self._get_experiment(connection, experiment_id, owner_id)
                connection.commit()
                return result
            if status in _TERMINAL_EXPERIMENT_STATUSES:
                raise ValueError("terminal Experiment cannot be cancelled")
            if current_mode == "cancel_queued" and mode == "stop_new":
                raise ValueError("Experiment cancel mode cannot regress")
            now_text = _time(datetime.now(timezone.utc))
            connection.execute(
                """
                UPDATE experiment_variants
                SET status='cancelled',completed_at=?,updated_at=?
                WHERE experiment_id=? AND owner_id=? AND status='pending'
                """,
                (now_text, now_text, experiment_id, owner_id),
            )
            counts = connection.execute(
                """
                SELECT submitted_count,running_count
                FROM experiments WHERE experiment_id=? AND owner_id=?
                """,
                (experiment_id, owner_id),
            ).fetchone()
            if counts is None:
                raise LookupError("Experiment was not found")
            if int(counts[0]) == 0 and int(counts[1]) == 0:
                connection.execute(
                    """
                    UPDATE experiments
                    SET status='cancelled',cancel_mode=?,updated_at=?,completed_at=?
                    WHERE experiment_id=? AND owner_id=?
                    """,
                    (mode, now_text, now_text, experiment_id, owner_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE experiments SET cancel_mode=?,updated_at=?
                    WHERE experiment_id=? AND owner_id=?
                    """,
                    (mode, now_text, experiment_id, owner_id),
                )
            subject_uri = f"comfyui://experiments/{experiment_id}"
            self._append_event_and_outbox(
                connection,
                event_type="EXPERIMENT_CANCEL_REQUESTED",
                subject_uri=subject_uri,
                correlation_id=_stable_id("cancel", experiment_id, mode),
                principal_id=owner_id,
                data={"experiment_id": experiment_id, "cancel_mode": mode},
                occurred_at=now_text,
            )
            result = self._get_experiment(connection, experiment_id, owner_id)
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_variants(
        self,
        experiment_id: str,
        owner_id: str,
        *,
        limit: int,
        after: tuple[str, str] | None,
    ) -> tuple[list[dict[str, Any]], bool]:
        if isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("Variant page limit must be between 1 and 200")
        parameters: list[object] = [experiment_id, owner_id]
        keyset = ""
        if after is not None:
            keyset = (
                "AND (variants.created_at>? OR (variants.created_at=? AND variants.variant_id>?))"
            )
            parameters.extend((after[0], after[0], after[1]))
        parameters.append(limit + 1)
        connection = self._connect()
        try:
            rows = connection.execute(
                f"""
                SELECT variants.variant_id,variants.experiment_id,variants.owner_id,
                       variants.ordinal,plans.base_arguments_json,variants.overrides_json,
                       variants.parameter_digest,variants.execution_input_digest,
                       plans.payload_pruned_at,variants.client_id,variants.idempotency_key,variants.status,
                       COALESCE(bindings.job_id,''),variants.created_at,
                       variants.updated_at,variants.completed_at,
                       variants.measured_pixels,variants.measured_outputs,
                       variants.measured_seconds,variants.error_code
                FROM experiment_variants AS variants
                JOIN experiments
                  ON experiments.experiment_id=variants.experiment_id
                 AND experiments.owner_id=variants.owner_id
                JOIN experiment_plans AS plans
                  ON plans.plan_id=experiments.plan_id AND plans.owner_id=experiments.owner_id
                LEFT JOIN experiment_variant_jobs AS bindings
                  ON bindings.experiment_id=variants.experiment_id
                 AND bindings.variant_id=variants.variant_id
                 AND bindings.owner_id=variants.owner_id
                WHERE variants.experiment_id=? AND variants.owner_id=? {keyset}
                ORDER BY variants.created_at,variants.variant_id LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
            has_more = len(rows) > limit
            variants = [self._variant_from_row(row) for row in rows[:limit]]
            self._hydrate_variant_projections(connection, variants)
            return variants, has_more
        finally:
            connection.close()

    def get_variant(
        self, experiment_id: str, variant_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT variants.variant_id,variants.experiment_id,variants.owner_id,
                       variants.ordinal,plans.base_arguments_json,variants.overrides_json,
                       variants.parameter_digest,variants.execution_input_digest,
                       plans.payload_pruned_at,variants.client_id,variants.idempotency_key,variants.status,
                       COALESCE(bindings.job_id,''),variants.created_at,
                       variants.updated_at,variants.completed_at,
                       variants.measured_pixels,variants.measured_outputs,
                       variants.measured_seconds,variants.error_code
                FROM experiment_variants AS variants
                JOIN experiments
                  ON experiments.experiment_id=variants.experiment_id
                 AND experiments.owner_id=variants.owner_id
                JOIN experiment_plans AS plans
                  ON plans.plan_id=experiments.plan_id AND plans.owner_id=experiments.owner_id
                LEFT JOIN experiment_variant_jobs AS bindings
                  ON bindings.experiment_id=variants.experiment_id
                 AND bindings.variant_id=variants.variant_id
                 AND bindings.owner_id=variants.owner_id
                WHERE variants.experiment_id=? AND variants.variant_id=?
                  AND variants.owner_id=?
                """,
                (experiment_id, variant_id, owner_id),
            ).fetchone()
            if row is None:
                return None
            variant = self._variant_from_row(row)
            self._hydrate_variant_projection(connection, variant)
            return variant
        finally:
            connection.close()

    def resource_owner_for_uri(self, uri: str) -> str | None:
        prefix = "comfyui://experiments/"
        if not isinstance(uri, str) or not uri.startswith(prefix):
            return None
        components = uri[len(prefix) :].split("/")
        connection = self._connect()
        try:
            if len(components) == 1 and components[0]:
                row = connection.execute(
                    "SELECT owner_id FROM experiments WHERE experiment_id=?",
                    (components[0],),
                ).fetchone()
            elif (
                len(components) == 3
                and components[0]
                and components[1] == "variants"
                and components[2]
            ):
                row = connection.execute(
                    "SELECT owner_id FROM experiment_variants WHERE experiment_id=? AND variant_id=?",
                    (components[0], components[2]),
                ).fetchone()
            else:
                return None
        finally:
            connection.close()
        return None if row is None else str(row[0])

    def list_for_advance(
        self, experiment_id: str, owner_id: str, *, limit: int
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("Experiment advance limit must be between 1 and 100")
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT variants.variant_id,variants.experiment_id,variants.owner_id,
                       variants.ordinal,plans.base_arguments_json,variants.overrides_json,
                       variants.parameter_digest,variants.execution_input_digest,
                       plans.payload_pruned_at,variants.client_id,variants.idempotency_key,variants.status,
                       COALESCE(bindings.job_id,''),variants.created_at,
                       variants.updated_at,variants.completed_at,
                       variants.measured_pixels,variants.measured_outputs,
                       variants.measured_seconds,variants.error_code
                FROM experiment_variants AS variants INDEXED BY ix_experiment_variants_worker
                JOIN experiments
                  ON experiments.experiment_id=variants.experiment_id
                 AND experiments.owner_id=variants.owner_id
                JOIN experiment_plans AS plans
                  ON plans.plan_id=experiments.plan_id AND plans.owner_id=experiments.owner_id
                LEFT JOIN experiment_variant_jobs AS bindings
                  ON bindings.experiment_id=variants.experiment_id
                 AND bindings.variant_id=variants.variant_id
                 AND bindings.owner_id=variants.owner_id
                WHERE variants.experiment_id=? AND variants.owner_id=?
                  AND variants.status IN ('pending','submitted','running')
                ORDER BY variants.ordinal,variants.variant_id LIMIT ?
                """,
                (experiment_id, owner_id, limit),
            ).fetchall()
        finally:
            connection.close()
        return [self._variant_from_row(row) for row in rows]

    def claim_variant_for_submission(
        self,
        lease: WorkLease,
        *,
        experiment_id: str,
        owner_id: str,
        variant_id: str,
        now: datetime,
    ) -> dict[str, Any] | None:
        now_text = _time(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._require_experiment_lease(connection, lease, experiment_id, owner_id, now_text)
            row = connection.execute(
                """
                SELECT variants.variant_id,variants.status,variants.client_id,variants.lease_work_item_id,
                       variants.lease_worker_id,variants.lease_token,
                       experiments.status AS experiment_status,experiments.cancel_mode,
                       experiments.workflow_id,experiments.server_id,experiments.plan_id,
                       experiments.pinned_revision_id,experiments.pinned_deployment_id,
                       experiments.pinned_content_digest,plans.base_arguments_json,
                       variants.overrides_json,variants.parameter_digest,
                       variants.execution_input_digest
                FROM experiment_variants AS variants
                JOIN experiments ON experiments.experiment_id=variants.experiment_id
                 AND experiments.owner_id=variants.owner_id
                JOIN experiment_plans AS plans
                  ON plans.plan_id=experiments.plan_id AND plans.owner_id=experiments.owner_id
                JOIN experiment_capacity_reservations AS reservations
                  ON reservations.experiment_id=experiments.experiment_id
                 AND reservations.owner_id=experiments.owner_id
                 AND reservations.server_id=experiments.server_id
                 AND reservations.released_at IS NULL
                WHERE variants.experiment_id=? AND variants.variant_id=?
                  AND variants.owner_id=?
                """,
                (experiment_id, variant_id, owner_id),
            ).fetchone()
            if row is None:
                raise LookupError("Experiment Variant was not found or has no active capacity")
            if (
                str(row["status"]) == "submitted"
                and row["lease_work_item_id"] == lease.work_item_id
                and row["lease_worker_id"] == lease.worker_id
                and int(row["lease_token"]) == lease.fencing_token
            ):
                connection.commit()
                return _submission_claim_projection(row, lease=lease, claimed_at=now_text)
            if str(row["status"]) != "pending":
                connection.commit()
                return None
            if (
                row["cancel_mode"] is not None
                or str(row["experiment_status"]) in _TERMINAL_EXPERIMENT_STATUSES
            ):
                connection.commit()
                return None
            arguments = _materialize_arguments(
                str(row["base_arguments_json"]),
                str(row["overrides_json"]),
                expected_execution_input_digest=None,
            )
            execution_input_digest = _execution_input_digest(arguments)
            capacity = connection.execute(
                """
                SELECT COALESCE(capacities.execution_slots,1),
                       COALESCE(capacities.subject_submission_quota,0),
                       SUM(variants.status='submitted'),SUM(variants.status='running')
                FROM experiments
                JOIN experiment_variants AS variants
                  ON variants.experiment_id=experiments.experiment_id
                 AND variants.owner_id=experiments.owner_id
                LEFT JOIN experiment_server_capacities AS capacities
                  ON capacities.server_id=experiments.server_id
                WHERE experiments.server_id=?
                  AND experiments.status NOT IN ('completed','completed_with_errors','cancelled')
                """,
                (row["server_id"],),
            ).fetchone()
            execution_slots = int(capacity[0])
            submission_quota = int(capacity[1])
            submitted = int(capacity[2] or 0)
            running = int(capacity[3] or 0)
            if submitted + running >= execution_slots + submission_quota:
                connection.commit()
                return None
            changed = connection.execute(
                """
                UPDATE experiment_variants
                SET status='submitted',execution_input_digest=?,lease_work_item_id=?,
                    lease_worker_id=?,lease_token=?,lease_expires_at=?,updated_at=?
                WHERE experiment_id=? AND variant_id=? AND owner_id=? AND status='pending'
                """,
                (
                    execution_input_digest,
                    lease.work_item_id,
                    lease.worker_id,
                    lease.fencing_token,
                    lease.expires_at,
                    now_text,
                    experiment_id,
                    variant_id,
                    owner_id,
                ),
            ).rowcount
            if changed != 1:
                connection.commit()
                return None
            claimed = connection.execute(
                """
                SELECT variants.variant_id,variants.status,variants.client_id,variants.lease_work_item_id,
                       variants.lease_worker_id,variants.lease_token,
                       experiments.status AS experiment_status,experiments.cancel_mode,
                       experiments.workflow_id,experiments.server_id,experiments.plan_id,
                       experiments.pinned_revision_id,experiments.pinned_deployment_id,
                       experiments.pinned_content_digest,plans.base_arguments_json,
                       variants.overrides_json,variants.parameter_digest,
                       variants.execution_input_digest
                FROM experiment_variants AS variants
                JOIN experiments ON experiments.experiment_id=variants.experiment_id
                 AND experiments.owner_id=variants.owner_id
                JOIN experiment_plans AS plans
                  ON plans.plan_id=experiments.plan_id AND plans.owner_id=experiments.owner_id
                WHERE variants.experiment_id=? AND variants.variant_id=?
                  AND variants.owner_id=?
                """,
                (experiment_id, variant_id, owner_id),
            ).fetchone()
            if claimed is None:
                raise RuntimeError("claimed Experiment Variant disappeared")
            connection.commit()
            return _submission_claim_projection(claimed, lease=lease, claimed_at=now_text)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def apply_transition(
        self,
        lease: WorkLease,
        *,
        experiment_id: str,
        owner_id: str,
        variant_id: str,
        status: str,
        job_id: str,
        checkpoint: dict[str, Any],
        now: datetime,
        event_type: str,
        event_data: dict[str, Any],
    ) -> None:
        if status not in {
            "pending",
            "submitted",
            "running",
            "completed",
            "failed",
            "cancelled",
            "lost",
        }:
            raise ValueError("Experiment Variant status is invalid")
        now_text = _time(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._require_experiment_lease(connection, lease, experiment_id, owner_id, now_text)
            row = connection.execute(
                """
                SELECT variants.status,bindings.job_id,variants.measured_pixels,
                       variants.measured_outputs,variants.measured_seconds,variants.error_code,
                       variants.lease_worker_id,variants.lease_token,variants.lease_expires_at,
                       variants.lease_work_item_id,experiments.workflow_id,experiments.server_id,
                       experiments.plan_id,experiments.pinned_revision_id,
                       experiments.pinned_deployment_id,experiments.pinned_content_digest,
                       plans.budgets_json
                FROM experiment_variants AS variants
                JOIN experiments ON experiments.experiment_id=variants.experiment_id
                 AND experiments.owner_id=variants.owner_id
                JOIN experiment_plans AS plans ON plans.plan_id=experiments.plan_id
                LEFT JOIN experiment_variant_jobs AS bindings
                  ON bindings.experiment_id=variants.experiment_id
                 AND bindings.variant_id=variants.variant_id
                 AND bindings.owner_id=variants.owner_id
                WHERE variants.experiment_id=? AND variants.variant_id=?
                  AND variants.owner_id=?
                """,
                (experiment_id, variant_id, owner_id),
            ).fetchone()
            if row is None:
                raise LookupError("Experiment Variant was not found")
            previous_status = str(row["status"])
            previous_job_id = "" if row["job_id"] is None else str(row["job_id"])
            if previous_job_id and job_id and previous_job_id != job_id:
                raise RuntimeError("Experiment Variant Job identity conflict")
            if job_id and not previous_job_id:
                job = connection.execute(
                    """
                    SELECT job_id,owner_id,workflow_id,server_id,plan_id,revision_id,deployment_id
                    FROM jobs WHERE job_id=? AND owner_id=?
                    """,
                    (job_id, owner_id),
                ).fetchone()
                if job is None:
                    raise LookupError("Experiment Job was not found for Variant binding")
                attempt = connection.execute(
                    """
                    SELECT attempt_id FROM execution_attempts
                    WHERE job_id=? AND server_id=? ORDER BY attempt DESC LIMIT 1
                    """,
                    (job_id, row["server_id"]),
                ).fetchone()
                if attempt is None:
                    raise LookupError("Experiment Job has no canonical execution Attempt")
                connection.execute(
                    """
                    INSERT INTO experiment_variant_jobs(
                        experiment_id,variant_id,owner_id,job_id,workflow_id,server_id,
                        plan_id,revision_id,deployment_id,attempt_id,linked_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        experiment_id,
                        variant_id,
                        owner_id,
                        job_id,
                        row["workflow_id"],
                        row["server_id"],
                        job["plan_id"],
                        row["pinned_revision_id"],
                        row["pinned_deployment_id"],
                        attempt["attempt_id"],
                        now_text,
                    ),
                )
            measurements: dict[str, Any] = {}
            for field, minimum in (
                ("measured_pixels", 0),
                ("measured_outputs", 0),
                ("measured_seconds", 0.0),
            ):
                value = event_data.get(field, row[field])
                if value is not None:
                    number = float(value)
                    if (
                        number < minimum
                        or number != number
                        or number in (float("inf"), float("-inf"))
                    ):
                        raise ValueError(f"{field} measurement is invalid")
                    if field != "measured_seconds" and int(number) != number:
                        raise ValueError(f"{field} measurement is invalid")
                    measurements[field] = int(number) if field != "measured_seconds" else number
            aggregate = connection.execute(
                """
                SELECT COALESCE(SUM(measured_pixels),0),
                       COALESCE(SUM(measured_outputs),0),
                       COALESCE(SUM(measured_seconds),0)
                FROM experiment_variants
                WHERE experiment_id=? AND owner_id=? AND variant_id!=?
                """,
                (experiment_id, owner_id, variant_id),
            ).fetchone()
            budgets = _json_object(str(row["budgets_json"]), field="Experiment budgets")
            measured_totals = {
                "max_pixels": int(aggregate[0]) + int(measurements.get("measured_pixels", 0)),
                "max_outputs": int(aggregate[1]) + int(measurements.get("measured_outputs", 0)),
                "max_seconds": float(aggregate[2])
                + float(measurements.get("measured_seconds", 0.0)),
            }
            if any(measured_totals[key] > float(budgets[key]) for key in measured_totals):
                checkpoint["pause_reason"] = "MEASURED_BUDGET_EXCEEDED"
            error_code = str(event_data.get("error_code", "")) or None
            changed = (
                previous_status != status
                or (bool(job_id) and not previous_job_id)
                or any(measurements.get(field) != row[field] for field in measurements)
                or error_code != row["error_code"]
            )
            if previous_status != status or changed:
                completed_at = now_text if status in _TERMINAL_VARIANT_STATUSES else None
                clear_lease = status in _TERMINAL_VARIANT_STATUSES
                connection.execute(
                    """
                    UPDATE experiment_variants
                    SET status=?,checkpoint_json=?,error_code=?,
                        measured_pixels=?,measured_outputs=?,measured_seconds=?,
                        lease_work_item_id=CASE WHEN ? THEN NULL ELSE lease_work_item_id END,
                        lease_worker_id=CASE WHEN ? THEN NULL ELSE lease_worker_id END,
                        lease_token=CASE WHEN ? THEN 0 ELSE lease_token END,
                        lease_expires_at=CASE WHEN ? THEN NULL ELSE lease_expires_at END,
                        updated_at=?,completed_at=?
                    WHERE experiment_id=? AND variant_id=? AND owner_id=?
                    """,
                    (
                        status,
                        _json(checkpoint),
                        error_code,
                        measurements.get("measured_pixels"),
                        measurements.get("measured_outputs"),
                        measurements.get("measured_seconds"),
                        int(clear_lease),
                        int(clear_lease),
                        int(clear_lease),
                        int(clear_lease),
                        now_text,
                        completed_at,
                        experiment_id,
                        variant_id,
                        owner_id,
                    ),
                )
            connection.execute(
                "UPDATE operation_work_items SET checkpoint_json=?,updated_at=? WHERE work_item_id=?",
                (_json(checkpoint), now_text, lease.work_item_id),
            )
            if changed:
                self._append_event_and_outbox(
                    connection,
                    event_type="EXPERIMENT_UPDATED",
                    subject_uri=f"comfyui://experiments/{experiment_id}",
                    correlation_id=lease.work_item_id,
                    principal_id=owner_id,
                    data={
                        "experiment_id": experiment_id,
                        "variant_id": variant_id,
                        "status": status,
                        **measurements,
                    },
                    occurred_at=now_text,
                )
            if changed and event_type:
                self._append_event_and_outbox(
                    connection,
                    event_type=event_type,
                    subject_uri=f"comfyui://experiments/{experiment_id}/variants/{variant_id}",
                    correlation_id=lease.work_item_id,
                    principal_id=owner_id,
                    data={**event_data, "experiment_id": experiment_id, "variant_id": variant_id},
                    occurred_at=now_text,
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def finish_advance(
        self,
        lease: WorkLease,
        *,
        experiment_id: str,
        owner_id: str,
        checkpoint: dict[str, Any],
        now: datetime,
        completed: bool,
        delay_seconds: int,
        status: str,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("Experiment advance delay must not be negative")
        if completed != (status in _TERMINAL_EXPERIMENT_STATUSES):
            raise ValueError("Experiment completion and status conflict")
        now_text = _time(now)
        next_attempt = _time(now + timedelta(seconds=delay_seconds))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._require_experiment_lease(connection, lease, experiment_id, owner_id, now_text)
            row = connection.execute(
                "SELECT status FROM experiments WHERE experiment_id=? AND owner_id=?",
                (experiment_id, owner_id),
            ).fetchone()
            if row is None:
                raise LookupError("Experiment was not found")
            previous_status = str(row[0])
            connection.execute(
                """
                UPDATE experiments SET status=?,updated_at=?,completed_at=?
                WHERE experiment_id=? AND owner_id=?
                """,
                (
                    status,
                    now_text,
                    now_text if completed else None,
                    experiment_id,
                    owner_id,
                ),
            )
            connection.execute(
                """
                UPDATE operation_work_items
                SET checkpoint_json=?,status=?,next_attempt_at=?,updated_at=?
                WHERE work_item_id=?
                """,
                (
                    _json(checkpoint),
                    "completed" if completed else "pending",
                    next_attempt,
                    now_text,
                    lease.work_item_id,
                ),
            )
            connection.execute(
                "UPDATE work_leases SET expires_at=? WHERE work_item_id=?",
                (now_text, lease.work_item_id),
            )
            if previous_status != status:
                self._append_event_and_outbox(
                    connection,
                    event_type="EXPERIMENT_UPDATED",
                    subject_uri=f"comfyui://experiments/{experiment_id}",
                    correlation_id=lease.work_item_id,
                    principal_id=owner_id,
                    data={"experiment_id": experiment_id, "status": status},
                    occurred_at=now_text,
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save_rating(self, rating: dict[str, Any]) -> dict[str, Any]:
        required = (
            "rating_id",
            "owner_id",
            "experiment_id",
            "variant_id",
            "rubric_version",
            "scores",
            "created_at",
        )
        if any(key not in rating for key in required):
            raise ValueError("Experiment rating fields are incomplete")
        owner_id = str(rating["owner_id"])
        experiment_id = str(rating["experiment_id"])
        variant_id = str(rating["variant_id"])
        rubric_version = str(rating["rubric_version"])
        supplied_definition = "rubric_definition" in rating
        scores = _json_copy(rating["scores"])
        raw_definition = rating.get("rubric_definition")
        if raw_definition is None and rubric_version == "v1":
            raw_definition = {
                "version": "v1",
                "dimensions": {
                    name: {"minimum": 0.0, "maximum": 5.0}
                    for name in ("quality", "prompt_adherence", "technical_quality")
                },
            }
        definition = _json_copy(raw_definition)
        if not isinstance(scores, dict) or not isinstance(definition, dict):
            raise ValueError("Experiment rating scores and rubric definition must be objects")
        dimensions = definition.get("dimensions")
        if definition.get("version") != rubric_version or not isinstance(dimensions, dict):
            raise ValueError("Experiment rubric definition version is invalid")
        if not dimensions or len(dimensions) > 32 or set(scores) != set(dimensions):
            raise ValueError("Experiment rating dimensions do not match its rubric definition")
        normalized_dimensions: dict[str, dict[str, float]] = {}
        for name in sorted(dimensions):
            bounds = dimensions[name]
            score = scores[name]
            if not isinstance(name, str) or not name or not isinstance(bounds, dict):
                raise ValueError("Experiment rubric dimension is invalid")
            minimum = bounds.get("minimum")
            maximum = bounds.get("maximum")
            if (
                isinstance(minimum, bool)
                or not isinstance(minimum, (int, float))
                or isinstance(maximum, bool)
                or not isinstance(maximum, (int, float))
                or float(maximum) < float(minimum)
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not float(minimum) <= float(score) <= float(maximum)
            ):
                raise ValueError("Experiment rubric dimension bounds or score are invalid")
            normalized_dimensions[name] = {
                "minimum": float(minimum),
                "maximum": float(maximum),
            }
            scores[name] = float(score)
        definition = {"version": rubric_version, "dimensions": normalized_dimensions}
        definition_json = _json(definition)
        definition_digest = hashlib.sha256(definition_json.encode("utf-8")).hexdigest()
        scores_json = _json(scores)
        rating_digest = hashlib.sha256(
            _json(
                {
                    "rubric_definition_digest": definition_digest,
                    "scores": scores,
                }
            ).encode("utf-8")
        ).hexdigest()
        created_at = str(rating["created_at"])
        mutated_at = _time(datetime.now(timezone.utc))
        rating_id = str(rating["rating_id"])
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            variant = connection.execute(
                "SELECT 1 FROM experiment_variants WHERE experiment_id=? AND variant_id=? AND owner_id=?",
                (experiment_id, variant_id, owner_id),
            ).fetchone()
            if variant is None:
                raise LookupError("Experiment Variant was not found")
            existing_definition = connection.execute(
                """
                SELECT definition_json,definition_digest
                FROM experiment_rubric_versions
                WHERE owner_id=? AND rubric_version=?
                """,
                (owner_id, rubric_version),
            ).fetchone()
            if existing_definition is None:
                connection.execute(
                    """
                    INSERT INTO experiment_rubric_versions(
                        owner_id,rubric_version,created_at,definition_json,definition_digest
                    ) VALUES(?,?,?,?,?)
                    """,
                    (owner_id, rubric_version, mutated_at, definition_json, definition_digest),
                )
                for ordinal, name in enumerate(normalized_dimensions):
                    bounds = normalized_dimensions[name]
                    connection.execute(
                        """
                        INSERT INTO experiment_rubric_dimensions(
                            owner_id,rubric_version,dimension,minimum,maximum,ordinal
                        ) VALUES(?,?,?,?,?,?)
                        """,
                        (
                            owner_id,
                            rubric_version,
                            name,
                            bounds["minimum"],
                            bounds["maximum"],
                            ordinal,
                        ),
                    )
            elif tuple(existing_definition) != (definition_json, definition_digest):
                raise ValueError("Experiment rubric version conflicts with immutable definition")
            existing = connection.execute(
                """
                SELECT owner_id,experiment_id,variant_id,rubric_version,
                       rubric_definition_digest,scores_json,rating_digest,created_at,updated_at
                FROM experiment_ratings WHERE rating_id=?
                """,
                (rating_id,),
            ).fetchone()
            changed = existing is None or str(existing[6]) != rating_digest
            if existing is not None:
                identity = tuple(existing[:5])
                expected = (
                    owner_id,
                    experiment_id,
                    variant_id,
                    rubric_version,
                    definition_digest,
                )
                if identity != expected:
                    raise ValueError("Experiment rating identity conflict")
                if changed:
                    connection.execute(
                        """
                        UPDATE experiment_ratings
                        SET scores_json=?,rating_digest=?,updated_at=? WHERE rating_id=?
                        """,
                        (scores_json, rating_digest, mutated_at, rating_id),
                    )
                else:
                    created_at = str(existing[7])
                    mutated_at = str(existing[8])
            else:
                connection.execute(
                    """
                    INSERT INTO experiment_ratings(
                        rating_id,owner_id,experiment_id,variant_id,rubric_version,
                        scores_json,rubric_definition_digest,rating_digest,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        rating_id,
                        owner_id,
                        experiment_id,
                        variant_id,
                        rubric_version,
                        scores_json,
                        definition_digest,
                        rating_digest,
                        created_at,
                        mutated_at,
                    ),
                )
            if changed:
                event_data = {
                    "experiment_id": experiment_id,
                    "variant_id": variant_id,
                    "rating_id": rating_id,
                    "rubric_version": rubric_version,
                }
                self._append_event_and_outbox(
                    connection,
                    event_type="EXPERIMENT_VARIANT_RATED",
                    subject_uri=f"comfyui://experiments/{experiment_id}/variants/{variant_id}",
                    correlation_id=rating_id,
                    principal_id=owner_id,
                    data=event_data,
                    occurred_at=mutated_at,
                )
                self._append_event_and_outbox(
                    connection,
                    event_type="EXPERIMENT_UPDATED",
                    subject_uri=f"comfyui://experiments/{experiment_id}",
                    correlation_id=rating_id,
                    principal_id=owner_id,
                    data=event_data,
                    occurred_at=mutated_at,
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        if not supplied_definition:
            return _json_copy(rating)
        return {
            "rating_id": rating_id,
            "owner_id": owner_id,
            "experiment_id": experiment_id,
            "variant_id": variant_id,
            "rubric_version": rubric_version,
            "rubric_definition": definition,
            "scores": scores,
            "created_at": created_at,
            "updated_at": mutated_at,
        }

    def promote_variant(
        self, experiment_id: str, variant_id: str, target: str, owner_id: str
    ) -> dict[str, Any]:
        if target not in {"preset", "revision"}:
            raise ValueError("promotion target is invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT variants.status,plans.base_arguments_json,variants.overrides_json,
                       variants.parameter_digest,variants.execution_input_digest,
                       plans.payload_pruned_at,experiments.workflow_id,experiments.server_id,
                       experiments.pinned_revision_id,experiments.pinned_content_digest
                FROM experiment_variants AS variants
                JOIN experiments ON experiments.experiment_id=variants.experiment_id
                 AND experiments.owner_id=variants.owner_id
                JOIN experiment_plans AS plans
                  ON plans.plan_id=experiments.plan_id AND plans.owner_id=experiments.owner_id
                WHERE variants.experiment_id=? AND variants.variant_id=? AND variants.owner_id=?
                """,
                (experiment_id, variant_id, owner_id),
            ).fetchone()
            if row is None:
                raise LookupError("Experiment Variant was not found")
            if str(row[0]) != "completed":
                raise ValueError("only completed Experiment Variants can be promoted")
            existing = connection.execute(
                """
                SELECT promotion_id,preset_id,revision_id,created_at
                FROM experiment_promotions
                WHERE owner_id=? AND experiment_id=? AND variant_id=? AND target=?
                """,
                (owner_id, experiment_id, variant_id, target),
            ).fetchone()
            if existing is not None:
                result: dict[str, Any] = {
                    "promotion_id": str(existing[0]),
                    "experiment_id": experiment_id,
                    "variant_id": variant_id,
                    "target": target,
                    "created_at": str(existing[3]),
                }
                if target == "preset":
                    result["preset_id"] = str(existing[1])
                else:
                    result["revision_id"] = str(existing[2])
                    result["published"] = False
                connection.commit()
                return result
            if row[5] is not None:
                raise ValueError("Experiment Variant payload was compacted by retention")
            arguments = _materialize_arguments(
                str(row[1]),
                str(row[2]),
                expected_execution_input_digest=(None if row[4] is None else str(row[4])),
            )
            arguments_json = _json(arguments)
            workflow_id = str(row[6])
            server_id = str(row[7])
            source_revision_id = str(row[8])
            source_content_digest = str(row[9])
            created_at = _time(datetime.now(timezone.utc))
            preset_id: str | None = None
            revision_id: str | None = None
            if target == "preset":
                preset_digest = hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()
                preset = connection.execute(
                    """
                    SELECT preset_id,arguments_json FROM experiment_presets
                    WHERE owner_id=? AND workflow_id=? AND server_id=? AND content_digest=?
                    """,
                    (owner_id, workflow_id, server_id, preset_digest),
                ).fetchone()
                if preset is not None:
                    if str(preset[1]) != arguments_json:
                        raise RuntimeError("Experiment preset digest conflicts with stored payload")
                    preset_id = str(preset[0])
                else:
                    preset_id = _stable_id(
                        "preset", owner_id, workflow_id, server_id, preset_digest
                    )
                    connection.execute(
                        """
                        INSERT INTO experiment_presets(
                            preset_id,owner_id,workflow_id,server_id,experiment_id,
                            variant_id,arguments_json,content_digest,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            preset_id,
                            owner_id,
                            workflow_id,
                            server_id,
                            experiment_id,
                            variant_id,
                            arguments_json,
                            preset_digest,
                            created_at,
                        ),
                    )
            else:
                source = connection.execute(
                    """
                    SELECT graph_json,parameter_schema_json,dependency_contract_json
                    FROM workflow_revisions
                    WHERE revision_id=? AND workflow_id=? AND content_digest=?
                    """,
                    (source_revision_id, workflow_id, source_content_digest),
                ).fetchone()
                if source is None:
                    raise RuntimeError("Pinned Workflow Revision is unavailable")
                graph = _json_object(str(source[0]), field="pinned Workflow graph")
                source_schema = _json_object(
                    str(source[1]), field="pinned Workflow parameter schema"
                )
                dependencies = _json_object(str(source[2]), field="pinned Workflow dependencies")
                parameters = normalize_parameters(source_schema)
                validate_arguments(parameters, arguments)
                for name in sorted(arguments):
                    parameters[name] = dict(parameters[name])
                    parameters[name]["default"] = _json_copy(arguments[name])
                promoted_schema = _json_copy(source_schema)
                promoted_schema["parameters"] = parameters
                graph_json = _json(graph)
                schema_json = _json(promoted_schema)
                dependencies_json = _json(dependencies)
                revision_digest = _revision_digest(graph, promoted_schema, dependencies)
                matching = connection.execute(
                    """
                    SELECT revision_id,graph_json,parameter_schema_json,dependency_contract_json
                    FROM workflow_revisions
                    WHERE workflow_id=? AND content_digest=?
                    ORDER BY revision_id LIMIT 2
                    """,
                    (workflow_id, revision_digest),
                ).fetchall()
                expected_payload = (graph_json, schema_json, dependencies_json)
                if matching:
                    if len(matching) != 1 or tuple(matching[0][1:]) != expected_payload:
                        raise RuntimeError("Workflow Revision digest conflicts with stored payload")
                    revision_id = str(matching[0][0])
                else:
                    revision_id = derived_control_plane_id(
                        "revision", "workflow-import-v1", [workflow_id, revision_digest]
                    )
                    identity_row = connection.execute(
                        """
                        SELECT graph_json,parameter_schema_json,dependency_contract_json,content_digest
                        FROM workflow_revisions WHERE revision_id=?
                        """,
                        (revision_id,),
                    ).fetchone()
                    if identity_row is None:
                        connection.execute(
                            """
                            INSERT INTO workflow_revisions(
                                revision_id,workflow_id,graph_json,parameter_schema_json,
                                dependency_contract_json,content_digest,created_at
                            ) VALUES(?,?,?,?,?,?,?)
                            """,
                            (
                                revision_id,
                                workflow_id,
                                graph_json,
                                schema_json,
                                dependencies_json,
                                revision_digest,
                                created_at,
                            ),
                        )
                    elif tuple(identity_row) != (*expected_payload, revision_digest):
                        raise RuntimeError(
                            "Workflow Revision identity conflicts with stored payload"
                        )
            promotion_id = _stable_id("promotion", owner_id, experiment_id, variant_id, target)
            promotion_digest = hashlib.sha256(
                _json({"target": target, "target_id": preset_id or revision_id}).encode("utf-8")
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO experiment_promotions(
                    promotion_id,owner_id,experiment_id,variant_id,workflow_id,
                    target,preset_id,revision_id,promotion_digest,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    promotion_id,
                    owner_id,
                    experiment_id,
                    variant_id,
                    workflow_id,
                    target,
                    preset_id,
                    revision_id,
                    promotion_digest,
                    created_at,
                ),
            )
            event_data = {
                "experiment_id": experiment_id,
                "variant_id": variant_id,
                "target": target,
            }
            self._append_event_and_outbox(
                connection,
                event_type="EXPERIMENT_VARIANT_PROMOTED",
                subject_uri=f"comfyui://experiments/{experiment_id}/variants/{variant_id}",
                correlation_id=promotion_id,
                principal_id=owner_id,
                data=event_data,
                occurred_at=created_at,
            )
            self._append_event_and_outbox(
                connection,
                event_type="EXPERIMENT_UPDATED",
                subject_uri=f"comfyui://experiments/{experiment_id}",
                correlation_id=promotion_id,
                principal_id=owner_id,
                data=event_data,
                occurred_at=created_at,
            )
            result = {
                "promotion_id": promotion_id,
                "experiment_id": experiment_id,
                "variant_id": variant_id,
                "target": target,
                "created_at": created_at,
            }
            if target == "preset":
                result["preset_id"] = preset_id
            else:
                result["revision_id"] = revision_id
                result["published"] = False
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_preset(self, preset_id: str, owner_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT preset_id,workflow_id,server_id,experiment_id,variant_id,
                       arguments_json,content_digest,created_at
                FROM experiment_presets WHERE preset_id=? AND owner_id=?
                """,
                (preset_id, owner_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return {
            "preset_id": str(row[0]),
            "workflow_id": str(row[1]),
            "server_id": str(row[2]),
            "experiment_id": str(row[3]),
            "variant_id": str(row[4]),
            "arguments": _json_object(str(row[5]), field="Experiment preset arguments"),
            "content_digest": str(row[6]),
            "created_at": str(row[7]),
            "resource_uri": f"comfyui://presets/{str(row[0])}",
        }

    def consume_preset(
        self, preset_id: str, owner_id: str, workflow_id: str, server_id: str
    ) -> dict[str, Any]:
        preset = self.get_preset(preset_id, owner_id)
        if preset is None:
            raise LookupError("Experiment Preset was not found")
        if preset["workflow_id"] != workflow_id or preset["server_id"] != server_id:
            raise ValueError("Experiment Preset does not match Workflow deployment")
        return _json_copy(preset["arguments"])

    @staticmethod
    def _require_experiment_lease(
        connection: sqlite3.Connection,
        lease: WorkLease,
        experiment_id: str,
        owner_id: str,
        now_text: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT work.subject_uri,work.payload_json
            FROM work_leases AS leases
            JOIN operation_work_items AS work USING(work_item_id)
            WHERE leases.work_item_id=? AND leases.worker_id=?
              AND leases.fencing_token=?
              AND julianday(leases.expires_at)>julianday(?)
              AND work.work_type='experiment.advance' AND work.status='running'
            """,
            (lease.work_item_id, lease.worker_id, lease.fencing_token, now_text),
        ).fetchone()
        expected_uri = f"comfyui://experiments/{experiment_id}"
        if row is None or str(row[0]) != expected_uri:
            raise RuntimeError("work lease is expired or fenced")
        payload = _json_object(str(row[1]), field="Experiment work payload")
        if payload != {"experiment_id": experiment_id, "owner_id": owner_id}:
            raise RuntimeError("Experiment work lease ownership conflict")

    @staticmethod
    def _variant_from_row(row: sqlite3.Row) -> dict[str, Any]:
        arguments = (
            {}
            if row[8] is not None
            else _materialize_arguments(
                str(row[4]),
                str(row[5]),
                expected_execution_input_digest=(None if row[7] is None else str(row[7])),
            )
        )
        result = {
            "variant_id": str(row[0]),
            "experiment_id": str(row[1]),
            "owner_id": str(row[2]),
            "ordinal": int(row[3]),
            "arguments": arguments,
            "parameter_digest": str(row[6]),
            "client_id": str(row[9]),
            "idempotency_key": str(row[10]),
            "status": str(row[11]),
            "job_id": str(row[12]),
            "created_at": str(row[13]),
            "updated_at": str(row[14]),
            "completed_at": "" if row[15] is None else str(row[15]),
            "measured_pixels": row[16],
            "measured_outputs": row[17],
            "measured_seconds": row[18],
            "error_code": "" if row[19] is None else str(row[19]),
        }
        result["resource_uri"] = (
            f"comfyui://experiments/{result['experiment_id']}/variants/{result['variant_id']}"
        )
        return result

    @staticmethod
    def _hydrate_variant_projection(
        connection: sqlite3.Connection, variant: dict[str, Any]
    ) -> None:
        owner_id = str(variant["owner_id"])
        experiment_id = str(variant["experiment_id"])
        variant_id = str(variant["variant_id"])
        job_id = str(variant.get("job_id", ""))
        if job_id:
            variant["job_uri"] = f"comfyui://jobs/{job_id}"
            artifact_rows = connection.execute(
                """
                SELECT artifacts.artifact_id
                FROM artifacts
                JOIN jobs ON jobs.job_id=artifacts.job_id
                WHERE artifacts.job_id=? AND jobs.owner_id=?
                ORDER BY artifacts.created_at,artifacts.artifact_id LIMIT 101
                """,
                (job_id, owner_id),
            ).fetchall()
            if len(artifact_rows) > 100:
                raise RuntimeError("Experiment Variant has more than 100 Artifact results")
            variant["artifact_uris"] = [
                f"comfyui://artifacts/{str(row[0])}" for row in artifact_rows
            ]
        else:
            variant["artifact_uris"] = []
        rating_rows = connection.execute(
            """
            SELECT ratings.rating_id,ratings.rubric_version,versions.definition_json,
                   ratings.scores_json,ratings.created_at,ratings.updated_at
            FROM experiment_ratings AS ratings
            JOIN experiment_rubric_versions AS versions
              ON versions.owner_id=ratings.owner_id
             AND versions.rubric_version=ratings.rubric_version
             AND versions.definition_digest=ratings.rubric_definition_digest
            WHERE ratings.owner_id=? AND ratings.experiment_id=? AND ratings.variant_id=?
            ORDER BY ratings.created_at,ratings.rating_id LIMIT 33
            """,
            (owner_id, experiment_id, variant_id),
        ).fetchall()
        if len(rating_rows) > 32:
            raise RuntimeError("Experiment Variant has more than 32 Ratings")
        variant["ratings"] = [
            {
                "rating_id": str(row[0]),
                "experiment_id": experiment_id,
                "variant_id": variant_id,
                "rubric_version": str(row[1]),
                "rubric_definition": _json_object(
                    str(row[2]), field="Experiment rubric definition"
                ),
                "scores": _json_object(str(row[3]), field="Experiment rating scores"),
                "created_at": str(row[4]),
                "updated_at": str(row[5]),
            }
            for row in rating_rows
        ]
        promotion_rows = connection.execute(
            """
            SELECT promotion_id,target,preset_id,revision_id,created_at
            FROM experiment_promotions
            WHERE owner_id=? AND experiment_id=? AND variant_id=?
            ORDER BY target LIMIT 3
            """,
            (owner_id, experiment_id, variant_id),
        ).fetchall()
        if len(promotion_rows) > 2:
            raise RuntimeError("Experiment Variant has more than two Promotions")
        promotions: list[dict[str, Any]] = []
        for row in promotion_rows:
            target = str(row[1])
            promotion: dict[str, Any] = {
                "promotion_id": str(row[0]),
                "experiment_id": experiment_id,
                "variant_id": variant_id,
                "target": target,
                "created_at": str(row[4]),
            }
            if target == "preset":
                promotion["preset_id"] = str(row[2])
            else:
                promotion["revision_id"] = str(row[3])
                promotion["published"] = False
            promotions.append(promotion)
        variant["promotions"] = promotions

    @staticmethod
    def _hydrate_variant_projections(
        connection: sqlite3.Connection, variants: list[dict[str, Any]]
    ) -> None:
        if not variants:
            return
        by_variant_id = {str(variant["variant_id"]): variant for variant in variants}
        owner_id = str(variants[0]["owner_id"])
        experiment_id = str(variants[0]["experiment_id"])
        for variant in variants:
            job_id = str(variant.get("job_id", ""))
            variant["artifact_uris"] = []
            variant["ratings"] = []
            variant["promotions"] = []
            if job_id:
                variant["job_uri"] = f"comfyui://jobs/{job_id}"

        by_job_id = {
            str(variant["job_id"]): variant
            for variant in variants
            if str(variant.get("job_id", ""))
        }
        if by_job_id:
            placeholders = ",".join("?" for _ in by_job_id)
            artifact_rows = connection.execute(
                f"""
                SELECT artifacts.job_id,artifacts.artifact_id
                FROM artifacts JOIN jobs ON jobs.job_id=artifacts.job_id
                WHERE jobs.owner_id=? AND artifacts.job_id IN ({placeholders})
                ORDER BY artifacts.job_id,artifacts.created_at,artifacts.artifact_id
                """,
                (owner_id, *by_job_id),
            ).fetchall()
            for row in artifact_rows:
                artifact_uris = by_job_id[str(row[0])]["artifact_uris"]
                if len(artifact_uris) >= 100:
                    raise RuntimeError("Experiment Variant has more than 100 Artifact results")
                artifact_uris.append(f"comfyui://artifacts/{str(row[1])}")

        placeholders = ",".join("?" for _ in by_variant_id)
        rating_rows = connection.execute(
            f"""
            SELECT ratings.variant_id,ratings.rating_id,ratings.rubric_version,
                   versions.definition_json,ratings.scores_json,
                   ratings.created_at,ratings.updated_at
            FROM experiment_ratings AS ratings
            JOIN experiment_rubric_versions AS versions
              ON versions.owner_id=ratings.owner_id
             AND versions.rubric_version=ratings.rubric_version
             AND versions.definition_digest=ratings.rubric_definition_digest
            WHERE ratings.owner_id=? AND ratings.experiment_id=?
              AND ratings.variant_id IN ({placeholders})
            ORDER BY ratings.variant_id,ratings.created_at,ratings.rating_id
            """,
            (owner_id, experiment_id, *by_variant_id),
        ).fetchall()
        for row in rating_rows:
            ratings = by_variant_id[str(row[0])]["ratings"]
            if len(ratings) >= 32:
                raise RuntimeError("Experiment Variant has more than 32 Ratings")
            ratings.append(
                {
                    "rating_id": str(row[1]),
                    "experiment_id": experiment_id,
                    "variant_id": str(row[0]),
                    "rubric_version": str(row[2]),
                    "rubric_definition": _json_object(
                        str(row[3]), field="Experiment rubric definition"
                    ),
                    "scores": _json_object(str(row[4]), field="Experiment rating scores"),
                    "created_at": str(row[5]),
                    "updated_at": str(row[6]),
                }
            )

        promotion_rows = connection.execute(
            f"""
            SELECT variant_id,promotion_id,target,preset_id,revision_id,created_at
            FROM experiment_promotions
            WHERE owner_id=? AND experiment_id=? AND variant_id IN ({placeholders})
            ORDER BY variant_id,target
            """,
            (owner_id, experiment_id, *by_variant_id),
        ).fetchall()
        for row in promotion_rows:
            promotions = by_variant_id[str(row[0])]["promotions"]
            if len(promotions) >= 2:
                raise RuntimeError("Experiment Variant has more than two Promotions")
            promotion: dict[str, Any] = {
                "promotion_id": str(row[1]),
                "experiment_id": experiment_id,
                "variant_id": str(row[0]),
                "target": str(row[2]),
                "created_at": str(row[5]),
            }
            if str(row[2]) == "preset":
                promotion["preset_id"] = str(row[3])
            else:
                promotion["revision_id"] = str(row[4])
                promotion["published"] = False
            promotions.append(promotion)

    @staticmethod
    def _get_experiment(
        connection: sqlite3.Connection, experiment_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT experiments.experiment_id,experiments.owner_id,experiments.plan_id,
                   plans.plan_digest,experiments.workflow_id,experiments.server_id,
                   experiments.pinned_revision_id,experiments.pinned_deployment_id,
                   experiments.pinned_content_digest,experiments.status,
                   experiments.failure_policy,experiments.concurrency,experiments.submission_window,
                   experiments.variant_count,experiments.pending_count,experiments.submitted_count,
                   experiments.running_count,experiments.completed_count,experiments.failed_count,
                   experiments.cancelled_count,experiments.lost_count,experiments.cancel_mode,
                   experiments.created_at,experiments.updated_at,experiments.completed_at
            FROM experiments
            JOIN experiment_plans AS plans
              ON plans.plan_id=experiments.plan_id AND plans.owner_id=experiments.owner_id
            WHERE experiments.experiment_id=? AND experiments.owner_id=?
            """,
            (experiment_id, owner_id),
        ).fetchone()
        if row is None:
            return None
        fields = (
            "experiment_id",
            "owner_id",
            "plan_id",
            "plan_digest",
            "workflow_id",
            "server_id",
            "pinned_revision_id",
            "pinned_deployment_id",
            "pinned_content_digest",
            "status",
            "failure_policy",
            "concurrency",
            "submission_window",
            "variant_count",
            "pending_count",
            "submitted_count",
            "running_count",
            "completed_count",
            "failed_count",
            "cancelled_count",
            "lost_count",
            "cancel_mode",
            "created_at",
            "updated_at",
            "completed_at",
        )
        result = dict(zip(fields, row, strict=True))
        result["cancel_mode"] = result["cancel_mode"] or ""
        result["completed_at"] = result["completed_at"] or ""
        result["resource_uri"] = f"comfyui://experiments/{experiment_id}"
        return result

    def _fault(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    def _connect(self) -> sqlite3.Connection:
        return self._store._connect()

    @staticmethod
    def _append_event_and_outbox(
        connection: sqlite3.Connection,
        *,
        event_type: str,
        subject_uri: str,
        correlation_id: str,
        principal_id: str,
        data: dict[str, Any],
        occurred_at: str,
    ) -> None:
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM domain_events WHERE subject_uri=?",
                (subject_uri,),
            ).fetchone()[0]
        )
        event_id = _stable_id("event", correlation_id, str(sequence), event_type)
        outbox_id = _stable_id("outbox", event_id)
        connection.execute(
            """
            INSERT INTO domain_events(
                event_id,event_type,subject_uri,sequence,occurred_at,principal_id,
                correlation_id,data_json
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                event_type,
                subject_uri,
                sequence,
                occurred_at,
                principal_id,
                correlation_id,
                _json(data),
            ),
        )
        connection.execute(
            """
            INSERT INTO outbox(outbox_id,event_id,topic,payload_json,status,created_at)
            VALUES(?,?, 'resources.updated',?,'pending',?)
            """,
            (
                outbox_id,
                event_id,
                _json({"uri": subject_uri, "sequence": sequence, "owner_id": principal_id}),
                occurred_at,
            ),
        )


def _stable_id(kind: str, *parts: str) -> str:
    payload = "\0".join((f"phase-m-{kind}-v1", *parts)).encode("utf-8")
    return f"{kind}_{hashlib.sha256(payload).hexdigest()}"


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _json_copy(value: object) -> Any:
    return json.loads(_json(value))


def _json_list(raw: str, *, field: str) -> list[Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{field} is not valid JSON") from exc
    if not isinstance(value, list):
        raise RuntimeError(f"{field} is not a list")
    return value


def _json_object(raw: str, *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{field} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{field} is not an object")
    return value


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Experiment persistence time must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _retained_plan_bytes(
    plan: dict[str, Any],
    *,
    encoded: dict[str, str],
    enrollments: Sequence[tuple[object, ...]],
    context: dict[str, Any],
    expires_at: str,
) -> int:
    plan_payload = (
        plan["plan_id"],
        plan["experiment_id"],
        plan["owner_id"],
        plan["workflow_id"],
        plan["server_id"],
        plan["plan_digest"],
        context["revision_id"],
        context["deployment_id"],
        context["content_digest"],
        encoded["expansion"],
        encoded["base_arguments"],
        encoded["budgets"],
        encoded["budget_totals"],
        plan["failure_policy"],
        plan["concurrency"],
        plan.get("execution_slots", 1),
        plan["submission_window"],
        plan["variant_count"],
        plan.get("output_cardinality", 1),
        plan.get("trusted_seconds_per_run", 1.0),
        encoded["variants"],
        encoded["variant_overrides"],
        plan["created_at"],
        expires_at,
    )
    return sum(_stored_value_bytes(value) for value in plan_payload) + sum(
        _stored_value_bytes(value) for enrollment in enrollments for value in enrollment
    )


def _stored_value_bytes(value: object) -> int:
    return len(str(value).encode("utf-8"))


def _enforce_owner_plan_quota(
    connection: sqlite3.Connection, *, owner_id: str, retained_bytes: int
) -> None:
    row = connection.execute(
        """
        SELECT count(*),COALESCE(sum(retained_bytes),0)
        FROM experiment_plans INDEXED BY ix_experiment_plans_owner_unpruned
        WHERE owner_id=? AND payload_pruned_at IS NULL
        """,
        (owner_id,),
    ).fetchone()
    live_count = int(row[0])
    live_bytes = int(row[1])
    if (
        live_count + 1 > _MAX_OWNER_LIVE_PLAN_COUNT
        or live_bytes + retained_bytes > _MAX_OWNER_LIVE_PLAN_BYTES
    ):
        raise ValueError("owner live Experiment plan quota exceeded")


def _execution_input_digest(arguments: dict[str, Any]) -> str:
    execution_input_json = _json({"arguments": arguments, "resolved_inputs": arguments})
    encoded_input = execution_input_json.encode("utf-8")
    if len(encoded_input) > _MAX_EXECUTION_INPUT_BYTES:
        raise ValueError("Experiment Variant materialized arguments exceed 1 MiB")
    return hashlib.sha256(encoded_input).hexdigest()


def _materialize_arguments(
    base_arguments_json: str,
    overrides_json: str,
    *,
    expected_execution_input_digest: str | None,
) -> dict[str, Any]:
    base_arguments = _json_object(base_arguments_json, field="Experiment plan base arguments")
    overrides = _json_object(overrides_json, field="Experiment Variant overrides")
    arguments = {**base_arguments, **overrides}
    execution_input_digest = _execution_input_digest(arguments)
    if (
        expected_execution_input_digest is not None
        and execution_input_digest != expected_execution_input_digest
    ):
        raise sqlite3.IntegrityError("Experiment Variant materialized argument digest conflict")
    return arguments


def _ensure_compact_materialization_bound(base_json: str, overrides_json: str) -> None:
    conservative_bytes = 2 * (
        len(base_json.encode("utf-8")) + len(overrides_json.encode("utf-8")) + 64
    )
    if conservative_bytes > _MAX_EXECUTION_INPUT_BYTES:
        raise ValueError("Experiment Variant materialized arguments exceed 1 MiB")


def _encode_plan_payload(
    plan: dict[str, Any], variants: Sequence[dict[str, Any]]
) -> tuple[dict[str, str], list[tuple[object, ...]]]:
    base_arguments = _json_copy(plan["base_arguments"])
    if not isinstance(base_arguments, dict):
        raise ValueError("Experiment plan base arguments must be an object")
    base_json = _json(base_arguments)
    base_digest = hashlib.sha256(base_json.encode("utf-8")).hexdigest()
    enrollments: list[tuple[object, ...]] = []
    seen_ids: set[str] = set()
    seen_ordinals: set[int] = set()
    for value in variants:
        if not isinstance(value, dict):
            raise ValueError("Experiment plan Variant must be an object")
        if (
            value.get("experiment_id", plan["experiment_id"]) != plan["experiment_id"]
            or value.get("owner_id", plan["owner_id"]) != plan["owner_id"]
        ):
            raise ValueError("Experiment plan Variant ownership conflict")
        ordinal = value["ordinal"]
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
            raise ValueError("Experiment plan Variant ordinal must be a non-negative integer")
        parameter_digest = str(value["parameter_digest"])
        variant_id = str(
            value.get("variant_id")
            or "variant_"
            + hashlib.sha256(
                _json(
                    [
                        "experiment-variant-v2",
                        plan["experiment_id"],
                        ordinal,
                        parameter_digest,
                    ]
                ).encode("utf-8")
            ).hexdigest()
        )
        if variant_id in seen_ids or ordinal in seen_ordinals:
            raise ValueError("Experiment plan Variant enrollment conflict")
        seen_ids.add(variant_id)
        seen_ordinals.add(ordinal)
        if value.get("status", "pending") != "pending" or value.get("job_id") not in (None, ""):
            raise ValueError("new Experiment Variant must be pending and unsubmitted")
        if "overrides" in value:
            overrides = _json_copy(value["overrides"])
            if not isinstance(overrides, dict):
                raise ValueError("Experiment plan Variant overrides must be an object")
        else:
            arguments = _json_copy(value.get("arguments"))
            if not isinstance(arguments, dict):
                raise ValueError("Experiment plan Variant arguments must be an object")
            overrides = {
                key: arguments[key]
                for key in sorted(arguments)
                if key not in base_arguments or arguments[key] != base_arguments[key]
            }
        overrides_json = _json(overrides)
        computed_digest = hashlib.sha256(
            _json(["resolved-variant-v2", base_digest, overrides]).encode("utf-8")
        ).hexdigest()
        if "overrides" in value and parameter_digest != computed_digest:
            raise ValueError("Experiment plan Variant parameter digest conflict")
        _ensure_compact_materialization_bound(base_json, overrides_json)
        enrollments.append(
            (
                plan["plan_id"],
                plan["owner_id"],
                plan["experiment_id"],
                variant_id,
                ordinal,
                overrides_json,
                parameter_digest,
                str(value.get("created_at", plan["created_at"])),
            )
        )
    enrollments.sort(key=_enrollment_sort_key)
    if [_enrollment_sort_key(row)[0] for row in enrollments] != list(range(len(enrollments))):
        raise ValueError("Experiment plan Variant ordinals must be contiguous")
    return (
        {
            "expansion": _json(plan["expansion"]),
            "base_arguments": base_json,
            "budgets": _json(plan["budgets"]),
            "budget_totals": _json(plan["budget_totals"]),
            "variants": "[]",
            "variant_overrides": "{}",
        },
        enrollments,
    )


def _enrollment_sort_key(row: tuple[object, ...]) -> tuple[int, str]:
    ordinal = row[4]
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
        raise ValueError("Experiment plan Variant ordinal must be an integer")
    return ordinal, str(row[3])


def _variant_runtime_row(
    enrollment: sqlite3.Row,
    *,
    experiment_id: str,
    owner_id: str,
) -> tuple[object, ...]:
    variant_id = str(enrollment["variant_id"])
    idempotency_key = f"experiment:{experiment_id}:variant:{variant_id}"
    client_id = "experiment-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return (
        variant_id,
        experiment_id,
        owner_id,
        int(enrollment["ordinal"]),
        str(enrollment["overrides_json"]),
        str(enrollment["parameter_digest"]),
        client_id,
        idempotency_key,
        "pending",
        str(enrollment["created_at"]),
        str(enrollment["created_at"]),
    )


def _submission_claim_projection(
    row: sqlite3.Row, *, lease: WorkLease, claimed_at: str
) -> dict[str, Any]:
    arguments = _materialize_arguments(
        str(row["base_arguments_json"]),
        str(row["overrides_json"]),
        expected_execution_input_digest=str(row["execution_input_digest"]),
    )
    return {
        "status": "submitted",
        "variant_id": str(row["variant_id"]),
        "client_id": str(row["client_id"]),
        "workflow_id": str(row["workflow_id"]),
        "server_id": str(row["server_id"]),
        "plan_id": str(row["plan_id"]),
        "revision_id": str(row["pinned_revision_id"]),
        "deployment_id": str(row["pinned_deployment_id"]),
        "content_digest": str(row["pinned_content_digest"]),
        "arguments": arguments,
        "attempt_id": "",
        "claim_token": f"{lease.work_item_id}:{lease.fencing_token}",
        "fencing_token": lease.fencing_token,
        "claimed_at": claimed_at,
    }


def _ensure_published_context(
    connection: sqlite3.Connection,
    *,
    owner_id: str,
    workflow_id: str,
    server_id: str,
    created_at: str,
    expected_revision_id: object = None,
    expected_deployment_id: object = None,
    expected_content_digest: object = None,
) -> dict[str, Any]:
    expected = (expected_revision_id, expected_deployment_id, expected_content_digest)
    if any(value is not None for value in expected) and not all(
        value is not None for value in expected
    ):
        raise ValueError("Experiment planning publication pin is incomplete")
    parameters: tuple[object, ...]
    pin_filter = ""
    if all(value is not None for value in expected):
        pin_filter = (
            "AND revisions.revision_id=? AND deployments.deployment_id=? "
            "AND revisions.content_digest=?"
        )
        parameters = (workflow_id, server_id, *expected, owner_id, owner_id)
    else:
        parameters = (workflow_id, server_id, owner_id, owner_id)
    row = connection.execute(
        f"""
        SELECT revisions.revision_id,deployments.deployment_id,revisions.content_digest,
               revisions.graph_json,revisions.parameter_schema_json,
               revisions.dependency_contract_json
        FROM workflow_deployments AS deployments
        JOIN workflow_revisions AS revisions
          ON revisions.workflow_id=deployments.workflow_id
         AND revisions.revision_id=deployments.revision_id
        WHERE deployments.workflow_id=? AND deployments.server_id=?
          AND deployments.enabled=1 AND deployments.validation_status='valid' {pin_filter}
          AND (
            (NOT EXISTS (SELECT 1 FROM config_workflow_snapshots WHERE owner_id=?)
             AND deployments.published=1)
            OR EXISTS (
                SELECT 1 FROM config_workflow_deployments AS bindings
                JOIN config_workflow_states AS states ON states.owner_id=bindings.owner_id
                 AND states.server_id=bindings.server_id
                 AND states.workflow_id=bindings.workflow_id
                JOIN managed_servers AS servers ON servers.owner_id=bindings.owner_id
                 AND servers.server_id=bindings.server_id
                 AND servers.lifecycle_status='active'
                WHERE bindings.owner_id=?
                 AND bindings.server_id=deployments.server_id
                 AND bindings.workflow_id=deployments.workflow_id
                 AND bindings.deployment_id=deployments.deployment_id
                 AND states.enabled=1
            )
          )
        """,
        parameters,
    ).fetchone()
    if row is None:
        raise ValueError("Experiment planning requires a valid published deployment")
    graph = _json_object(str(row[3]), field="pinned Revision graph")
    schema = _json_object(str(row[4]), field="pinned Revision parameter schema")
    dependencies = _json_object(str(row[5]), field="pinned Revision dependency contract")
    capacity = connection.execute(
        "SELECT execution_slots,subject_submission_quota FROM experiment_server_capacities WHERE server_id=?",
        (server_id,),
    ).fetchone()
    output_cardinality = dependencies.get("output_cardinality", graph.get("output_cardinality", 0))
    trusted_seconds = dependencies.get(
        "trusted_seconds_per_run", graph.get("trusted_seconds_per_run", 0)
    )
    return {
        "revision_id": str(row[0]),
        "deployment_id": str(row[1]),
        "content_digest": str(row[2]),
        "parameter_schema": _enrich_parameter_schema(schema, graph),
        "output_cardinality": output_cardinality,
        "trusted_seconds_per_run": trusted_seconds,
        "execution_slots": 1 if capacity is None else int(capacity[0]),
        "subject_submission_quota": 0 if capacity is None else int(capacity[1]),
    }


def _enrich_parameter_schema(schema: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    enriched = _json_copy(schema)
    properties = enriched.get("properties")
    if not isinstance(properties, dict):
        return enriched
    graph_defaults = graph.get("parameter_defaults", {})
    if not isinstance(graph_defaults, dict):
        graph_defaults = {}
    for name, definition in properties.items():
        if not isinstance(definition, dict):
            continue
        if "default" not in definition and name in graph_defaults:
            definition["default"] = _json_copy(graph_defaults[name])
        if "semantic_role" not in definition and name in {"width", "height", "batch_size"}:
            definition["semantic_role"] = name
    return enriched
