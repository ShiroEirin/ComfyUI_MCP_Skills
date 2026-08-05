"""Owner-bound SQLite persistence for Phase O server and provisioning control."""

# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from comfyui_mcp_skills.domain.orchestration import OutboxMessage, WorkLease
from comfyui_mcp_skills.domain.provisioning import (
    MAX_BUNDLE_BYTES,
    MAX_CHECKPOINT_BYTES,
    MAX_PLAN_BYTES,
    MAX_RESULT_BYTES,
    canonical_digest,
    canonical_json,
    require_public_json,
    require_sha256,
    validate_dependency_items,
)
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore

_INSTALL_CONFIRMATION = "INSTALL APPROVED DEPENDENCIES"
_TERMINAL_JOBS = frozenset({"completed", "failed", "cancelled"})
_TERMINAL_ITEMS = frozenset({"completed", "failed", "cancelled"})


class SQLiteProvisioningRepository:
    """Persist plans, approvals, install evidence, audit, and admin notifications."""

    def __init__(self, store: SQLiteControlPlaneStore) -> None:
        self._store = store

    def save_server_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        value = _server_plan(plan)
        row = (
            value["plan_id"],
            value["plan_digest"],
            value["owner_id"],
            value["operation"],
            value["server_id"],
            _json(value["changes"]),
            value["expected_revision"],
            _json(value["impact"]),
            value["created_at"],
            value["expires_at"],
            value["resource_uri"],
        )
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT plan_id,plan_digest,owner_id,operation,server_id,changes_json,expected_revision,impact_json,created_at,expires_at,resource_uri FROM server_change_plans WHERE plan_id=?",
                (value["plan_id"],),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != row:
                    raise ValueError("server plan identity conflict")
            else:
                connection.execute(
                    "INSERT INTO server_change_plans(plan_id,plan_digest,owner_id,operation,server_id,changes_json,expected_revision,impact_json,created_at,expires_at,resource_uri) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    row,
                )
        return _copy(value)

    def commit_server_plan(
        self, plan_id: str, plan_digest: str, owner_id: str, *, now: datetime
    ) -> dict[str, Any]:
        require_sha256(plan_digest, field="server plan digest")
        now_text = _time(now)
        with self._transaction() as connection:
            plan = connection.execute(
                "SELECT operation,server_id,changes_json,expected_revision,expires_at FROM server_change_plans WHERE plan_id=? AND plan_digest=? AND owner_id=?",
                (plan_id, plan_digest, owner_id),
            ).fetchone()
            if plan is None:
                if connection.execute(
                    "SELECT 1 FROM server_change_plans WHERE plan_id=?", (plan_id,)
                ).fetchone():
                    raise ValueError("server plan owner or digest conflict")
                raise LookupError("server plan was not found")
            committed = connection.execute(
                "SELECT server_id FROM server_plan_commits WHERE plan_id=? AND owner_id=?",
                (plan_id, owner_id),
            ).fetchone()
            if committed is not None:
                result = self._get_server(
                    connection, str(committed[0]), owner_id, include_deleted=True
                )
                if result is None:
                    raise RuntimeError("committed server revision is missing")
                return result
            if _parse_time(str(plan[4])) <= _aware(now):
                raise ValueError("server plan is expired")
            operation, server_id = str(plan[0]), str(plan[1])
            changes = _object(str(plan[2]), field="server changes")
            current = self._server_row(connection, server_id, owner_id)
            expected = plan[3]
            current_revision = 0 if current is None else int(current[0])
            if expected is not None and int(expected) != current_revision:
                raise ValueError("server revision conflict")
            if operation != "upsert" and current is None:
                raise LookupError("server was not found")
            if operation == "set_default":
                if current is None or str(current[2]) != "active":
                    raise ValueError("default server must be active")
                connection.execute(
                    "INSERT INTO server_defaults(owner_id,server_id,server_revision,plan_id,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(owner_id) DO UPDATE SET server_id=excluded.server_id,server_revision=excluded.server_revision,plan_id=excluded.plan_id,updated_at=excluded.updated_at",
                    (owner_id, server_id, current_revision, plan_id, now_text),
                )
                revision, result_digest = current_revision, str(current[1])
            else:
                config = {} if current is None else _object(str(current[3]), field="server config")
                config.update(changes)
                if operation == "set_enabled":
                    config["enabled"] = bool(changes["enabled"])
                lifecycle = (
                    "deleted"
                    if operation == "delete"
                    else "active"
                    if bool(config.get("enabled", True))
                    else "disabled"
                )
                revision = current_revision + 1
                result_digest = canonical_digest(config)
                connection.execute(
                    "INSERT INTO server_revisions(server_id,owner_id,revision,lifecycle_status,config_json,config_digest,plan_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        server_id,
                        owner_id,
                        revision,
                        lifecycle,
                        _json(config),
                        result_digest,
                        plan_id,
                        now_text,
                    ),
                )
                if current is None:
                    connection.execute(
                        "INSERT INTO managed_servers(server_id,owner_id,current_revision,current_digest,lifecycle_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (
                            server_id,
                            owner_id,
                            revision,
                            result_digest,
                            lifecycle,
                            now_text,
                            now_text,
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE managed_servers SET current_revision=?,current_digest=?,lifecycle_status=?,updated_at=? WHERE server_id=? AND owner_id=?",
                        (revision, result_digest, lifecycle, now_text, server_id, owner_id),
                    )
                if lifecycle != "active":
                    connection.execute(
                        "DELETE FROM server_defaults WHERE owner_id=? AND server_id=?",
                        (owner_id, server_id),
                    )
                if lifecycle == "deleted":
                    connection.execute(
                        "DELETE FROM config_workflow_states WHERE owner_id=? AND server_id=?",
                        (owner_id, server_id),
                    )
            connection.execute(
                "INSERT INTO server_plan_commits(plan_id,plan_digest,owner_id,server_id,committed_revision,result_digest,committed_at) VALUES(?,?,?,?,?,?,?)",
                (plan_id, plan_digest, owner_id, server_id, revision, result_digest, now_text),
            )
            result = self._get_server(connection, server_id, owner_id, include_deleted=True)
            if result is None:
                raise RuntimeError("server commit did not produce a revision")
            self._event(
                connection,
                owner_id,
                "SERVER_UPDATED",
                result["resource_uri"],
                plan_id,
                {"server_id": server_id, "revision": revision, "status": result["status"]},
                now_text,
            )
            if operation == "upsert" and result["status"] != "deleted":
                connection.execute(
                    "INSERT INTO config_workflow_snapshots(owner_id,updated_at) VALUES(?,?) "
                    "ON CONFLICT(owner_id) DO UPDATE SET updated_at=excluded.updated_at",
                    (owner_id, now_text),
                )
            self._advance_config_state(connection, owner_id, now_text)
            return result

    def list_servers(self, owner_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT server_id FROM managed_servers WHERE owner_id=? AND lifecycle_status!='deleted' ORDER BY server_id LIMIT 201",
                (owner_id,),
            ).fetchall()
            return [
                item
                for row in rows
                if (item := self._get_server(connection, str(row[0]), owner_id)) is not None
            ]
        finally:
            connection.close()

    def get_server(self, server_id: str, owner_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            return self._get_server(connection, server_id, owner_id)
        finally:
            connection.close()

    def server_delete_impact(self, server_id: str, owner_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            if self._server_row(connection, server_id, owner_id) is None:
                raise LookupError("server was not found")
            jobs = int(
                connection.execute(
                    "SELECT count(*) FROM jobs WHERE owner_id=? AND server_id=? AND status NOT IN ('completed','error','interrupted','cancelled','lost')",
                    (owner_id, server_id),
                ).fetchone()[0]
            )
            workflows = int(
                connection.execute(
                    "SELECT count(DISTINCT workflow_id) FROM jobs WHERE owner_id=? AND server_id=?",
                    (owner_id, server_id),
                ).fetchone()[0]
            )
            provisioning = int(
                connection.execute(
                    "SELECT count(*) FROM provisioning_jobs WHERE owner_id=? AND server_id=? AND status NOT IN ('completed','failed','cancelled')",
                    (owner_id, server_id),
                ).fetchone()[0]
            )
            return {
                "server_id": server_id,
                "workflow_count": workflows,
                "nonterminal_jobs": jobs + provisioning,
            }
        finally:
            connection.close()

    def current_revision(self, owner_id: str) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT current_revision FROM config_state WHERE owner_id=?", (owner_id,)
            ).fetchone()
            return 0 if row is None else int(row[0])
        finally:
            connection.close()

    def export_snapshot(self, owner_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT server_id FROM managed_servers WHERE owner_id=? AND lifecycle_status!='deleted' ORDER BY server_id LIMIT 201",
                (owner_id,),
            ).fetchall()
            servers = [self._get_server(connection, str(row[0]), owner_id) for row in rows]
            default = connection.execute(
                "SELECT server_id FROM server_defaults WHERE owner_id=?", (owner_id,)
            ).fetchone()
            workflows = connection.execute(
                "SELECT server_id,workflow_id,enabled FROM config_workflow_states "
                "WHERE owner_id=? ORDER BY server_id,workflow_id LIMIT 257",
                (owner_id,),
            ).fetchall()
            if len(workflows) > 256:
                raise ValueError("Config snapshot has too many Workflows")
            return {
                "servers": [item for item in servers if item is not None],
                "workflows": [
                    {
                        "server_id": str(item[0]),
                        "workflow_id": str(item[1]),
                        "enabled": bool(item[2]),
                    }
                    for item in workflows
                ],
                "default_server": None if default is None else str(default[0]),
            }
        finally:
            connection.close()

    def save_bundle(self, bundle: dict[str, Any]) -> dict[str, Any]:
        value = _bundle(bundle)
        row = (
            value["bundle_id"],
            value["owner_id"],
            value["version"],
            value["revision"],
            _json(value["content"]),
            value["content_digest"],
            value["created_at"],
            value["resource_uri"],
        )
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT bundle_id,owner_id,bundle_version,revision,content_json,content_digest,created_at,resource_uri FROM config_bundles WHERE bundle_id=?",
                (value["bundle_id"],),
            ).fetchone()
            if existing is not None and tuple(existing) != row:
                raise ValueError("config bundle identity conflict")
            if existing is None:
                connection.execute(
                    "INSERT INTO config_bundles(bundle_id,owner_id,bundle_version,revision,content_json,content_digest,created_at,resource_uri) VALUES(?,?,?,?,?,?,?,?)",
                    row,
                )
        return _copy(value)

    def get_bundle(self, revision: int, owner_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT bundle_id,owner_id,bundle_version,revision,content_json,content_digest,created_at,resource_uri FROM config_bundles WHERE owner_id=? AND revision=? ORDER BY created_at DESC,bundle_id DESC LIMIT 1",
                (owner_id, revision),
            ).fetchone()
            return None if row is None else _bundle_projection(row)
        finally:
            connection.close()

    def save_import_plan(self, plan: dict[str, Any]) -> None:
        value = _import_plan(plan)
        row = (
            value["plan_id"],
            value["plan_digest"],
            value["owner_id"],
            value["expected_revision"],
            value["bundle_version"],
            value["source_digest"],
            _json(value["content"]),
            _json(value["merge_summary"]),
            value["created_at"],
            value["expires_at"],
            value["resource_uri"],
        )
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT plan_id,plan_digest,owner_id,expected_revision,bundle_version,source_digest,content_json,merge_summary_json,created_at,expires_at,resource_uri FROM config_import_plans WHERE plan_id=?",
                (value["plan_id"],),
            ).fetchone()
            if existing is not None and tuple(existing) != row:
                raise ValueError("config import plan identity conflict")
            if existing is None:
                connection.execute(
                    "INSERT INTO config_import_plans(plan_id,plan_digest,owner_id,expected_revision,bundle_version,source_digest,content_json,merge_summary_json,created_at,expires_at,resource_uri) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    row,
                )

    def commit_import_plan(
        self, plan_id: str, plan_digest: str, owner_id: str, *, now: datetime
    ) -> dict[str, Any]:
        now_text = _time(now)
        with self._transaction() as connection:
            plan = connection.execute(
                "SELECT expected_revision,bundle_version,source_digest,content_json,expires_at FROM config_import_plans WHERE plan_id=? AND plan_digest=? AND owner_id=?",
                (plan_id, plan_digest, owner_id),
            ).fetchone()
            if plan is None:
                if connection.execute(
                    "SELECT 1 FROM config_import_plans WHERE plan_id=?", (plan_id,)
                ).fetchone():
                    raise ValueError("config import owner or digest conflict")
                raise LookupError("config import plan was not found")
            existing = connection.execute(
                "SELECT bundle_id FROM config_import_commits WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if existing is not None:
                row = connection.execute(
                    "SELECT bundle_id,owner_id,bundle_version,revision,content_json,content_digest,created_at,resource_uri FROM config_bundles WHERE bundle_id=? AND owner_id=?",
                    (existing[0], owner_id),
                ).fetchone()
                if row is None:
                    raise RuntimeError("committed config bundle is missing")
                return _bundle_projection(row)
            if _parse_time(str(plan[4])) <= _aware(now):
                raise ValueError("config import plan is expired")
            current_row = connection.execute(
                "SELECT current_revision FROM config_state WHERE owner_id=?", (owner_id,)
            ).fetchone()
            current = 0 if current_row is None else int(current_row[0])
            expected = int(plan[0])
            if current != expected:
                raise ValueError("config revision conflict")
            revision = expected + 1
            content = _object(str(plan[3]), field="config import content")
            digest = canonical_digest(content)
            if digest != str(plan[2]):
                raise ValueError("config import source digest conflict")
            self._apply_config_content(
                connection,
                owner_id,
                content,
                import_plan_id=plan_id,
                created_at=now_text,
                expires_at=str(plan[4]),
            )
            bundle_id = _stable("config_bundle", owner_id, str(revision), digest)
            uri = f"comfyui://config/bundles/{revision}"
            connection.execute(
                "INSERT INTO config_bundles(bundle_id,owner_id,bundle_version,revision,content_json,content_digest,created_at,resource_uri) VALUES(?,?,?,?,?,?,?,?)",
                (
                    bundle_id,
                    owner_id,
                    int(plan[1]),
                    revision,
                    _json(content),
                    digest,
                    now_text,
                    uri,
                ),
            )
            if current_row is None:
                connection.execute(
                    "INSERT INTO config_state(owner_id,current_revision,current_digest,updated_at) VALUES(?,?,?,?)",
                    (owner_id, revision, digest, now_text),
                )
            else:
                connection.execute(
                    "UPDATE config_state SET current_revision=?,current_digest=?,updated_at=? WHERE owner_id=? AND current_revision=?",
                    (revision, digest, now_text, owner_id, expected),
                )
            connection.execute(
                "INSERT INTO config_import_commits(plan_id,plan_digest,owner_id,committed_revision,bundle_id,committed_at) VALUES(?,?,?,?,?,?)",
                (plan_id, plan_digest, owner_id, revision, bundle_id, now_text),
            )
            self._event(
                connection,
                owner_id,
                "CONFIG_IMPORTED",
                uri,
                plan_id,
                {"revision": revision, "content_digest": digest},
                now_text,
            )
            return {
                "bundle_id": bundle_id,
                "owner_id": owner_id,
                "version": int(plan[1]),
                "revision": revision,
                "content": content,
                "content_digest": digest,
                "created_at": now_text,
                "resource_uri": uri,
            }

    def inspect_dependencies(
        self, owner_id: str, server_id: str, workflow_id: str, revision_id: str
    ) -> dict[str, Any]:
        connection = self._connect()
        try:
            if self._server_row(connection, server_id, owner_id) is None:
                raise LookupError("server was not found")
            requirements: list[dict[str, Any]] = []
            if workflow_id and revision_id:
                row = connection.execute(
                    "SELECT dependency_contract_json FROM workflow_revisions WHERE workflow_id=? AND revision_id=?",
                    (workflow_id, revision_id),
                ).fetchone()
                if row is None:
                    raise LookupError("workflow revision was not found")
                raw = json.loads(str(row[0]))
                candidates = (
                    raw.get("requirements", raw.get("dependencies", []))
                    if isinstance(raw, dict)
                    else []
                )
                if isinstance(candidates, list):
                    requirements = [
                        dict(item) for item in candidates[:200] if isinstance(item, dict)
                    ]
            return {
                "owner_id": owner_id,
                "server_id": server_id,
                "workflow_id": workflow_id,
                "revision_id": revision_id,
                "requirements": requirements,
            }
        finally:
            connection.close()

    def save_plan(self, plan: dict[str, Any], items: list[dict[str, Any]]) -> None:
        value, normalized_items = _dependency_plan(plan, items)
        row = (
            value["plan_id"],
            value["plan_digest"],
            value["owner_id"],
            value["server_id"],
            value["server_revision"],
            value["server_config_digest"],
            value["inspection_digest"],
            int(value["restart_required"]),
            value["request_confirmation"],
            value["created_at"],
            value["expires_at"],
            value["resource_uri"],
        )
        with self._transaction() as connection:
            server = self._server_row(connection, str(value["server_id"]), str(value["owner_id"]))
            if server is None or str(server[2]) == "deleted":
                raise LookupError("dependency plan server was not found for owner")
            if int(server[0]) != int(value["server_revision"]) or str(server[1]) != str(
                value["server_config_digest"]
            ):
                raise ValueError("dependency plan Server revision conflict")
            existing = connection.execute(
                "SELECT plan_id,plan_digest,owner_id,server_id,server_revision,server_config_digest,inspection_digest,restart_required,request_confirmation,created_at,expires_at,resource_uri FROM dependency_plans WHERE plan_id=?",
                (value["plan_id"],),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != row:
                    raise ValueError("dependency plan identity conflict")
                stored = connection.execute(
                    "SELECT item_id,dependency_id,kind,source_type,source_url,version,checksum,size_bytes,target_dir,license,restart_required,install_state FROM dependency_plan_items WHERE plan_id=? ORDER BY ordinal",
                    (value["plan_id"],),
                ).fetchall()
                if [_item_projection(item) for item in stored] != normalized_items:
                    raise ValueError("dependency plan item conflict")
                return
            connection.execute(
                "INSERT INTO dependency_plans(plan_id,plan_digest,owner_id,server_id,server_revision,server_config_digest,inspection_digest,restart_required,request_confirmation,created_at,expires_at,resource_uri) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            connection.executemany(
                "INSERT INTO dependency_plan_items(plan_id,owner_id,item_id,ordinal,dependency_id,kind,source_type,source_url,version,checksum,size_bytes,target_dir,license,restart_required,install_state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        value["plan_id"],
                        value["owner_id"],
                        item["item_id"],
                        ordinal,
                        item["dependency_id"],
                        item["kind"],
                        item["source_type"],
                        item["source_url"],
                        item["version"],
                        item["checksum"],
                        item["size_bytes"],
                        item["target_dir"],
                        item["license"],
                        int(item["restart_required"]),
                        item["install_state"],
                    )
                    for ordinal, item in enumerate(normalized_items)
                ],
            )

    def get_plan(self, plan_id: str, owner_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            return self._get_plan(connection, plan_id, owner_id)
        finally:
            connection.close()

    def create_approval(
        self, plan_id: str, plan_digest: str, owner_id: str, *, now: datetime
    ) -> dict[str, Any]:
        now_text = _time(now)
        with self._transaction() as connection:
            plan = connection.execute(
                "SELECT restart_required,expires_at FROM dependency_plans WHERE plan_id=? AND plan_digest=? AND owner_id=?",
                (plan_id, plan_digest, owner_id),
            ).fetchone()
            if plan is None:
                raise LookupError("dependency plan was not found")
            approval_id = _stable("approval", owner_id, plan_id, plan_digest)
            existing = self._get_approval(connection, approval_id, owner_id, now=_aware(now))
            if existing is not None:
                return existing
            count = int(
                connection.execute(
                    "SELECT count(*) FROM dependency_plan_items WHERE plan_id=?", (plan_id,)
                ).fetchone()[0]
            )
            impact = {"item_count": count, "restart_required": bool(plan[0])}
            connection.execute(
                "INSERT INTO approvals(approval_id,owner_id,operation,plan_id,plan_digest,impact_summary_json,single_use,revision,created_at,expires_at,resource_uri) VALUES(?,?,'dependency.install',?,?,?,1,0,?,?,?)",
                (
                    approval_id,
                    owner_id,
                    plan_id,
                    plan_digest,
                    _json(impact),
                    now_text,
                    str(plan[1]),
                    f"comfyui://approvals/{approval_id}",
                ),
            )
            result = self._get_approval(connection, approval_id, owner_id, now=_aware(now))
            if result is None:
                raise RuntimeError("approval was not created")
            self._event(
                connection,
                owner_id,
                "APPROVAL_REQUESTED",
                result["resource_uri"],
                plan_id,
                {"approval_id": approval_id, "status": "pending"},
                now_text,
            )
            return result

    def get_approval(
        self,
        approval_id: str,
        owner_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            return self._get_approval(
                connection,
                approval_id,
                owner_id,
                now=_aware(now or datetime.now(timezone.utc)),
            )
        finally:
            connection.close()

    def save_approval_plan(self, plan: dict[str, Any]) -> None:
        value = _approval_plan(plan)
        row = (
            value["approval_plan_id"],
            value["plan_digest"],
            value["approval_id"],
            value["owner_id"],
            value["decision"],
            value["reason"],
            value["approval_revision"],
            value["status_before"],
            value["created_at"],
            value["expires_at"],
            value["resource_uri"],
        )
        with self._transaction() as connection:
            approval = self._get_approval(
                connection,
                value["approval_id"],
                value["owner_id"],
                now=_parse_time(value["created_at"]),
            )
            if approval is None:
                raise LookupError("approval was not found")
            if (
                approval["status"] != "pending"
                or approval["revision"] != value["approval_revision"]
            ):
                raise ValueError("approval state conflict")
            existing = connection.execute(
                "SELECT approval_plan_id,plan_digest,approval_id,owner_id,decision,reason,approval_revision,status_before,created_at,expires_at,resource_uri FROM approval_decision_plans WHERE approval_plan_id=?",
                (value["approval_plan_id"],),
            ).fetchone()
            if existing is not None and tuple(existing) != row:
                raise ValueError("approval plan identity conflict")
            if existing is None:
                connection.execute(
                    "INSERT INTO approval_decision_plans(approval_plan_id,plan_digest,approval_id,owner_id,decision,reason,approval_revision,status_before,created_at,expires_at,resource_uri) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    row,
                )

    def commit_approval_plan(
        self, approval_plan_id: str, plan_digest: str, owner_id: str, *, now: datetime
    ) -> dict[str, Any]:
        now_text = _time(now)
        with self._transaction() as connection:
            plan = connection.execute(
                "SELECT approval_id,decision,reason,approval_revision,expires_at FROM approval_decision_plans WHERE approval_plan_id=? AND plan_digest=? AND owner_id=?",
                (approval_plan_id, plan_digest, owner_id),
            ).fetchone()
            if plan is None:
                if connection.execute(
                    "SELECT 1 FROM approval_decision_plans WHERE approval_plan_id=?",
                    (approval_plan_id,),
                ).fetchone():
                    raise ValueError("approval plan owner or digest conflict")
                raise LookupError("approval plan was not found")
            approval_id = str(plan[0])
            committed = connection.execute(
                "SELECT 1 FROM approval_decision_commits WHERE approval_plan_id=?",
                (approval_plan_id,),
            ).fetchone()
            if committed is not None:
                result = self._get_approval(connection, approval_id, owner_id, now=_aware(now))
                if result is None:
                    raise RuntimeError("committed approval is missing")
                return result
            if _parse_time(str(plan[4])) <= _aware(now):
                raise ValueError("approval plan is expired")
            current = self._get_approval(connection, approval_id, owner_id, now=_aware(now))
            if (
                current is None
                or current["status"] != "pending"
                or current["revision"] != int(plan[3])
            ):
                raise ValueError("approval state conflict")
            connection.execute(
                "INSERT INTO approval_decisions(approval_id,owner_id,approval_plan_id,decision,reason,decided_at) VALUES(?,?,?,?,?,?)",
                (approval_id, owner_id, approval_plan_id, str(plan[1]), str(plan[2]), now_text),
            )
            connection.execute(
                "INSERT INTO approval_decision_commits(approval_plan_id,plan_digest,owner_id,approval_id,committed_at) VALUES(?,?,?,?,?)",
                (approval_plan_id, plan_digest, owner_id, approval_id, now_text),
            )
            result = self._get_approval(connection, approval_id, owner_id, now=_aware(now))
            if result is None:
                raise RuntimeError("approval commit failed")
            self._event(
                connection,
                owner_id,
                "APPROVAL_UPDATED",
                result["resource_uri"],
                approval_plan_id,
                {"approval_id": approval_id, "status": result["status"]},
                now_text,
            )
            return result

    def commit_plan(
        self,
        plan_id: str,
        plan_digest: str,
        approval_id: str,
        owner_id: str,
        request_id: str,
        confirmation: str,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        if confirmation != _INSTALL_CONFIRMATION:
            raise ValueError("exact installation confirmation is required")
        now_text = _time(now)
        with self._transaction() as connection:
            duplicate = connection.execute(
                "SELECT job_id,plan_id,plan_digest,approval_id FROM provisioning_jobs WHERE owner_id=? AND request_id=?",
                (owner_id, request_id),
            ).fetchone()
            if duplicate is not None:
                if tuple(str(duplicate[index]) for index in (1, 2, 3)) != (
                    plan_id,
                    plan_digest,
                    approval_id,
                ):
                    raise ValueError("provisioning request id conflict")
                result = self._get_job(connection, str(duplicate[0]), owner_id)
                if result is None:
                    raise RuntimeError("idempotent provisioning job is missing")
                return result
            plan = connection.execute(
                "SELECT server_id,server_revision,server_config_digest,restart_required,expires_at FROM dependency_plans WHERE plan_id=? AND plan_digest=? AND owner_id=?",
                (plan_id, plan_digest, owner_id),
            ).fetchone()
            if plan is None:
                raise ValueError("dependency plan owner or digest conflict")
            if _parse_time(str(plan[4])) <= _aware(now):
                raise ValueError("dependency plan is expired")
            server = self._server_row(connection, str(plan[0]), owner_id)
            if (
                server is None
                or int(server[0]) != int(plan[1])
                or str(server[1]) != str(plan[2])
                or str(server[2]) == "deleted"
            ):
                raise ValueError("dependency plan Server revision conflict")
            approval = self._get_approval(connection, approval_id, owner_id, now=_aware(now))
            if (
                approval is None
                or approval["plan_id"] != plan_id
                or approval["plan_digest"] != plan_digest
                or approval["status"] != "approved"
            ):
                raise ValueError("approval binding conflict")
            job_id = _stable("provisioning_job", owner_id, request_id)
            uri = f"comfyui://provisioning/jobs/{job_id}"
            connection.execute(
                "INSERT INTO provisioning_jobs(job_id,owner_id,plan_id,plan_digest,approval_id,request_id,server_id,server_revision,server_config_digest,status,restart_required,created_at,updated_at,completed_at,resource_uri) VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?)",
                (
                    job_id,
                    owner_id,
                    plan_id,
                    plan_digest,
                    approval_id,
                    request_id,
                    str(plan[0]),
                    int(plan[1]),
                    str(plan[2]),
                    int(plan[3]),
                    now_text,
                    now_text,
                    None,
                    uri,
                ),
            )
            connection.execute(
                "INSERT INTO approval_uses(approval_id,owner_id,plan_id,plan_digest,job_id,used_at) VALUES(?,?,?,?,?,?)",
                (approval_id, owner_id, plan_id, plan_digest, job_id, now_text),
            )
            items = connection.execute(
                "SELECT item_id,ordinal,kind,source_type,source_url,version,checksum,size_bytes,target_dir,restart_required FROM dependency_plan_items WHERE plan_id=? ORDER BY ordinal",
                (plan_id,),
            ).fetchall()
            connection.executemany(
                "INSERT INTO provisioning_install_items(job_id,owner_id,item_id,ordinal,kind,source_type,source_url,version,checksum,size_bytes,target_dir,restart_required,idempotency_key,status,current_checkpoint_json,current_checkpoint_digest,updated_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending','{}',NULL,?,NULL)",
                [
                    (
                        job_id,
                        owner_id,
                        str(item[0]),
                        int(item[1]),
                        str(item[2]),
                        str(item[3]),
                        str(item[4]),
                        str(item[5]),
                        str(item[6]),
                        int(item[7]),
                        str(item[8]),
                        int(item[9]),
                        _stable("manager_install", job_id, str(item[0])),
                        now_text,
                    )
                    for item in items
                ],
            )
            work_id = _stable("work", job_id, "provisioning.execute")
            payload = {"job_id": job_id, "owner_id": owner_id}
            connection.execute(
                "INSERT INTO operation_work_items(work_item_id,subject_uri,work_type,payload_json,checkpoint_json,status,next_attempt_at,created_at,updated_at) VALUES(?,?,'provisioning.execute',?,'{}','pending',?,?,?)",
                (work_id, uri, _json(payload), now_text, now_text, now_text),
            )
            self._event(
                connection,
                owner_id,
                "PROVISIONING_COMMITTED",
                uri,
                plan_id,
                {"job_id": job_id, "status": "pending"},
                now_text,
            )
            result = self._get_job(connection, job_id, owner_id)
            if result is None:
                raise RuntimeError("provisioning commit failed")
            return result

    def get_job(self, job_id: str, owner_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            return self._get_job(connection, job_id, owner_id)
        finally:
            connection.close()

    def get_work_context(self, job_id: str, owner_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            job = self._get_job(connection, job_id, owner_id)
            if job is None:
                raise LookupError("provisioning job was not found")
            row = connection.execute(
                "SELECT config_json,lifecycle_status FROM server_revisions WHERE server_id=? "
                "AND owner_id=? AND revision=? AND config_digest=?",
                (job["server_id"], owner_id, job["server_revision"], job["server_config_digest"]),
            ).fetchone()
            if row is None or str(row[1]) == "deleted":
                raise LookupError("provisioning Server revision was not found")
            config = _object(str(row[0]), field="provisioning Server config")
            return {**job, "server": {"server_id": job["server_id"], **config}}
        finally:
            connection.close()

    def renew_lease(self, lease: WorkLease, *, now: datetime, lease_seconds: int = 30) -> WorkLease:
        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be between 1 and 300")
        now_text = _time(now)
        expires = _time(_aware(now) + timedelta(seconds=lease_seconds))
        with self._transaction() as connection:
            self._require_lease(connection, lease, now_text)
            changed = connection.execute(
                "UPDATE work_leases SET expires_at=? WHERE work_item_id=? AND worker_id=? AND fencing_token=?",
                (expires, lease.work_item_id, lease.worker_id, lease.fencing_token),
            ).rowcount
            if changed != 1:
                raise RuntimeError("work lease is expired or fenced")
        return WorkLease(lease.work_item_id, lease.worker_id, lease.fencing_token, expires)

    def release_lease(self, lease: WorkLease, *, now: datetime) -> None:
        now_text = _time(now)
        with self._transaction() as connection:
            self._require_lease(connection, lease, now_text)
            connection.execute(
                "UPDATE operation_work_items SET status='pending',updated_at=?,next_attempt_at=? WHERE work_item_id=? AND status='running'",
                (now_text, now_text, lease.work_item_id),
            )
            connection.execute(
                "UPDATE work_leases SET expires_at=? WHERE work_item_id=? AND worker_id=? AND fencing_token=?",
                (now_text, lease.work_item_id, lease.worker_id, lease.fencing_token),
            )

    def claim_item_for_enqueue(
        self,
        lease: WorkLease,
        *,
        job_id: str,
        owner_id: str,
        item_id: str,
        queue_id: str,
        now: datetime,
    ) -> dict[str, Any] | None:
        if not isinstance(queue_id, str) or not 1 <= len(queue_id) <= 128:
            raise ValueError("queue_id must be a bounded string")
        now_text = _time(now)
        with self._transaction() as connection:
            self._require_job_lease(connection, lease, job_id, owner_id, now_text)
            row = connection.execute(
                "SELECT status FROM provisioning_install_items WHERE job_id=? AND item_id=? AND owner_id=?",
                (job_id, item_id, owner_id),
            ).fetchone()
            if row is None:
                raise LookupError("provisioning item was not found")
            if str(row[0]) not in {"pending", "enqueuing"}:
                return None
            connection.execute(
                "UPDATE provisioning_install_items SET status='enqueuing',lease_work_item_id=?,lease_worker_id=?,lease_token=?,updated_at=? WHERE job_id=? AND item_id=? AND owner_id=?",
                (
                    lease.work_item_id,
                    lease.worker_id,
                    lease.fencing_token,
                    now_text,
                    job_id,
                    item_id,
                    owner_id,
                ),
            )
            connection.execute(
                "UPDATE provisioning_jobs SET status='running',updated_at=? WHERE job_id=? AND owner_id=? AND status='pending'",
                (now_text, job_id, owner_id),
            )
            self._append_checkpoint(
                connection,
                job_id,
                item_id,
                owner_id,
                "enqueuing",
                {"enqueue_started": True, "queue_id": queue_id, "state": "enqueue_started"},
                now_text,
            )
            return self._work_item(connection, job_id, item_id, owner_id)

    def save_item_checkpoint(
        self,
        lease: WorkLease,
        *,
        job_id: str,
        owner_id: str,
        item_id: str,
        checkpoint: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        value = require_public_json(
            checkpoint, field="provisioning checkpoint", max_bytes=MAX_CHECKPOINT_BYTES
        )
        state = str(value.get("state", "unknown"))
        with self._transaction() as connection:
            self._require_job_lease(connection, lease, job_id, owner_id, _time(now))
            if state in {"queued", "running"}:
                status = state
            else:
                current = connection.execute(
                    "SELECT status FROM provisioning_install_items WHERE job_id=? AND item_id=? AND owner_id=?",
                    (job_id, item_id, owner_id),
                ).fetchone()
                if current is None:
                    raise LookupError("provisioning item was not found")
                status = (
                    str(current[0])
                    if str(current[0]) in {"enqueuing", "queued", "running"}
                    else "enqueuing"
                )
            self._append_checkpoint(
                connection, job_id, item_id, owner_id, status, value, _time(now)
            )
            result = self._work_item(connection, job_id, item_id, owner_id)
            if result is None:
                raise LookupError("provisioning item was not found")
            return result

    def complete_item(
        self,
        lease: WorkLease,
        *,
        job_id: str,
        owner_id: str,
        item_id: str,
        result: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        value = require_public_json(result, field="provisioning result", max_bytes=MAX_RESULT_BYTES)
        status = str(value.get("state", ""))
        if status not in _TERMINAL_ITEMS:
            raise ValueError("provisioning result must be terminal")
        now_text = _time(now)
        with self._transaction() as connection:
            self._require_job_lease(connection, lease, job_id, owner_id, now_text)
            self._append_checkpoint(connection, job_id, item_id, owner_id, status, value, now_text)
            self._event(
                connection,
                owner_id,
                "PROVISIONING_ITEM_UPDATED",
                f"comfyui://provisioning/jobs/{job_id}",
                item_id,
                {"job_id": job_id, "item_id": item_id, "status": status},
                now_text,
            )
            result_item = self._work_item(connection, job_id, item_id, owner_id)
            if result_item is None:
                raise LookupError("provisioning item was not found")
            return result_item

    def finish_work(
        self,
        lease: WorkLease,
        *,
        job_id: str,
        owner_id: str,
        checkpoint: dict[str, Any],
        now: datetime,
        completed: bool,
        delay_seconds: int,
        status: str,
    ) -> None:
        if (
            delay_seconds < 0
            or status not in {"running", "completed", "failed", "cancelled"}
            or completed != (status in _TERMINAL_JOBS)
        ):
            raise ValueError("provisioning work completion conflicts")
        value = require_public_json(
            checkpoint, field="provisioning work checkpoint", max_bytes=MAX_CHECKPOINT_BYTES
        )
        now_text = _time(now)
        next_attempt = _time(_aware(now) + timedelta(seconds=delay_seconds))
        with self._transaction() as connection:
            self._require_job_lease(connection, lease, job_id, owner_id, now_text)
            previous = connection.execute(
                "SELECT status FROM provisioning_jobs WHERE job_id=? AND owner_id=?",
                (job_id, owner_id),
            ).fetchone()
            if previous is None:
                raise LookupError("provisioning job was not found")
            connection.execute(
                "UPDATE provisioning_jobs SET status=?,updated_at=?,completed_at=? WHERE job_id=? AND owner_id=?",
                (status, now_text, now_text if completed else None, job_id, owner_id),
            )
            connection.execute(
                "UPDATE operation_work_items SET checkpoint_json=?,status=?,next_attempt_at=?,updated_at=? WHERE work_item_id=?",
                (
                    _json(value),
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
            if str(previous[0]) != status:
                self._event(
                    connection,
                    owner_id,
                    "PROVISIONING_UPDATED",
                    f"comfyui://provisioning/jobs/{job_id}",
                    lease.work_item_id,
                    {"job_id": job_id, "status": status},
                    now_text,
                )

    def save_cancel_plan(self, plan: dict[str, Any]) -> None:
        value = _cancel_plan(plan)
        row = (
            value["cancel_plan_id"],
            value["plan_digest"],
            value["owner_id"],
            value["job_id"],
            _json(value["impact"]),
            value["created_at"],
            value["expires_at"],
            value["resource_uri"],
        )
        with self._transaction() as connection:
            if self._get_job(connection, value["job_id"], value["owner_id"]) is None:
                raise LookupError("provisioning job was not found")
            existing = connection.execute(
                "SELECT cancel_plan_id,plan_digest,owner_id,job_id,impact_json,created_at,expires_at,resource_uri FROM provisioning_cancel_plans WHERE cancel_plan_id=?",
                (value["cancel_plan_id"],),
            ).fetchone()
            if existing is not None and tuple(existing) != row:
                raise ValueError("provisioning cancel plan identity conflict")
            if existing is None:
                connection.execute(
                    "INSERT INTO provisioning_cancel_plans(cancel_plan_id,plan_digest,owner_id,job_id,impact_json,created_at,expires_at,resource_uri) VALUES(?,?,?,?,?,?,?,?)",
                    row,
                )

    def commit_cancel_plan(
        self, cancel_plan_id: str, plan_digest: str, owner_id: str, *, now: datetime
    ) -> dict[str, Any]:
        now_text = _time(now)
        with self._transaction() as connection:
            plan = connection.execute(
                "SELECT job_id,impact_json,expires_at FROM provisioning_cancel_plans WHERE cancel_plan_id=? AND plan_digest=? AND owner_id=?",
                (cancel_plan_id, plan_digest, owner_id),
            ).fetchone()
            if plan is None:
                raise LookupError("provisioning cancel plan was not found")
            job_id = str(plan[0])
            existing = connection.execute(
                "SELECT 1 FROM provisioning_cancel_commits WHERE cancel_plan_id=?",
                (cancel_plan_id,),
            ).fetchone()
            if existing is None:
                if _parse_time(str(plan[2])) <= _aware(now):
                    raise ValueError("provisioning cancel plan is expired")
                impact = _object(str(plan[1]), field="provisioning cancel impact")
                current_job = self._get_job(connection, job_id, owner_id)
                if (
                    current_job is None
                    or current_job.get("status") != impact.get("job_status")
                    or current_job.get("updated_at") != impact.get("job_updated_at")
                    or sum(
                        1
                        for item in current_job.get("items", [])
                        if isinstance(item, dict) and item.get("status") == "pending"
                    )
                    != impact.get("pending_items")
                    or sum(
                        1
                        for item in current_job.get("items", [])
                        if isinstance(item, dict)
                        and item.get("status") in {"enqueuing", "queued", "running"}
                    )
                    != impact.get("enqueued_items_unchanged")
                ):
                    raise ValueError("provisioning cancel plan conflicts with current Job state")
                connection.execute(
                    "UPDATE provisioning_install_items SET status='cancelled',completed_at=?,updated_at=? WHERE job_id=? AND owner_id=? AND status='pending'",
                    (now_text, now_text, job_id, owner_id),
                )
                active = int(
                    connection.execute(
                        "SELECT count(*) FROM provisioning_install_items WHERE job_id=? AND owner_id=? AND status NOT IN ('completed','failed','cancelled')",
                        (job_id, owner_id),
                    ).fetchone()[0]
                )
                if active == 0:
                    connection.execute(
                        "UPDATE provisioning_jobs SET status='cancelled',completed_at=?,updated_at=? WHERE job_id=? AND owner_id=? AND status IN ('pending','running')",
                        (now_text, now_text, job_id, owner_id),
                    )
                    connection.execute(
                        "UPDATE operation_work_items SET status='completed',updated_at=? WHERE subject_uri=? AND work_type='provisioning.execute'",
                        (now_text, f"comfyui://provisioning/jobs/{job_id}"),
                    )
                connection.execute(
                    "INSERT INTO provisioning_cancel_commits(cancel_plan_id,plan_digest,owner_id,job_id,committed_at) VALUES(?,?,?,?,?)",
                    (cancel_plan_id, plan_digest, owner_id, job_id, now_text),
                )
                self._event(
                    connection,
                    owner_id,
                    "PROVISIONING_CANCELLED",
                    f"comfyui://provisioning/jobs/{job_id}",
                    cancel_plan_id,
                    {"job_id": job_id, "pending_items_cancelled": True},
                    now_text,
                )
            result = self._get_job(connection, job_id, owner_id)
            if result is None:
                raise RuntimeError("cancelled provisioning job is missing")
            return result

    def owner_for_uri(self, uri: str) -> str | None:
        connection = self._connect()
        try:
            if uri.startswith("comfyui://servers/"):
                row = connection.execute(
                    "SELECT owner_id FROM managed_servers WHERE server_id=?",
                    (uri.rsplit("/", 1)[-1],),
                ).fetchone()
            elif uri.startswith("comfyui://dependencies/plans/"):
                row = connection.execute(
                    "SELECT owner_id FROM dependency_plans WHERE plan_id=?",
                    (uri.rsplit("/", 1)[-1],),
                ).fetchone()
            elif uri.startswith("comfyui://approvals/"):
                row = connection.execute(
                    "SELECT owner_id FROM approvals WHERE approval_id=?", (uri.rsplit("/", 1)[-1],)
                ).fetchone()
            elif uri.startswith("comfyui://provisioning/jobs/"):
                row = connection.execute(
                    "SELECT owner_id FROM provisioning_jobs WHERE job_id=?",
                    (uri.rsplit("/", 1)[-1],),
                ).fetchone()
            elif uri.startswith("comfyui://config/bundles/"):
                try:
                    revision = int(uri.rsplit("/", 1)[-1])
                except ValueError:
                    return None
                rows = connection.execute(
                    "SELECT DISTINCT owner_id FROM config_bundles WHERE revision=? LIMIT 2",
                    (revision,),
                ).fetchall()
                return str(rows[0][0]) if len(rows) == 1 else None
            else:
                return None
            return None if row is None else str(row[0])
        finally:
            connection.close()

    def pending_outbox(
        self, owner_id: str | None = None, *, limit: int = 100
    ) -> list[OutboxMessage]:
        if isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("outbox limit must be between 1 and 1000")
        connection = self._connect()
        try:
            if owner_id is None:
                rows = connection.execute(
                    "SELECT outbox_id,topic,payload_json FROM phase_o_outbox WHERE status='pending' ORDER BY created_at,outbox_id LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT outbox_id,topic,payload_json FROM phase_o_outbox WHERE status='pending' AND owner_id=? ORDER BY created_at,outbox_id LIMIT ?",
                    (owner_id, limit),
                ).fetchall()
            return [
                OutboxMessage(
                    str(row[0]), str(row[1]), _object(str(row[2]), field="outbox payload")
                )
                for row in rows
            ]
        finally:
            connection.close()

    def mark_outbox_delivered(self, outbox_id: str, *, now: datetime) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status FROM phase_o_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            if row is None:
                raise LookupError("Phase O outbox message was not found")
            if str(row[0]) == "pending":
                connection.execute(
                    "UPDATE phase_o_outbox SET status='delivered',delivered_at=? "
                    "WHERE outbox_id=? AND status='pending'",
                    (_time(now), outbox_id),
                )

    def _advance_config_state(
        self,
        connection: sqlite3.Connection,
        owner_id: str,
        updated_at: str,
    ) -> int:
        row = connection.execute(
            "SELECT current_revision FROM config_state WHERE owner_id=?",
            (owner_id,),
        ).fetchone()
        revision = 1 if row is None else int(row[0]) + 1
        workflows = connection.execute(
            "SELECT server_id,workflow_id,enabled FROM config_workflow_states "
            "WHERE owner_id=? ORDER BY server_id,workflow_id",
            (owner_id,),
        ).fetchall()
        facts = {
            "servers": [
                tuple(item)
                for item in connection.execute(
                    "SELECT server_id,current_revision,current_digest,lifecycle_status "
                    "FROM managed_servers WHERE owner_id=? ORDER BY server_id",
                    (owner_id,),
                ).fetchall()
            ],
            "default_server": connection.execute(
                "SELECT server_id FROM server_defaults WHERE owner_id=?",
                (owner_id,),
            ).fetchone(),
            "workflows": [tuple(item) for item in workflows],
        }
        digest = canonical_digest(facts)
        connection.execute(
            "INSERT INTO config_state(owner_id,current_revision,current_digest,updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(owner_id) DO UPDATE SET "
            "current_revision=excluded.current_revision,current_digest=excluded.current_digest,"
            "updated_at=excluded.updated_at",
            (owner_id, revision, digest, updated_at),
        )
        return revision

    def _apply_config_content(
        self,
        connection: sqlite3.Connection,
        owner_id: str,
        content: dict[str, Any],
        *,
        import_plan_id: str,
        created_at: str,
        expires_at: str,
    ) -> None:
        raw_servers = content.get("servers")
        raw_workflows = content.get("workflows")
        if not isinstance(raw_servers, list) or not isinstance(raw_workflows, list):
            raise ValueError("config import content is invalid")
        incoming = {
            str(item.get("server_id")): dict(item)
            for item in raw_servers
            if isinstance(item, dict) and item.get("server_id")
        }
        if len(incoming) != len(raw_servers) or len(incoming) > 64:
            raise ValueError("config import Server identities conflict")
        existing_ids = {
            str(row[0])
            for row in connection.execute(
                "SELECT server_id FROM managed_servers WHERE owner_id=? AND lifecycle_status!='deleted'",
                (owner_id,),
            ).fetchall()
        }
        revision_by_server: dict[str, int] = {}
        plan_by_server: dict[str, str] = {}
        for server_id in sorted(existing_ids | set(incoming)):
            current = self._server_row(connection, server_id, owner_id)
            current_revision = 0 if current is None else int(current[0])
            item = incoming.get(server_id)
            if item is None:
                if current is None:
                    raise RuntimeError("existing Server revision is missing")
                config = _object(str(current[3]), field="server config")
                lifecycle = "deleted"
                operation = "delete"
            else:
                config = {
                    "url": item.get("endpoint_url"),
                    "enabled": bool(item.get("enabled", True)),
                }
                if item.get("display_name") is not None:
                    config["name"] = item["display_name"]
                if item.get("secret_refs") is not None:
                    config["secret_refs"] = item["secret_refs"]
                lifecycle = "active" if config["enabled"] else "disabled"
                operation = "upsert"
            server_plan_id = _stable("config_import_server", import_plan_id, server_id)
            server_plan_digest = canonical_digest(
                [import_plan_id, server_id, current_revision, operation, config]
            )
            connection.execute(
                "INSERT INTO server_change_plans(plan_id,plan_digest,owner_id,operation,server_id,"
                "changes_json,expected_revision,impact_json,created_at,expires_at,resource_uri) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    server_plan_id,
                    server_plan_digest,
                    owner_id,
                    operation,
                    server_id,
                    _json(config),
                    current_revision,
                    "{}",
                    created_at,
                    expires_at,
                    f"comfyui://servers/{server_id}",
                ),
            )
            revision = current_revision + 1
            config_digest = canonical_digest(config)
            connection.execute(
                "INSERT INTO server_revisions(server_id,owner_id,revision,lifecycle_status,"
                "config_json,config_digest,plan_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    server_id,
                    owner_id,
                    revision,
                    lifecycle,
                    _json(config),
                    config_digest,
                    server_plan_id,
                    created_at,
                ),
            )
            if current is None:
                connection.execute(
                    "INSERT INTO managed_servers(server_id,owner_id,current_revision,current_digest,"
                    "lifecycle_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        server_id,
                        owner_id,
                        revision,
                        config_digest,
                        lifecycle,
                        created_at,
                        created_at,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE managed_servers SET current_revision=?,current_digest=?,"
                    "lifecycle_status=?,updated_at=? WHERE server_id=? AND owner_id=?",
                    (revision, config_digest, lifecycle, created_at, server_id, owner_id),
                )
            revision_by_server[server_id] = revision
            plan_by_server[server_id] = server_plan_id
        default_server = content.get("default_server")
        connection.execute("DELETE FROM server_defaults WHERE owner_id=?", (owner_id,))
        if default_server is not None:
            default_id = str(default_server)
            if default_id not in incoming or not bool(incoming[default_id].get("enabled", True)):
                raise ValueError("config import default Server is invalid")
            connection.execute(
                "INSERT INTO server_defaults(owner_id,server_id,server_revision,plan_id,updated_at) "
                "VALUES(?,?,?,?,?)",
                (
                    owner_id,
                    default_id,
                    revision_by_server[default_id],
                    plan_by_server[default_id],
                    created_at,
                ),
            )
        connection.execute(
            "INSERT INTO config_workflow_snapshots(owner_id,updated_at) VALUES(?,?) "
            "ON CONFLICT(owner_id) DO UPDATE SET updated_at=excluded.updated_at",
            (owner_id, created_at),
        )
        connection.execute("DELETE FROM config_workflow_states WHERE owner_id=?", (owner_id,))
        for item in raw_workflows:
            if not isinstance(item, dict):
                raise ValueError("config import Workflow is invalid")
            exists = connection.execute(
                "SELECT 1 FROM config_workflow_deployments WHERE owner_id=? "
                "AND server_id=? AND workflow_id=?",
                (owner_id, item.get("server_id"), item.get("workflow_id")),
            ).fetchone()
            if exists is None:
                raise ValueError("config import Workflow deployment was not found")
            connection.execute(
                "INSERT INTO config_workflow_states(owner_id,server_id,workflow_id,enabled,updated_at) "
                "VALUES(?,?,?,?,?)",
                (
                    owner_id,
                    item.get("server_id"),
                    item.get("workflow_id"),
                    int(bool(item.get("enabled"))),
                    created_at,
                ),
            )

    def _server_row(
        self, connection: sqlite3.Connection, server_id: str, owner_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT managed_servers.current_revision,managed_servers.current_digest,"
            "managed_servers.lifecycle_status,revisions.config_json FROM managed_servers "
            "JOIN server_revisions AS revisions ON "
            "revisions.server_id=managed_servers.server_id AND "
            "revisions.owner_id=managed_servers.owner_id AND "
            "revisions.revision=managed_servers.current_revision "
            "WHERE managed_servers.server_id=? AND managed_servers.owner_id=?",
            (server_id, owner_id),
        ).fetchone()

    def _get_server(
        self,
        connection: sqlite3.Connection,
        server_id: str,
        owner_id: str,
        *,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        row = self._server_row(connection, server_id, owner_id)
        if row is None or (str(row[2]) == "deleted" and not include_deleted):
            return None
        config = _object(str(row[3]), field="server config")
        default = (
            connection.execute(
                "SELECT 1 FROM server_defaults WHERE owner_id=? AND server_id=?",
                (owner_id, server_id),
            ).fetchone()
            is not None
        )
        return {
            "server_id": server_id,
            "owner_id": owner_id,
            **config,
            "revision": int(row[0]),
            "config_digest": str(row[1]),
            "status": str(row[2]),
            "is_default": default,
            "resource_uri": f"comfyui://servers/{server_id}",
        }

    def _get_plan(
        self, connection: sqlite3.Connection, plan_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT plan_id,plan_digest,owner_id,server_id,server_revision,server_config_digest,inspection_digest,restart_required,request_confirmation,created_at,expires_at,resource_uri FROM dependency_plans WHERE plan_id=? AND owner_id=?",
            (plan_id, owner_id),
        ).fetchone()
        if row is None:
            return None
        items = connection.execute(
            "SELECT item_id,dependency_id,kind,source_type,source_url,version,checksum,size_bytes,target_dir,license,restart_required,install_state FROM dependency_plan_items WHERE plan_id=? ORDER BY ordinal",
            (plan_id,),
        ).fetchall()
        result = {
            "plan_id": str(row[0]),
            "plan_digest": str(row[1]),
            "owner_id": str(row[2]),
            "server_id": str(row[3]),
            "server_revision": int(row[4]),
            "server_config_digest": str(row[5]),
            "inspection_digest": str(row[6]),
            "items": [_item_projection(item) for item in items],
            "restart_required": bool(row[7]),
            "request_confirmation": str(row[8]),
            "created_at": str(row[9]),
            "expires_at": str(row[10]),
            "resource_uri": str(row[11]),
        }
        approval = connection.execute(
            "SELECT approval_id FROM approvals WHERE plan_id=? AND owner_id=?", (plan_id, owner_id)
        ).fetchone()
        if approval is not None:
            result["approval_id"] = str(approval[0])
            result["approval_uri"] = f"comfyui://approvals/{approval[0]}"
        return result

    def _get_approval(
        self, connection: sqlite3.Connection, approval_id: str, owner_id: str, *, now: datetime
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT approval_id,owner_id,operation,plan_id,plan_digest,impact_summary_json,single_use,revision,created_at,expires_at,resource_uri FROM approvals WHERE approval_id=? AND owner_id=?",
            (approval_id, owner_id),
        ).fetchone()
        if row is None:
            return None
        decision = connection.execute(
            "SELECT decision,reason,decided_at FROM approval_decisions WHERE approval_id=? AND owner_id=?",
            (approval_id, owner_id),
        ).fetchone()
        used = connection.execute(
            "SELECT job_id,used_at FROM approval_uses WHERE approval_id=? AND owner_id=?",
            (approval_id, owner_id),
        ).fetchone()
        if used is not None:
            status = "used"
        elif decision is not None:
            status = str(decision[0])
        elif _parse_time(str(row[9])) <= now:
            status = "expired"
        else:
            status = "pending"
        result = {
            "approval_id": str(row[0]),
            "owner_id": str(row[1]),
            "operation": str(row[2]),
            "plan_id": str(row[3]),
            "plan_digest": str(row[4]),
            "impact_summary": _object(str(row[5]), field="approval impact"),
            "single_use": bool(row[6]),
            "revision": 0 if decision is None else 1,
            "status": status,
            "created_at": str(row[8]),
            "expires_at": str(row[9]),
            "resource_uri": str(row[10]),
        }
        if decision is not None:
            result.update(
                {
                    "decision": str(decision[0]),
                    "reason": str(decision[1]),
                    "decided_at": str(decision[2]),
                }
            )
        if used is not None:
            result.update({"job_id": str(used[0]), "used_at": str(used[1])})
        return result

    def _get_job(
        self, connection: sqlite3.Connection, job_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT job_id,owner_id,plan_id,plan_digest,approval_id,request_id,server_id,server_revision,server_config_digest,status,restart_required,created_at,updated_at,completed_at,resource_uri FROM provisioning_jobs WHERE job_id=? AND owner_id=?",
            (job_id, owner_id),
        ).fetchone()
        if row is None:
            return None
        items = connection.execute(
            "SELECT item_id,kind,source_type,source_url,version,checksum,size_bytes,target_dir,restart_required,status,current_checkpoint_json,idempotency_key FROM provisioning_install_items WHERE job_id=? AND owner_id=? ORDER BY ordinal",
            (job_id, owner_id),
        ).fetchall()
        return {
            "job_id": str(row[0]),
            "owner_id": str(row[1]),
            "plan_id": str(row[2]),
            "plan_digest": str(row[3]),
            "approval_id": str(row[4]),
            "request_id": str(row[5]),
            "server_id": str(row[6]),
            "server_revision": int(row[7]),
            "server_config_digest": str(row[8]),
            "status": str(row[9]),
            "restart_required": bool(row[10]),
            "created_at": str(row[11]),
            "updated_at": str(row[12]),
            "completed_at": row[13],
            "resource_uri": str(row[14]),
            "items": [
                {
                    "item_id": str(item[0]),
                    "kind": str(item[1]),
                    "source_type": str(item[2]),
                    "source_url": str(item[3]),
                    "version": str(item[4]),
                    "checksum": str(item[5]),
                    "size_bytes": int(item[6]),
                    "target_dir": str(item[7]),
                    "restart_required": bool(item[8]),
                    "status": str(item[9]),
                    "checkpoint": _object(str(item[10]), field="provisioning checkpoint"),
                    "idempotency_key": str(item[11]),
                }
                for item in items
            ],
        }

    def _work_item(
        self, connection: sqlite3.Connection, job_id: str, item_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT item_id,kind,source_type,source_url,version,checksum,size_bytes,target_dir,restart_required,status,current_checkpoint_json,idempotency_key FROM provisioning_install_items WHERE job_id=? AND item_id=? AND owner_id=?",
            (job_id, item_id, owner_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "item_id": str(row[0]),
            "kind": str(row[1]),
            "source_type": str(row[2]),
            "source_url": str(row[3]),
            "version": str(row[4]),
            "checksum": str(row[5]),
            "size_bytes": int(row[6]),
            "target_dir": str(row[7]),
            "restart_required": bool(row[8]),
            "status": str(row[9]),
            "checkpoint": _object(str(row[10]), field="provisioning checkpoint"),
            "idempotency_key": str(row[11]),
        }

    def _append_checkpoint(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        item_id: str,
        owner_id: str,
        status: str,
        checkpoint: dict[str, Any],
        now_text: str,
    ) -> None:
        digest = canonical_digest(checkpoint)
        existing = connection.execute(
            "SELECT 1 FROM provisioning_item_checkpoints WHERE job_id=? AND item_id=? AND checkpoint_digest=?",
            (job_id, item_id, digest),
        ).fetchone()
        if existing is None:
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(max(sequence),0)+1 FROM provisioning_item_checkpoints WHERE job_id=? AND item_id=?",
                    (job_id, item_id),
                ).fetchone()[0]
            )
            checkpoint_id = _stable(
                "provisioning_checkpoint", job_id, item_id, str(sequence), digest
            )
            connection.execute(
                "INSERT INTO provisioning_item_checkpoints(checkpoint_id,job_id,owner_id,item_id,sequence,status,checkpoint_json,checkpoint_digest,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    checkpoint_id,
                    job_id,
                    owner_id,
                    item_id,
                    sequence,
                    status,
                    _json(checkpoint),
                    digest,
                    now_text,
                ),
            )
        terminal = status in _TERMINAL_ITEMS
        connection.execute(
            "UPDATE provisioning_install_items SET status=?,current_checkpoint_json=?,current_checkpoint_digest=?,updated_at=?,completed_at=? WHERE job_id=? AND item_id=? AND owner_id=?",
            (
                status,
                _json(checkpoint),
                digest,
                now_text,
                now_text if terminal else None,
                job_id,
                item_id,
                owner_id,
            ),
        )

    def _require_lease(
        self, connection: sqlite3.Connection, lease: WorkLease, now_text: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT work.subject_uri,work.payload_json FROM work_leases leases JOIN operation_work_items work USING(work_item_id) WHERE leases.work_item_id=? AND leases.worker_id=? AND leases.fencing_token=? AND julianday(leases.expires_at)>julianday(?) AND work.work_type='provisioning.execute' AND work.status='running'",
            (lease.work_item_id, lease.worker_id, lease.fencing_token, now_text),
        ).fetchone()
        if row is None:
            raise RuntimeError("work lease is expired or fenced")
        return row

    def _require_job_lease(
        self,
        connection: sqlite3.Connection,
        lease: WorkLease,
        job_id: str,
        owner_id: str,
        now_text: str,
    ) -> None:
        row = self._require_lease(connection, lease, now_text)
        if str(row[0]) != f"comfyui://provisioning/jobs/{job_id}" or _object(
            str(row[1]), field="work payload"
        ) != {"job_id": job_id, "owner_id": owner_id}:
            raise RuntimeError("provisioning work lease ownership conflict")

    def _event(
        self,
        connection: sqlite3.Connection,
        owner_id: str,
        event_type: str,
        uri: str,
        correlation_id: str,
        data: dict[str, Any],
        occurred_at: str,
    ) -> None:
        public = require_public_json(data, field="audit data", max_bytes=MAX_RESULT_BYTES)
        digest = canonical_digest(public)
        sequence = int(
            connection.execute(
                "SELECT COALESCE(max(sequence),0)+1 FROM phase_o_audit_events WHERE subject_uri=?",
                (uri,),
            ).fetchone()[0]
        )
        event_id = _stable("phase_o_event", uri, str(sequence), event_type, digest)
        outbox_id = _stable("phase_o_outbox", event_id)
        payload = {
            "uri": uri,
            "owner_id": owner_id,
            "event_type": event_type,
            "sequence": sequence,
        }
        connection.execute(
            "INSERT INTO phase_o_audit_events(event_id,owner_id,event_type,subject_uri,sequence,correlation_id,data_json,data_digest,occurred_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                owner_id,
                event_type,
                uri,
                sequence,
                correlation_id,
                _json(public),
                digest,
                occurred_at,
            ),
        )
        connection.execute(
            "INSERT INTO phase_o_outbox(outbox_id,event_id,owner_id,topic,payload_json,payload_digest,status,created_at,delivered_at) VALUES(?,?,?,'resource.updated',?,?,'pending',?,NULL)",
            (outbox_id, event_id, owner_id, _json(payload), canonical_digest(payload), occurred_at),
        )

    def _connect(self) -> sqlite3.Connection:
        return self._store._connect()

    def _transaction(self) -> _Transaction:
        return _Transaction(self._connect())


class _Transaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
        except BaseException:
            self.connection.close()
            raise
        return self.connection

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> Literal[False]:
        try:
            self.connection.commit() if exc_type is None else self.connection.rollback()
        finally:
            self.connection.close()
        return False


def _server_plan(plan: dict[str, Any]) -> dict[str, Any]:
    value = require_public_json(plan, field="server plan", max_bytes=MAX_PLAN_BYTES)
    required = {
        "plan_id",
        "plan_digest",
        "owner_id",
        "operation",
        "server_id",
        "changes",
        "expected_revision",
        "impact",
        "created_at",
        "expires_at",
        "resource_uri",
    }
    _fields(value, required, "server plan")
    immutable = {
        key: value[key]
        for key in (
            "owner_id",
            "operation",
            "server_id",
            "changes",
            "expected_revision",
            "impact",
            "created_at",
            "expires_at",
        )
    }
    _digest_matches(value, immutable, "server plan")
    return value


def _bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    value = require_public_json(bundle, field="config bundle", max_bytes=MAX_BUNDLE_BYTES)
    _fields(
        value,
        {
            "bundle_id",
            "owner_id",
            "version",
            "revision",
            "content",
            "content_digest",
            "created_at",
            "resource_uri",
        },
        "config bundle",
    )
    if canonical_digest(value["content"]) != value["content_digest"]:
        raise ValueError("config bundle digest conflict")
    return value


def _import_plan(plan: dict[str, Any]) -> dict[str, Any]:
    value = require_public_json(plan, field="config import plan", max_bytes=MAX_PLAN_BYTES)
    _fields(
        value,
        {
            "plan_id",
            "plan_digest",
            "owner_id",
            "expected_revision",
            "bundle_version",
            "source_digest",
            "content",
            "merge_summary",
            "created_at",
            "expires_at",
            "resource_uri",
        },
        "config import plan",
    )
    immutable = {
        key: value[key]
        for key in (
            "owner_id",
            "expected_revision",
            "bundle_version",
            "source_digest",
            "content",
            "merge_summary",
            "created_at",
            "expires_at",
        )
    }
    _digest_matches(value, immutable, "config import plan")
    if canonical_digest(value["content"]) != value["source_digest"]:
        raise ValueError("config import source digest conflict")
    return value


def _dependency_plan(
    plan: dict[str, Any], items: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    value = require_public_json(plan, field="dependency plan", max_bytes=MAX_PLAN_BYTES)
    _fields(
        value,
        {
            "plan_id",
            "plan_digest",
            "owner_id",
            "server_id",
            "inspection_digest",
            "server_revision",
            "server_config_digest",
            "items",
            "restart_required",
            "request_confirmation",
            "created_at",
            "expires_at",
            "resource_uri",
        },
        "dependency plan",
    )
    normalized = validate_dependency_items(items)
    if value["items"] != normalized:
        raise ValueError("dependency plan items conflict")
    immutable = {
        key: value[key]
        for key in (
            "owner_id",
            "server_id",
            "inspection_digest",
            "server_revision",
            "server_config_digest",
            "items",
            "restart_required",
            "request_confirmation",
            "created_at",
            "expires_at",
        )
    }
    _digest_matches(value, immutable, "dependency plan")
    return value, normalized


def _approval_plan(plan: dict[str, Any]) -> dict[str, Any]:
    value = require_public_json(plan, field="approval decision plan", max_bytes=MAX_PLAN_BYTES)
    _fields(
        value,
        {
            "approval_plan_id",
            "plan_digest",
            "approval_id",
            "owner_id",
            "decision",
            "reason",
            "approval_revision",
            "status_before",
            "created_at",
            "expires_at",
            "resource_uri",
        },
        "approval decision plan",
    )
    immutable = {
        key: value[key]
        for key in (
            "approval_id",
            "owner_id",
            "decision",
            "reason",
            "approval_revision",
            "status_before",
            "created_at",
            "expires_at",
        )
    }
    _digest_matches(value, immutable, "approval decision plan")
    return value


def _cancel_plan(plan: dict[str, Any]) -> dict[str, Any]:
    value = require_public_json(plan, field="provisioning cancel plan", max_bytes=MAX_PLAN_BYTES)
    _fields(
        value,
        {
            "cancel_plan_id",
            "plan_digest",
            "owner_id",
            "job_id",
            "impact",
            "created_at",
            "expires_at",
            "resource_uri",
        },
        "provisioning cancel plan",
    )
    immutable = {
        key: value[key] for key in ("owner_id", "job_id", "impact", "created_at", "expires_at")
    }
    _digest_matches(value, immutable, "provisioning cancel plan")
    return value


def _digest_matches(value: dict[str, Any], immutable: dict[str, Any], field: str) -> None:
    require_sha256(value.get("plan_digest"), field=f"{field} digest")
    if canonical_digest(immutable) != value["plan_digest"]:
        raise ValueError(f"{field} digest conflict")


def _fields(value: Any, required: set[str], field: str) -> None:
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError(f"{field} fields conflict")


def _bundle_projection(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "bundle_id": str(row[0]),
        "owner_id": str(row[1]),
        "version": int(row[2]),
        "revision": int(row[3]),
        "content": _object(str(row[4]), field="config content"),
        "content_digest": str(row[5]),
        "created_at": str(row[6]),
        "resource_uri": str(row[7]),
    }


def _item_projection(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "item_id": str(row[0]),
        "dependency_id": str(row[1]),
        "kind": str(row[2]),
        "source_type": str(row[3]),
        "source_url": str(row[4]),
        "version": str(row[5]),
        "checksum": str(row[6]),
        "size_bytes": int(row[7]),
        "target_dir": str(row[8]),
        "license": str(row[9]),
        "restart_required": bool(row[10]),
        "install_state": str(row[11]),
    }


def _stable(prefix: str, *parts: str) -> str:
    return f"{prefix}_{canonical_digest([f'phase-o-{prefix}-v1', *parts])}"


def _json(value: object) -> str:
    return canonical_json(value)


def _copy(value: object) -> Any:
    return json.loads(_json(value))


def _object(raw: str, *, field: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _time(value: datetime) -> str:
    return _aware(value).isoformat()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _aware(parsed)
