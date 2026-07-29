"""Submit validated workflows with explicit assets and durable idempotency."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from typing import Any

from comfyui_mcp_skills.application.catalog import WorkflowCatalog
from comfyui_mcp_skills.application.ports import (
    AssetRepository,
    ComfyUIGateway,
    RunRepository,
)
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import (
    AssetNotFound,
    ExecutionFailed,
    ExecutionInProgress,
    IdempotencyConflict,
    ServerOffline,
)
from comfyui_mcp_skills.domain.models import Job
from comfyui_mcp_skills.domain.workflow_schema import validate_arguments


GatewayFactory = Callable[[dict[str, Any]], ComfyUIGateway]


class ExecutionService:
    def __init__(
        self,
        catalog: WorkflowCatalog,
        servers: ServerRegistry,
        runs: RunRepository,
        assets: AssetRepository,
        gateway_factory: GatewayFactory,
    ) -> None:
        self._catalog = catalog
        self._servers = servers
        self._runs = runs
        self._assets = assets
        self._gateway_factory = gateway_factory

    def submit(
        self,
        server_id: str,
        workflow_id: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str = "",
        owner_id: str = "",
    ) -> Job:
        if not isinstance(idempotency_key, str) or len(idempotency_key) > 256:
            raise ValueError("idempotency_key must be a string up to 256 characters")
        workflow = self._catalog.get(server_id, workflow_id)
        validate_arguments(workflow.parameters, arguments)
        resolved = self._resolve_assets(
            server_id, workflow.parameters, arguments, owner_id
        )
        request_digest = self._runs.request_digest(workflow_id, arguments)
        client_id = uuid.uuid4().hex
        gateway = self._gateway_factory(self._servers.connection(server_id))
        lease_token = ""
        if idempotency_key:
            claimed = self._runs.claim(
                server_id,
                workflow_id,
                idempotency_key,
                arguments,
                owner_id=owner_id,
                client_id=client_id,
            )
            if claimed is None:
                claim = self._runs.get_claim(server_id, idempotency_key, owner_id)
                if (
                    claim is None
                    or claim.get("workflow_id") != workflow_id
                    or claim.get("request_digest") != request_digest
                ):
                    raise IdempotencyConflict(
                        "Idempotency key was already used for a different request"
                    )
                existing = self._runs.get_by_idempotency(
                    server_id, idempotency_key, owner_id
                )
                if existing is not None:
                    return existing
                recovered_prompt = self._find_prompt_by_client_id(
                    gateway, str(claim.get("client_id", ""))
                )
                if recovered_prompt:
                    recovered = Job(
                        prompt_id=recovered_prompt,
                        server_id=server_id,
                        workflow_id=workflow_id,
                        status="submitted",
                        idempotency_key=idempotency_key,
                        client_id=str(claim.get("client_id", "")),
                        request_digest=request_digest,
                        owner_id=owner_id,
                    )
                    self._runs.save(
                        recovered,
                        lease_token=str(claim.get("lease_token", "")),
                    )
                    return recovered
                raise ExecutionInProgress(
                    "Submission outcome is unknown; retry status reconciliation"
                )
            lease_token = claimed
        graph = self._inject(workflow.graph, workflow.parameters, resolved)
        try:
            queued = gateway.queue_prompt(graph, client_id=client_id)
        except ServerOffline:
            self._runs.mark_submission_unknown(
                server_id, idempotency_key, lease_token, owner_id
            )
            raise
        except Exception:
            self._runs.release_claim(
                server_id,
                idempotency_key,
                request_digest,
                lease_token,
                owner_id,
            )
            raise
        prompt_id = str(queued.get("prompt_id", ""))
        if not prompt_id:
            self._runs.mark_submission_unknown(
                server_id, idempotency_key, lease_token, owner_id
            )
            raise ServerOffline("ComfyUI submission outcome is unknown")
        job = Job(
            prompt_id=prompt_id,
            server_id=server_id,
            workflow_id=workflow_id,
            status="submitted",
            idempotency_key=idempotency_key,
            client_id=client_id,
            request_digest=request_digest,
            owner_id=owner_id,
        )
        self._runs.save(job, lease_token=lease_token)
        return job

    @classmethod
    def _find_prompt_by_client_id(
        cls, gateway: ComfyUIGateway, client_id: str
    ) -> str:
        if not client_id:
            return ""
        queue = gateway.get_queue()
        for key in ("queue_running", "queue_pending"):
            for item in queue.get(key, []):
                if (
                    isinstance(item, list)
                    and len(item) > 1
                    and cls._contains_client_id(item, client_id)
                ):
                    return str(item[1])
        histories = gateway.get_history_list(max_items=100)
        for prompt_id, history in histories.items():
            if cls._contains_client_id(history, client_id):
                return str(prompt_id)
        return ""

    @classmethod
    def _contains_client_id(cls, value: Any, client_id: str) -> bool:
        if isinstance(value, dict):
            if value.get("client_id") == client_id:
                return True
            return any(cls._contains_client_id(item, client_id) for item in value.values())
        if isinstance(value, list):
            return any(cls._contains_client_id(item, client_id) for item in value)
        return False


    def _resolve_assets(
        self,
        server_id: str,
        parameters: dict[str, Any],
        arguments: dict[str, Any],
        owner_id: str = "",
    ) -> dict[str, Any]:
        resolved = dict(arguments)
        for name, value in arguments.items():
            parameter_type = str(parameters.get(name, {}).get("type", ""))
            if parameter_type not in {"image", "mask", "audio", "video"}:
                continue
            if not isinstance(value, str) or not value.startswith("asset_"):
                if owner_id:
                    raise AssetNotFound(
                        f'Media parameter "{name}" requires an authorized asset_id'
                    )
                continue
            asset = self._assets.get(value)
            if asset is None or asset.server_id != server_id:
                raise AssetNotFound(f"Asset not found for server {server_id}: {value}")
            if owner_id and asset.owner_id != owner_id:
                raise AssetNotFound(f"Asset not found for owner: {value}")
            expected_media = "image" if parameter_type == "mask" else parameter_type
            if asset.media_type != expected_media:
                raise AssetNotFound(
                    f"Asset {value} is {asset.media_type}, expected {expected_media}"
                )
            resolved[name] = asset.comfyui_ref
        return resolved

    @staticmethod
    def _inject(
        graph: dict[str, Any],
        parameters: dict[str, Any],
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        result = copy.deepcopy(graph)
        for name, value in arguments.items():
            metadata = parameters[name]
            node_id = str(metadata.get("node_id", ""))
            field = str(metadata.get("field", ""))
            node = result.get(node_id)
            if not node_id or not field or not isinstance(node, dict):
                raise WorkflowArgumentsError(
                    f'Workflow parameter "{name}" has an invalid target'
                )
            inputs = node.get("inputs")
            if not isinstance(inputs, dict) or field not in inputs:
                raise WorkflowArgumentsError(
                    f'Workflow parameter "{name}" targets a missing input'
                )
            inputs[field] = value
        return result
