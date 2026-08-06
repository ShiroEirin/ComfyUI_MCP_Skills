"""Fail-closed dependency planning, approvals, and resumable provisioning work."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import requests

from comfyui_mcp_skills.application._phase_o import (
    bounded_public,
    bounded_string,
    digest,
    owner,
    require_digest,
    strip_secret_values,
    validate_http_url,
)
from comfyui_mcp_skills.application.provisioning_ports import ManagerGateway, ProvisioningRepository
from comfyui_mcp_skills.domain.identifiers import validate_identifier
from comfyui_mcp_skills.domain.orchestration import WorkItem, WorkLease

INSTALL_CONFIRMATION = "INSTALL APPROVED DEPENDENCIES"
_PLAN_TTL = timedelta(hours=1)
_MAX_REQUIREMENTS = 200
_MAX_GIT_BYTES = 512 * 1024 * 1024
_MAX_MODEL_BYTES = 20 * 1024 * 1024 * 1024
_TERMINAL_ITEMS = frozenset({"completed", "failed", "cancelled"})
# Bounded unknown-observation retries before declaring a Manager queue lost, so
# a crash between claim commit and enqueue cannot leave an item retrying forever.
_UNKNOWN_OBSERVATION_LIMIT = 6
_FIXED_COMMIT = re.compile(r"[0-9a-f]{40}")
_FIXED_TAG = re.compile(r"tag:[A-Za-z0-9][A-Za-z0-9._/+\-]{0,126}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class DependencyProvisioningService:
    """Resolve only maintained catalog entries and bind install to digest and Approval."""

    def __init__(
        self,
        repository: ProvisioningRepository,
        *,
        catalog: Mapping[str, Mapping[str, Any]] | None = None,
        allowed_source_hosts: set[str] | frozenset[str] | None = None,
        max_model_bytes: int = _MAX_MODEL_BYTES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if (
            isinstance(max_model_bytes, bool)
            or not isinstance(max_model_bytes, int)
            or not 1 <= max_model_bytes <= _MAX_MODEL_BYTES
        ):
            raise ValueError("max_model_bytes exceeds the hard 20 GiB limit")
        self._max_model_bytes = max_model_bytes
        raw_catalog = catalog or {}
        if len(raw_catalog) > 10_000:
            raise ValueError("dependency catalog is too large")
        derived_hosts = {
            str(urlparse(str(item.get("source_url", ""))).hostname or "").lower()
            for item in raw_catalog.values()
        }
        hosts = allowed_source_hosts if allowed_source_hosts is not None else derived_hosts
        self._allowed_hosts = frozenset(host.lower().rstrip(".") for host in hosts if host)
        self._catalog = {
            dependency_id: self._validate_catalog_item(dependency_id, item)
            for dependency_id, item in raw_catalog.items()
        }

    def inspect(
        self,
        server_id: str,
        owner_id: str,
        requirements: list[dict[str, Any]] | None = None,
        *,
        workflow_id: str = "",
        revision_id: str = "",
    ) -> dict[str, Any]:
        server_id = validate_identifier(server_id, field="server_id")
        owner_id = owner(owner_id)
        if bool(workflow_id) != bool(revision_id):
            raise ValueError("workflow_id and revision_id must be provided together")
        if workflow_id:
            workflow_id = validate_identifier(workflow_id, field="workflow_id")
            revision_id = bounded_string(revision_id, "revision_id", maximum=128)
        if requirements is None:
            raw = self._repository.inspect_dependencies(
                owner_id, server_id, workflow_id, revision_id
            )
            requirements = _requirements_from_repository(raw)
        normalized = [] if not requirements else _requirements(requirements)
        resolved: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        for requirement in normalized:
            item = self._catalog.get(requirement["dependency_id"])
            if item is None:
                unresolved.append(
                    {
                        "dependency_id": requirement["dependency_id"],
                        "kind": requirement["kind"],
                        "name": requirement["name"],
                    }
                )
            else:
                _verify_requested_pins(requirement, item)
                resolved.append(dict(item))
        facts = {
            "server_id": server_id,
            "status": "blocked" if unresolved else "ready",
            "requested": normalized,
            "requirements": resolved,
            "resolved": resolved,
            "unresolved": unresolved,
            "restart_required": any(bool(item["restart_required"]) for item in resolved),
        }
        inspection_digest = digest(facts)
        return bounded_public(
            {
                "inspection_id": "dependency_inspection_" + inspection_digest,
                "inspection_digest": inspection_digest,
                **facts,
            }
        )

    def plan(
        self, server_id: str, owner_id: str, requirements: list[dict[str, Any]]
    ) -> dict[str, Any]:
        owner_id = owner(owner_id)
        report = self.inspect(server_id, owner_id, requirements)
        if report["unresolved"]:
            raise ValueError("unresolved dependencies cannot be installed")
        server = self._repository.get_server(report["server_id"], owner_id)
        if server is None:
            raise LookupError("Dependency Plan Server was not found")
        server_revision = server.get("revision")
        server_digest = server.get("config_digest")
        if (
            isinstance(server_revision, bool)
            or not isinstance(server_revision, int)
            or server_revision <= 0
            or not isinstance(server_digest, str)
            or _SHA256.fullmatch(server_digest) is None
        ):
            raise ValueError("Dependency Plan Server revision is invalid")
        items: list[dict[str, Any]] = []
        for ordinal, raw in enumerate(report["resolved"]):
            item = dict(raw)
            item["item_id"] = "provisioning_item_" + digest(
                [report["inspection_digest"], ordinal, item["dependency_id"]]
            )
            items.append(item)
        if not items:
            raise ValueError("dependency Plan requires at least one exact requirement")
        now = self._clock()
        immutable = {
            "owner_id": owner_id,
            "server_id": report["server_id"],
            "inspection_digest": report["inspection_digest"],
            "server_revision": server_revision,
            "server_config_digest": server_digest,
            "items": items,
            "restart_required": any(bool(item["restart_required"]) for item in items),
            "request_confirmation": INSTALL_CONFIRMATION,
            "created_at": _time(now),
            "expires_at": _time(now + _PLAN_TTL),
        }
        plan_digest = digest(immutable)
        plan_id = "dependency_plan_" + plan_digest
        plan = {
            "plan_id": plan_id,
            "plan_digest": plan_digest,
            **immutable,
            "resource_uri": f"comfyui://dependencies/plans/{plan_id}",
        }
        self._repository.save_plan(plan, items)
        approval = self._repository.create_approval(plan_id, plan_digest, owner_id, now=now)
        approval_id = bounded_string(approval.get("approval_id"), "approval_id", maximum=128)
        return bounded_public(
            {
                **plan,
                "approval_id": approval_id,
                "approval_uri": f"comfyui://approvals/{approval_id}",
            }
        )

    def get_plan(self, plan_id: str, owner_id: str) -> dict[str, Any]:
        plan_id = bounded_string(plan_id, "plan_id", maximum=128)
        owner_id = owner(owner_id)
        result = self._repository.get_plan(plan_id, owner_id)
        if result is None:
            raise LookupError("Dependency Plan was not found")
        return bounded_public(strip_secret_values(result))

    def get_approval(self, approval_id: str, owner_id: str) -> dict[str, Any]:
        approval_id = bounded_string(approval_id, "approval_id", maximum=128)
        owner_id = owner(owner_id)
        result = self._repository.get_approval(approval_id, owner_id, now=self._clock())
        if result is None:
            raise LookupError("Approval was not found")
        return bounded_public(strip_secret_values(result))

    def plan_approval(
        self, approval_id: str, decision: str, owner_id: str, reason: str = ""
    ) -> dict[str, Any]:
        approval_id = bounded_string(approval_id, "approval_id", maximum=128)
        owner_id = owner(owner_id)
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        if (
            not isinstance(reason, str)
            or len(reason) > 512
            or "\x00" in reason
            or re.search(
                r"(?i)\b(?:authorization|bearer|password|secret|token|api[_ -]?key)\b",
                reason,
            )
        ):
            raise ValueError("reason is invalid or may contain sensitive data")
        approval = self._repository.get_approval(
            approval_id,
            owner_id,
            now=self._clock(),
        )
        if approval is None:
            raise LookupError("Approval was not found")
        if approval.get("status") != "pending":
            raise ValueError("Approval is no longer pending")
        revision = approval.get("revision", 0)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("Approval revision is invalid")
        now = self._clock()
        immutable = {
            "approval_id": approval_id,
            "owner_id": owner_id,
            "decision": decision,
            "reason": reason,
            "approval_revision": revision,
            "status_before": "pending",
            "created_at": _time(now),
            "expires_at": _time(now + _PLAN_TTL),
        }
        plan_digest = digest(immutable)
        approval_plan_id = "approval_plan_" + plan_digest
        plan = {
            "approval_plan_id": approval_plan_id,
            "plan_digest": plan_digest,
            **immutable,
            "resource_uri": f"comfyui://approvals/{approval_id}",
        }
        self._repository.save_approval_plan(plan)
        return bounded_public(plan)

    def commit_approval(
        self, approval_plan_id: str, plan_digest: str, owner_id: str
    ) -> dict[str, Any]:
        approval_plan_id = bounded_string(approval_plan_id, "approval_plan_id", maximum=128)
        plan_digest = require_digest(plan_digest)
        owner_id = owner(owner_id)
        try:
            result = self._repository.commit_approval_plan(
                approval_plan_id, plan_digest, owner_id, now=self._clock()
            )
        except LookupError as exc:
            raise LookupError("Approval Plan was not found") from exc
        except ValueError as exc:
            raise ValueError(
                "Approval Plan conflicts with its owner, digest, state, or revision"
            ) from exc
        return bounded_public(strip_secret_values(result))

    def commit(
        self,
        plan_id: str,
        plan_digest: str,
        approval_id: str,
        owner_id: str,
        request_id: str,
        confirmation: str,
    ) -> dict[str, Any]:
        plan_id = bounded_string(plan_id, "plan_id", maximum=128)
        plan_digest = require_digest(plan_digest)
        approval_id = bounded_string(approval_id, "approval_id", maximum=128)
        owner_id = owner(owner_id)
        request_id = bounded_string(request_id, "request_id", maximum=128)
        if confirmation != INSTALL_CONFIRMATION:
            raise ValueError("exact dependency installation confirmation is required")
        plan = self._repository.get_plan(plan_id, owner_id)
        approval = self._repository.get_approval(
            approval_id,
            owner_id,
            now=self._clock(),
        )
        if plan is None:
            raise LookupError("Dependency Plan was not found")
        if approval is None:
            raise LookupError("Approval was not found")
        if (
            plan.get("plan_digest") != plan_digest
            or approval.get("plan_id") != plan_id
            or approval.get("plan_digest") != plan_digest
            or approval.get("status") not in {"approved", "used"}
        ):
            raise ValueError("Dependency Plan, digest, and Approval are not bound")
        try:
            result = self._repository.commit_plan(
                plan_id,
                plan_digest,
                approval_id,
                owner_id,
                request_id,
                confirmation,
                now=self._clock(),
            )
        except ValueError as exc:
            raise ValueError(
                "Dependency install conflicts with its digest, Approval, owner, or request"
            ) from exc
        return bounded_public(strip_secret_values(result))

    def get_job(self, job_id: str, owner_id: str) -> dict[str, Any]:
        job_id = bounded_string(job_id, "job_id", maximum=128)
        owner_id = owner(owner_id)
        result = self._repository.get_job(job_id, owner_id)
        if result is None:
            raise LookupError("Provisioning Job was not found")
        return bounded_public(strip_secret_values(result))

    def plan_cancel(self, job_id: str, owner_id: str) -> dict[str, Any]:
        job = self.get_job(job_id, owner_id)
        if str(job.get("status", "")) in {"completed", "failed", "cancelled"}:
            raise ValueError("Provisioning Job is already terminal")
        impact = {
            "job_status": str(job.get("status", "")),
            "job_updated_at": bounded_string(job.get("updated_at"), "job_updated_at", maximum=64),
            "pending_items": sum(
                1
                for item in job.get("items", [])
                if isinstance(item, Mapping) and item.get("status") == "pending"
            ),
            "enqueued_items_unchanged": sum(
                1
                for item in job.get("items", [])
                if isinstance(item, Mapping)
                and item.get("status") in {"enqueuing", "queued", "running"}
            ),
        }
        if impact["pending_items"] == 0:
            raise ValueError("Provisioning Job has no pending items to cancel")
        now = self._clock()
        immutable = {
            "owner_id": owner(owner_id),
            "job_id": job_id,
            "impact": impact,
            "created_at": _time(now),
            "expires_at": _time(now + _PLAN_TTL),
        }
        plan_digest = digest(immutable)
        cancel_plan_id = "provisioning_cancel_plan_" + plan_digest
        plan = {
            "cancel_plan_id": cancel_plan_id,
            "plan_digest": plan_digest,
            **immutable,
            "resource_uri": f"comfyui://provisioning/jobs/{job_id}",
        }
        self._repository.save_cancel_plan(plan)
        return bounded_public(plan)

    def commit_cancel(self, cancel_plan_id: str, plan_digest: str, owner_id: str) -> dict[str, Any]:
        cancel_plan_id = bounded_string(cancel_plan_id, "cancel_plan_id", maximum=128)
        try:
            result = self._repository.commit_cancel_plan(
                cancel_plan_id, require_digest(plan_digest), owner(owner_id), now=self._clock()
            )
        except LookupError as exc:
            raise LookupError("Provisioning cancel Plan was not found") from exc
        except ValueError as exc:
            raise ValueError("Provisioning cancel Plan conflicts with current state") from exc
        return bounded_public(strip_secret_values(result))

    def _validate_catalog_item(self, dependency_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        dependency_id = bounded_string(dependency_id, "dependency_id", maximum=256)
        if not isinstance(raw, Mapping):
            raise ValueError("dependency catalog entries must be objects")
        kind = raw.get("kind")
        source_type = raw.get("source_type")
        if (kind, source_type) not in {("node", "git"), ("model", "model")}:
            raise ValueError("dependency kind and source_type are unsupported")
        prefix, separator, name = dependency_id.partition(":")
        if separator != ":" or prefix != kind or not name:
            raise ValueError("dependency_id must match the catalog item kind")
        version = bounded_string(raw.get("version"), "version", maximum=128)
        if _FIXED_COMMIT.fullmatch(version) is None and _FIXED_TAG.fullmatch(version) is None:
            raise ValueError("dependency version must be a fixed Git commit or tag")
        checksum = bounded_string(raw.get("checksum"), "checksum", maximum=64)
        if _SHA256.fullmatch(checksum) is None:
            raise ValueError("dependency checksum must be a known SHA-256 digest")
        source_url = validate_http_url(
            raw.get("source_url"),
            field="source_url",
            https_only=True,
            allowed_hosts=self._allowed_hosts,
        )
        license_name = bounded_string(raw.get("license"), "license", maximum=128)
        size = raw.get("size_bytes")
        maximum = self._max_model_bytes if kind == "model" else _MAX_GIT_BYTES
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= maximum:
            raise ValueError("dependency size exceeds its hard limit")
        target_default = "models" if kind == "model" else "custom_nodes"
        target_dir = bounded_string(
            raw.get("target_dir", target_default), "target_dir", maximum=256
        )
        if target_dir.startswith(("/", "\\")) or ".." in target_dir.replace("\\", "/").split("/"):
            raise ValueError("dependency target_dir must be a safe relative directory")
        restart_required = raw.get("restart_required")
        if not isinstance(restart_required, bool):
            raise ValueError("restart_required must be an exact boolean fact")
        install_state = raw.get("install_state")
        if install_state not in {"missing", "installed", "update_available"}:
            raise ValueError("install_state must be an exact supported fact")
        return bounded_public(
            {
                "dependency_id": dependency_id,
                "kind": kind,
                "source_type": source_type,
                "source_url": source_url,
                "version": version,
                "checksum": checksum,
                "size_bytes": size,
                "target_dir": target_dir,
                "license": license_name,
                "restart_required": restart_required,
                "install_state": install_state,
            }
        )


class ProvisioningWorkHandler:
    """Checkpoint Manager enqueue before I/O and reconcile unknown outcomes durably."""

    def __init__(
        self,
        repository: ProvisioningRepository,
        manager: ManagerGateway,
        *,
        retry_delay_seconds: int = 5,
    ) -> None:
        if (
            isinstance(retry_delay_seconds, bool)
            or not isinstance(retry_delay_seconds, int)
            or not 1 <= retry_delay_seconds <= 300
        ):
            raise ValueError("retry_delay_seconds must be between 1 and 300")
        self._repository = repository
        self._manager = manager
        self._retry_delay = retry_delay_seconds

    def __call__(self, work: WorkItem, lease: WorkLease, *, now: datetime) -> None:
        job_id = bounded_string(work.payload.get("job_id"), "job_id", maximum=128)
        owner_id = owner(work.payload.get("owner_id"))
        lease = self._repository.renew_lease(lease, now=now)
        context = self._repository.get_work_context(job_id, owner_id)
        if context.get("job_id") != job_id or context.get("owner_id") != owner_id:
            raise LookupError("Provisioning work context was not found")
        items = context.get("items")
        if not isinstance(items, list) or len(items) > _MAX_REQUIREMENTS:
            raise ValueError("Provisioning work items are invalid")
        active = next(
            (
                item
                for item in items
                if isinstance(item, Mapping) and item.get("status") not in _TERMINAL_ITEMS
            ),
            None,
        )
        if active is None:
            states = {str(item.get("status")) for item in items if isinstance(item, Mapping)}
            status = (
                "failed"
                if "failed" in states
                else "cancelled"
                if "cancelled" in states
                else "completed"
            )
            self._repository.finish_work(
                lease,
                job_id=job_id,
                owner_id=owner_id,
                checkpoint=bounded_public(work.checkpoint),
                now=now,
                completed=True,
                delay_seconds=0,
                status=status,
            )
            return
        item_id = bounded_string(active.get("item_id"), "item_id", maximum=128)
        checkpoint = active.get("checkpoint")
        if not isinstance(checkpoint, Mapping):
            checkpoint = {}
        checkpoint = bounded_public(checkpoint)
        server = context.get("server")
        if not isinstance(server, dict):
            raise ValueError("Provisioning Server context is invalid")
        preflight_failed = False
        enqueue_server = server
        if not checkpoint.get("enqueue_started"):
            try:
                preflight = getattr(self._manager, "preflight_install", self._manager.inspect)
                availability = preflight(server)
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError, OSError):
                self._defer(
                    lease, job_id, owner_id, work.checkpoint, now, "manager_preflight_unavailable"
                )
                return
            except (LookupError, ValueError):
                preflight_failed = True
            else:
                if (
                    availability.get("state") != "available"
                    and availability.get("available") is not True
                ):
                    self._defer(
                        lease,
                        job_id,
                        owner_id,
                        work.checkpoint,
                        now,
                        "manager_preflight_unavailable",
                    )
                    return
                enqueue_server = {
                    **server,
                    "_secure_fetch_preflight_token": availability.get("preflight_token"),
                }
        if not checkpoint.get("enqueue_started"):
            queue_id = "manager_queue_" + digest([job_id, item_id])
            claimed = self._repository.claim_item_for_enqueue(
                lease,
                job_id=job_id,
                owner_id=owner_id,
                item_id=item_id,
                queue_id=queue_id,
                now=now,
            )
            if claimed is None:
                self._defer(lease, job_id, owner_id, work.checkpoint, now, "claim_changed")
                return
            checkpoint = {"enqueue_started": True, "queue_id": queue_id, "state": "enqueue_started"}
            if preflight_failed:
                observation = {"queue_id": queue_id, "state": "failed", "retryable": False}
            else:
                try:
                    observation = self._manager.enqueue_install(
                        enqueue_server, dict(claimed), queue_id=queue_id
                    )
                except (requests.Timeout, requests.ConnectionError, OSError):
                    observation = {"queue_id": queue_id, "state": "unknown", "retryable": True}
                except (LookupError, ValueError, requests.HTTPError):
                    observation = {"queue_id": queue_id, "state": "failed", "retryable": False}
        else:
            persisted_queue_id = checkpoint.get("queue_id")
            queue_id = (
                bounded_string(persisted_queue_id, "queue_id", maximum=128)
                if persisted_queue_id
                else "manager_queue_" + digest([job_id, item_id])
            )
            checkpoint = {**checkpoint, "enqueue_started": True, "queue_id": queue_id}
            try:
                observation = self._manager.observe_install(server, queue_id, item=dict(active))
            except (requests.Timeout, requests.ConnectionError, OSError):
                observation = {"queue_id": queue_id, "state": "unknown", "retryable": True}
            except (LookupError, ValueError, requests.HTTPError):
                observation = {"queue_id": queue_id, "state": "failed", "retryable": False}
        state = str(observation.get("state", "unknown"))
        if state not in {"unknown", "queued", "running", "completed", "failed", "cancelled"}:
            state = "unknown"
        public_observation = bounded_public(
            {
                "queue_id": queue_id,
                "state": state,
                "retryable": state == "unknown",
                "restart_required": bool(active.get("restart_required", False))
                if state == "completed"
                else False,
            }
        )
        raw_unknown_count = checkpoint.get("unknown_count")
        unknown_count = (
            raw_unknown_count
            if isinstance(raw_unknown_count, int) and not isinstance(raw_unknown_count, bool)
            else 0
        )
        if state == "unknown":
            unknown_count += 1
        else:
            unknown_count = 0
        next_checkpoint = {
            **checkpoint,
            **public_observation,
            "last_observed_at": _time(now),
            "unknown_count": unknown_count,
        }
        if state in _TERMINAL_ITEMS:
            self._repository.complete_item(
                lease,
                job_id=job_id,
                owner_id=owner_id,
                item_id=item_id,
                result=public_observation,
                now=now,
            )
        elif state == "unknown" and unknown_count >= _UNKNOWN_OBSERVATION_LIMIT:
            # The Manager never acknowledges this queue_id (likely the enqueue
            # was lost between claim commit and the HTTP call). Fail the item
            # with a diagnosable error instead of retrying forever.
            self._repository.complete_item(
                lease,
                job_id=job_id,
                owner_id=owner_id,
                item_id=item_id,
                result={
                    **public_observation,
                    "state": "failed",
                    "retryable": False,
                    "error": "manager_queue_unknown_timeout",
                },
                now=now,
            )
        else:
            self._repository.save_item_checkpoint(
                lease,
                job_id=job_id,
                owner_id=owner_id,
                item_id=item_id,
                checkpoint=next_checkpoint,
                now=now,
            )
        self._repository.finish_work(
            lease,
            job_id=job_id,
            owner_id=owner_id,
            checkpoint={
                **bounded_public(work.checkpoint),
                "current_item_id": item_id,
                "manager": next_checkpoint,
            },
            now=now,
            completed=False,
            delay_seconds=self._retry_delay,
            status="running",
        )

    def _defer(
        self,
        lease: WorkLease,
        job_id: str,
        owner_id: str,
        checkpoint: dict[str, Any],
        now: datetime,
        reason: str,
    ) -> None:
        self._repository.finish_work(
            lease,
            job_id=job_id,
            owner_id=owner_id,
            checkpoint={**bounded_public(checkpoint), "deferred_reason": reason},
            now=now,
            completed=False,
            delay_seconds=self._retry_delay,
            status="running",
        )


def _requirements(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= _MAX_REQUIREMENTS:
        raise ValueError("requirements must contain between 1 and 200 exact dependencies")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for requirement in raw:
        if not isinstance(requirement, Mapping):
            raise ValueError("each dependency requirement must be an object")
        if set(requirement) - {
            "dependency_id",
            "kind",
            "name",
            "source_url",
            "version",
            "checksum",
            "size_bytes",
        }:
            raise ValueError("dependency requirement fields are unsupported")
        kind = requirement.get("kind")
        if kind not in {"node", "model"}:
            raise ValueError("dependency requirement kind must be node or model")
        name = bounded_string(requirement.get("name"), "dependency name", maximum=192)
        dependency_id = bounded_string(
            requirement.get("dependency_id", f"{kind}:{name}"),
            "dependency_id",
            maximum=256,
        )
        if dependency_id != f"{kind}:{name}":
            raise ValueError("dependency_id must exactly match kind and name")
        if dependency_id in seen:
            continue
        normalized: dict[str, Any] = {
            "dependency_id": dependency_id,
            "kind": kind,
            "name": name,
        }
        for key in ("source_url", "version", "checksum", "size_bytes"):
            if key in requirement:
                normalized[key] = requirement[key]
        result.append(normalized)
        seen.add(dependency_id)
    return result


def _verify_requested_pins(requirement: Mapping[str, Any], item: Mapping[str, Any]) -> None:
    pairs = {
        "source_url": "source_url",
        "version": "version",
        "checksum": "checksum",
        "size_bytes": "size_bytes",
    }
    for requested_key, item_key in pairs.items():
        if requested_key in requirement and requirement[requested_key] != item[item_key]:
            raise ValueError("requested dependency pins do not match the maintained catalog")


def _requirements_from_repository(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, Mapping):
        raise ValueError("dependency inspection context is invalid")
    requirements = raw.get("requirements")
    if isinstance(requirements, list):
        return [] if not requirements else _requirements(requirements)
    result: list[dict[str, Any]] = []
    for key, kind in (("missing_nodes", "node"), ("missing_models", "model")):
        values = raw.get(key, [])
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
            raise ValueError("dependency inspection facts are invalid")
        result.extend({"kind": kind, "name": value} for value in values)
    return result


def _time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return an aware datetime")
    return value.astimezone(timezone.utc).isoformat()
