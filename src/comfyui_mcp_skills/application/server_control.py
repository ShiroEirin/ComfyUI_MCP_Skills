"""Owner-bound two-phase administration of ComfyUI server records."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from comfyui_mcp_skills.application._phase_o import (
    bounded_public,
    bounded_string,
    digest,
    owner,
    require_digest,
    strip_secret_values,
    validate_http_url,
)
from comfyui_mcp_skills.application.provisioning_ports import ServerControlRepository
from comfyui_mcp_skills.domain.identifiers import validate_identifier

_OPERATIONS = frozenset({"upsert", "set_enabled", "set_default", "delete"})
_PLAN_TTL = timedelta(hours=1)
_SERVER_KEYS = frozenset(
    {
        "name",
        "display_name",
        "url",
        "endpoint_url",
        "enabled",
        "output_dir",
        "timeout",
        "secret_refs",
    }
)


class ServerControlService:
    """Plan and atomically commit bounded server configuration mutations."""

    def __init__(
        self,
        repository: ServerControlRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def inspect(self, server_id: str, owner_id: str) -> dict[str, Any]:
        server_id = validate_identifier(server_id, field="server_id")
        owner_id = owner(owner_id)
        result = self._repository.get_server(server_id, owner_id)
        if result is None:
            raise LookupError("Server was not found")
        public = strip_secret_values(result)
        public["resource_uri"] = f"comfyui://servers/{server_id}"
        return bounded_public(public)

    def list(self, owner_id: str, *, limit: int = 50, cursor: str = "") -> dict[str, Any]:
        owner_id = owner(owner_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if not isinstance(cursor, str) or len(cursor) > 32 or (cursor and not cursor.isdigit()):
            raise ValueError("cursor must be a bounded decimal offset")
        offset = int(cursor or "0")
        rows = self._repository.list_servers(owner_id)
        if not isinstance(rows, list) or len(rows) > 10_000:
            raise ValueError("Server repository returned an invalid result")
        rows = sorted(rows, key=lambda item: str(item.get("server_id", "")))
        page = rows[offset : offset + limit]
        items = []
        for row in page:
            public = strip_secret_values(row)
            server_id = validate_identifier(str(public.get("server_id", "")), field="server_id")
            public["resource_uri"] = f"comfyui://servers/{server_id}"
            items.append(public)
        next_offset = offset + len(page)
        return bounded_public(
            {"items": items, "next_cursor": str(next_offset) if next_offset < len(rows) else ""}
        )

    def plan(
        self,
        operation: str,
        server_id: str,
        owner_id: str,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        owner_id = owner(owner_id)
        server_id = validate_identifier(server_id, field="server_id")
        if operation not in _OPERATIONS:
            raise ValueError("operation must be upsert, set_enabled, set_default, or delete")
        if not isinstance(changes, dict):
            raise ValueError("changes must be an object")
        normalized, expected_revision = self._normalize_changes(operation, changes)
        existing = self._repository.get_server(server_id, owner_id)
        if operation != "upsert" and existing is None:
            raise LookupError("Server was not found")
        if expected_revision is None and existing is not None:
            revision = existing.get("revision")
            if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0:
                expected_revision = revision
        if operation == "upsert" and existing is None and expected_revision not in {None, 0}:
            raise ValueError("expected_revision conflicts with a new Server")
        if operation == "upsert" and existing is None and expected_revision is None:
            expected_revision = 0
        impact: dict[str, Any] = {}
        if operation == "delete":
            raw_impact = self._repository.server_delete_impact(server_id, owner_id)
            if not isinstance(raw_impact, Mapping):
                raise ValueError("Server delete impact is invalid")
            impact = bounded_public(raw_impact)
        now = self._clock()
        immutable = {
            "owner_id": owner_id,
            "operation": operation,
            "server_id": server_id,
            "changes": normalized,
            "expected_revision": expected_revision,
            "impact": impact,
            "created_at": _time(now),
            "expires_at": _time(now + _PLAN_TTL),
        }
        plan_digest = digest(immutable)
        plan = {
            "plan_id": "server_plan_" + plan_digest,
            "plan_digest": plan_digest,
            **immutable,
            "resource_uri": f"comfyui://servers/{server_id}",
        }
        saved = self._repository.save_server_plan(plan)
        return bounded_public(saved if isinstance(saved, Mapping) else plan)

    def commit(self, plan_id: str, plan_digest: str, owner_id: str) -> dict[str, Any]:
        plan_id = bounded_string(plan_id, "plan_id", maximum=128)
        plan_digest = require_digest(plan_digest)
        owner_id = owner(owner_id)
        try:
            result = self._repository.commit_server_plan(
                plan_id, plan_digest, owner_id, now=self._clock()
            )
        except LookupError as exc:
            raise LookupError("Server mutation Plan was not found") from exc
        except ValueError as exc:
            raise ValueError(
                "Server mutation Plan conflicts with its owner, digest, or revision"
            ) from exc
        return bounded_public(strip_secret_values(result))

    @staticmethod
    def _normalize_changes(
        operation: str, changes: dict[str, Any]
    ) -> tuple[dict[str, Any], int | None]:
        unknown = set(changes) - (_SERVER_KEYS | {"expected_revision"})
        if unknown:
            raise ValueError("changes contain unsupported Server fields")
        expected = changes.get("expected_revision")
        if expected is not None and (
            isinstance(expected, bool) or not isinstance(expected, int) or expected < 0
        ):
            raise ValueError("expected_revision must be a non-negative integer")
        payload = {key: changes[key] for key in changes if key != "expected_revision"}
        if "display_name" in payload:
            if "name" in payload:
                raise ValueError("use only display_name or name")
            payload["name"] = payload.pop("display_name")
        if "endpoint_url" in payload:
            if "url" in payload:
                raise ValueError("use only endpoint_url or url")
            payload["url"] = payload.pop("endpoint_url")
        if operation == "upsert":
            if "url" not in payload:
                raise ValueError("upsert requires a Server URL")
            if strip_secret_values(payload) != payload:
                raise ValueError("Server credentials must use secret references")
            payload["url"] = validate_http_url(payload["url"], field="url", allow_loopback=True)
            if "name" in payload:
                payload["name"] = bounded_string(payload["name"], "name", maximum=128)
            if "enabled" in payload and not isinstance(payload["enabled"], bool):
                raise ValueError("enabled must be a boolean")
            if "timeout" in payload and (
                isinstance(payload["timeout"], bool)
                or not isinstance(payload["timeout"], (int, float))
                or not 0.1 <= float(payload["timeout"]) <= 60.0
            ):
                raise ValueError("timeout must be between 0.1 and 60 seconds")
            if "secret_refs" in payload:
                refs = payload["secret_refs"]
                if not isinstance(refs, Mapping) or len(refs) > 16:
                    raise ValueError("secret_refs must be a bounded object")
                payload["secret_refs"] = {
                    bounded_string(key, "secret reference key", maximum=64): bounded_string(
                        value, "secret reference", maximum=256
                    )
                    for key, value in refs.items()
                }
        elif operation == "set_enabled":
            if set(payload) != {"enabled"} or not isinstance(payload["enabled"], bool):
                raise ValueError("set_enabled requires only a boolean enabled field")
        elif payload:
            raise ValueError(f"{operation} does not accept mutable fields")
        return bounded_public(payload), expected


def _time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return an aware datetime")
    return value.astimezone(timezone.utc).isoformat()
