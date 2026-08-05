"""Focused Phase O application and Manager safety contracts."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any

import pytest
import requests

from comfyui_mcp_skills.application.config_bundles import ConfigBundleService
from comfyui_mcp_skills.application.provisioning import (
    DependencyProvisioningService,
    ProvisioningWorkHandler,
)
from comfyui_mcp_skills.application.server_control import ServerControlService
from comfyui_mcp_skills.domain.orchestration import WorkItem, WorkLease
from comfyui_mcp_skills.infrastructure.comfyui.manager_gateway import SafeManagerGateway

_NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)


class MemoryAdminRepository:
    def __init__(self) -> None:
        self.server_plans: dict[str, dict[str, Any]] = {}
        self.servers: dict[str, dict[str, Any]] = {}
        self.revision = 0
        self.import_plans: dict[str, dict[str, Any]] = {}

    def get_server(self, server_id: str, owner_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.servers.get(server_id))

    def list_servers(self, owner_id: str) -> list[dict[str, Any]]:
        return copy.deepcopy(list(self.servers.values()))

    def server_delete_impact(self, server_id: str, owner_id: str) -> dict[str, Any]:
        return {"workflow_count": 0, "active_job_count": 0}

    def save_server_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        self.server_plans[plan["plan_id"]] = copy.deepcopy(plan)
        return copy.deepcopy(plan)

    def commit_server_plan(
        self, plan_id: str, plan_digest: str, owner_id: str, *, now: datetime
    ) -> dict[str, Any]:
        plan = self.server_plans[plan_id]
        if (plan["plan_digest"], plan["owner_id"]) != (plan_digest, owner_id):
            raise ValueError("binding mismatch")
        self.revision += 1
        self.servers[plan["server_id"]] = {
            **plan["changes"],
            "server_id": plan["server_id"],
            "revision": self.revision,
            "is_default": not self.servers,
        }
        return {"server_id": plan["server_id"], "revision": self.revision}

    def current_revision(self, owner_id: str) -> int:
        return self.revision

    def export_snapshot(self, owner_id: str) -> dict[str, Any]:
        return {
            "servers": [
                {
                    "server_id": "local",
                    "url": "http://127.0.0.1:8188",
                    "password": "never-export",
                    "secret_refs": {"api_key_ref": "COMFY_API_KEY"},
                }
            ],
            "workflows": [],
        }

    def save_bundle(self, bundle: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(bundle)

    def get_bundle(self, revision: int, owner_id: str) -> dict[str, Any] | None:
        return None

    def save_import_plan(self, plan: dict[str, Any]) -> None:
        self.import_plans[plan["plan_id"]] = copy.deepcopy(plan)

    def commit_import_plan(
        self, plan_id: str, plan_digest: str, owner_id: str, *, now: datetime
    ) -> dict[str, Any]:
        plan = self.import_plans[plan_id]
        if plan["expected_revision"] != self.revision:
            raise ValueError("revision conflict")
        return {"status": "imported", "revision": self.revision + 1}


def test_empty_project_first_server_uses_plan_commit_and_digest_binding() -> None:
    repository = MemoryAdminRepository()
    service = ServerControlService(repository, clock=lambda: _NOW)
    plan = service.plan(
        "upsert", "local", "owner-a", {"url": "http://127.0.0.1:8188", "expected_revision": 0}
    )
    with pytest.raises(ValueError):
        service.commit(plan["plan_id"], "0" * 64, "owner-a")
    result = service.commit(plan["plan_id"], plan["plan_digest"], "owner-a")
    assert result["server_id"] == "local"
    assert repository.servers["local"]["is_default"] is True


def test_config_export_recursively_removes_secret_values_and_fences_revision() -> None:
    repository = MemoryAdminRepository()
    service = ConfigBundleService(repository, clock=lambda: _NOW)
    bundle = service.export("owner-a")
    assert "never-export" not in repr(bundle)
    assert "COMFY_API_KEY" in repr(bundle)
    plan = service.plan_import(bundle, "0", "owner-a")
    repository.revision = 1
    with pytest.raises(ValueError, match="revision"):
        service.commit_import(plan["plan_id"], plan["plan_digest"], "owner-a")


class DependencyRepository:
    def inspect_dependencies(
        self, owner_id: str, server_id: str, workflow_id: str, revision_id: str
    ) -> dict[str, Any]:
        return {"missing_nodes": ["UnknownNode"], "missing_models": []}


def _catalog_item(**changes: Any) -> dict[str, Any]:
    item = {
        "kind": "node",
        "source_type": "git",
        "source_url": "https://example.com/node.git",
        "version": "0123456789abcdef0123456789abcdef01234567",
        "checksum": "a" * 64,
        "size_bytes": 1024,
        "target_dir": "custom_nodes",
        "restart_required": True,
        "install_state": "missing",
        "license": "Apache-2.0",
    }
    item.update(changes)
    return item


def test_unresolved_nodes_are_reported_without_repository_guessing() -> None:
    service = DependencyProvisioningService(DependencyRepository(), catalog={})
    report = service.inspect("local", "owner-a")
    assert report["resolved"] == []
    assert report["unresolved"][0]["dependency_id"] == "node:UnknownNode"
    with pytest.raises(ValueError, match="unresolved"):
        service.plan("local", "owner-a", [{"kind": "node", "name": "UnknownNode"}])


@pytest.mark.parametrize(
    "item",
    [
        _catalog_item(version="main"),
        _catalog_item(checksum="unknown"),
        _catalog_item(source_url="http://169.254.169.254/node.git"),
        _catalog_item(kind="model", source_type="model", size_bytes=21 * 1024**3),
    ],
)
def test_floating_unknown_unsafe_and_oversized_catalog_entries_fail_closed(
    item: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        DependencyProvisioningService(DependencyRepository(), catalog={"node:Unsafe": item})


class WorkRepository:
    def __init__(self) -> None:
        self.item = {
            **_catalog_item(),
            "item_id": "item-a",
            "status": "pending",
            "checkpoint": {"enqueue_started": True},
        }
        self.checkpoints: list[dict[str, Any]] = []

    def renew_lease(self, lease: WorkLease, *, now: datetime, lease_seconds: int = 30) -> WorkLease:
        return lease

    def get_work_context(self, job_id: str, owner_id: str) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "owner_id": owner_id,
            "server": {"url": "http://127.0.0.1:8188"},
            "items": [copy.deepcopy(self.item)],
        }

    def claim_item_for_enqueue(self, lease: WorkLease, **kwargs: Any) -> dict[str, Any] | None:
        raise AssertionError("lease recovery must not enqueue again")

    def save_item_checkpoint(self, lease: WorkLease, **kwargs: Any) -> dict[str, Any]:
        self.item["checkpoint"] = copy.deepcopy(kwargs["checkpoint"])
        self.checkpoints.append(copy.deepcopy(kwargs["checkpoint"]))
        return kwargs["checkpoint"]

    def complete_item(self, lease: WorkLease, **kwargs: Any) -> dict[str, Any]:
        return kwargs["result"]

    def finish_work(self, lease: WorkLease, **kwargs: Any) -> None:
        return None


class WorkManager:
    def inspect(self, server: dict[str, Any]) -> dict[str, Any]:
        return {"state": "available"}

    def enqueue_install(
        self, server: dict[str, Any], item: dict[str, Any], *, queue_id: str
    ) -> dict[str, Any]:
        raise AssertionError("lease recovery must not enqueue again")

    def observe_install(
        self, server: dict[str, Any], queue_id: str, *, item: dict[str, Any]
    ) -> dict[str, Any]:
        return {"queue_id": queue_id, "state": "running", "retryable": False}


def test_handler_recovers_missing_queue_id_deterministically_without_reenqueue() -> None:
    repository = WorkRepository()
    handler = ProvisioningWorkHandler(repository, WorkManager())
    work = WorkItem(
        "work-a",
        "comfyui://provisioning/jobs/job-a",
        "provisioning.execute",
        {"job_id": "job-a", "owner_id": "owner-a"},
        {},
        "running",
    )
    handler(work, WorkLease("work-a", "worker-a", 1, "2026-08-03T12:01:00+00:00"), now=_NOW)
    assert repository.checkpoints[0]["queue_id"].startswith("manager_queue_")
    assert repository.checkpoints[0]["state"] == "running"


def test_safe_gateway_turns_timeout_into_unknown_and_disables_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = SafeManagerGateway(
        allowed_source_hosts={"example.com"},
        allowed_server_origins={"http://127.0.0.1:8188"},
    )

    def timeout(*args: Any, **kwargs: Any) -> Any:
        assert kwargs["allow_redirects"] is False
        raise requests.Timeout("bounded timeout")

    monkeypatch.setattr(requests, "get", timeout)
    assert gateway.observe_install(
        {"url": "http://127.0.0.1:8188"},
        "queue-a",
        item=_catalog_item(),
    ) == {
        "queue_id": "queue-a",
        "state": "unknown",
        "retryable": True,
    }


def _manager_response(payload: dict[str, Any], url: str) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.url = url
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(payload).encode()
    response._content_consumed = True
    return response


def test_safe_gateway_requires_policy_before_install_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = SafeManagerGateway(
        allowed_source_hosts={"example.com"},
        allowed_server_origins={"http://127.0.0.1:8188"},
    )
    monkeypatch.setattr(
        requests,
        "get",
        lambda url, **_kwargs: _manager_response({}, url),
    )
    posted: list[str] = []
    monkeypatch.setattr(requests, "post", lambda url, **_kwargs: posted.append(url))

    with pytest.raises(ValueError, match="capability is not supported"):
        gateway.enqueue_install(
            {"url": "http://127.0.0.1:8188"}, _catalog_item(), queue_id="queue-a"
        )

    assert posted == []


def test_safe_gateway_uses_versioned_endpoint_and_queue_bound_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = SafeManagerGateway(
        allowed_source_hosts={"example.com"},
        allowed_server_origins={"http://127.0.0.1:8188"},
    )
    capability = {
        "secure_fetch": {
            "version": "comfyui-mcp-secure-fetch-v1",
            "enqueue_receipt": True,
            "completion_receipt": True,
            "queue_id_bound": True,
        }
    }
    monkeypatch.setattr(
        requests,
        "get",
        lambda url, **_kwargs: _manager_response(capability, url),
    )

    def post(url: str, **_kwargs: Any) -> requests.Response:
        item = _catalog_item()
        assert url.endswith("/manager/queue/secure-fetch-v1/install")
        return _manager_response(
            {
                "state": "queued",
                "receipt": {
                    "queue_id": "queue-a",
                    "policy_version": "comfyui-mcp-secure-fetch-v1",
                    "source_url": item["source_url"],
                    "version": item["version"],
                    "sha256": item["checksum"],
                    "size_bytes": item["size_bytes"],
                    "redirects_allowed": False,
                    "public_ip_enforced": True,
                },
            },
            url,
        )

    monkeypatch.setattr(requests, "post", post)
    assert gateway.enqueue_install(
        {"url": "http://127.0.0.1:8188"}, _catalog_item(), queue_id="queue-a"
    ) == {"queue_id": "queue-a", "state": "queued", "retryable": False}
