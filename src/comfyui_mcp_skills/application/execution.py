"""Submit validated workflows with explicit assets and durable idempotency."""

from __future__ import annotations

import copy
import re
import uuid
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from comfyui_mcp_skills.application.catalog import WorkflowCatalog
from comfyui_mcp_skills.application.jobs import JobService
from comfyui_mcp_skills.application.planning import ExecutionIdentity, ExecutionPlanningService
from comfyui_mcp_skills.application.ports import (
    AssetRepository,
    ComfyUIGateway,
    RunRepository,
)
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import (
    AssetNotFound,
    ExecutionInProgress,
    IdempotencyConflict,
    ServerOffline,
    WorkflowArgumentsError,
)
from comfyui_mcp_skills.domain.identifiers import validate_identifier
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
        planning: ExecutionPlanningService | None = None,
    ) -> None:
        self._catalog = catalog
        self._servers = servers
        self._runs = runs
        self._assets = assets
        self._gateway_factory = gateway_factory
        self._planning = planning
        self._jobs = JobService(servers, runs, gateway_factory)

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
        if idempotency_key and (
            idempotency_key.strip() != idempotency_key
            or any(ord(char) < 33 or ord(char) == 127 for char in idempotency_key)
        ):
            raise ValueError("idempotency_key must contain printable non-whitespace characters")
        workflow = self._catalog.get(server_id, workflow_id)
        validate_arguments(workflow.parameters, arguments)
        resolved = self._resolve_assets(server_id, workflow.parameters, arguments, owner_id)
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
                existing = self._runs.get_by_idempotency(server_id, idempotency_key, owner_id)
                if existing is not None:
                    return existing
                recovered_prompt = self._find_prompt_by_client_id(
                    gateway, str(claim.get("client_id", ""))
                )
                if recovered_prompt:
                    if self._planning is not None:
                        recovered_identity = self._planning.identity_for_client(
                            server_id, str(claim.get("client_id", ""))
                        )
                        if recovered_identity is None:
                            raise ExecutionInProgress(
                                "Recovered upstream submission has no canonical G4 identity"
                            )
                        self._planning.finalize_submission(
                            recovered_identity,
                            upstream_prompt_id=recovered_prompt,
                            idempotency_key=idempotency_key,
                            request_digest=request_digest,
                            lease_token=str(claim.get("lease_token", "")),
                        )
                        return Job(
                            prompt_id=recovered_prompt,
                            server_id=server_id,
                            workflow_id=workflow_id,
                            status="submitted",
                            idempotency_key=idempotency_key,
                            client_id=str(claim.get("client_id", "")),
                            request_digest=request_digest,
                            owner_id=owner_id,
                            job_id=recovered_identity.job_id,
                            plan_id=recovered_identity.plan_id,
                            revision_id=recovered_identity.revision_id,
                            deployment_id=recovered_identity.deployment_id,
                            plan_digest=recovered_identity.plan_digest,
                        )
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
        execution_identity: ExecutionIdentity | None = None
        if self._planning is not None:
            try:
                execution_identity = self._planning.materialize(
                    server_id=server_id,
                    workflow_id=workflow_id,
                    owner_id=owner_id,
                    arguments=arguments,
                    client_id=client_id,
                    resolved_inputs=resolved,
                    workflow_graph=workflow.graph,
                    parameter_schema=workflow.parameters,
                )
            except BaseException:
                self._runs.release_claim(
                    server_id,
                    idempotency_key,
                    request_digest,
                    lease_token,
                    owner_id,
                )
                raise
        graph = self._inject(workflow.graph, workflow.parameters, resolved)
        try:
            queued = gateway.queue_prompt(graph, client_id=client_id)
        except ServerOffline as exc:
            self._runs.mark_submission_unknown(server_id, idempotency_key, lease_token, owner_id)
            if execution_identity is not None:
                assert self._planning is not None
                self._planning.mark_submission_unknown(execution_identity, str(exc))
            raise
        except Exception as exc:
            if execution_identity is not None:
                assert self._planning is not None
                self._runs.mark_submission_unknown(
                    server_id, idempotency_key, lease_token, owner_id
                )
                self._planning.mark_submission_unknown(execution_identity, str(exc))
            else:
                self._runs.release_claim(
                    server_id,
                    idempotency_key,
                    request_digest,
                    lease_token,
                    owner_id,
                )
            raise
        prompt_id = str(queued.get("prompt_id", ""))
        upstream_job_id = str(queued.get("job_id", ""))
        if not prompt_id and not upstream_job_id:
            self._runs.mark_submission_unknown(server_id, idempotency_key, lease_token, owner_id)
            if execution_identity is not None:
                assert self._planning is not None
                self._planning.mark_submission_unknown(
                    execution_identity, "ComfyUI submission outcome is unknown"
                )
            raise ServerOffline("ComfyUI submission outcome is unknown")
        if execution_identity is not None:
            assert self._planning is not None
            self._planning.finalize_submission(
                execution_identity,
                upstream_prompt_id=prompt_id,
                upstream_job_id=upstream_job_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                lease_token=lease_token,
            )
        job = Job(
            prompt_id=prompt_id,
            server_id=server_id,
            workflow_id=workflow_id,
            status="submitted",
            idempotency_key=idempotency_key,
            client_id=client_id,
            request_digest=request_digest,
            owner_id=owner_id,
            job_id=execution_identity.job_id if execution_identity else "",
            plan_id=execution_identity.plan_id if execution_identity else "",
            revision_id=execution_identity.revision_id if execution_identity else "",
            deployment_id=execution_identity.deployment_id if execution_identity else "",
            plan_digest=execution_identity.plan_digest if execution_identity else "",
        )
        if execution_identity is None:
            self._runs.save(job, lease_token=lease_token)
        return job

    @classmethod
    def _find_prompt_by_client_id(cls, gateway: ComfyUIGateway, client_id: str) -> str:
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
            metadata = parameters.get(name, {})
            parameter_type = str(metadata.get("type", ""))
            if parameter_type not in {"image", "mask", "audio", "video"}:
                continue
            if metadata.get("storage_type") == "output" and not (
                isinstance(value, str) and value.startswith("comfyui://outputs/")
            ):
                raise AssetNotFound(f'Media parameter "{name}" requires an output URI')
            if isinstance(value, str) and value.startswith("comfyui://outputs/"):
                resolved[name] = self._resolve_output(
                    server_id,
                    value,
                    parameter_type,
                    owner_id,
                )
                continue
            if not isinstance(value, str) or not value.startswith("asset_"):
                if owner_id:
                    raise AssetNotFound(
                        f'Media parameter "{name}" requires an authorized asset_id or output URI'
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

    def _resolve_output(
        self,
        server_id: str,
        uri: str,
        parameter_type: str,
        owner_id: str,
    ) -> str:
        match = re.fullmatch(
            r"comfyui://outputs/([^/?#%]+)/([^/?#%]+)/([0-9]+)",
            uri,
            flags=re.ASCII,
        )
        if match is None:
            raise AssetNotFound(f"Invalid output URI: {uri}")
        output_server_id, prompt_id, raw_index = match.groups()
        try:
            validate_identifier(output_server_id, field="server_id")
            validate_identifier(prompt_id, field="prompt_id")
        except ValueError as exc:
            raise AssetNotFound(f"Invalid output URI: {uri}") from exc
        if output_server_id != server_id:
            raise AssetNotFound(f"Output does not belong to server: {server_id}")
        job = self._jobs.get(server_id, prompt_id, owner_id=owner_id)
        index = int(raw_index)
        if index >= len(job.outputs):
            raise AssetNotFound(f"Output not found: {uri}")
        output = job.outputs[index]
        expected_media = "image" if parameter_type == "mask" else parameter_type
        media_type = str(output.get("media_type", ""))
        if media_type != expected_media:
            raise AssetNotFound(f"Output is {media_type or 'unknown'}, expected {expected_media}")
        return self._comfyui_output_ref(uri, output)

    @staticmethod
    def _comfyui_output_ref(uri: str, output: dict[str, Any]) -> str:
        filename = str(output.get("filename", ""))
        subfolder = str(output.get("subfolder", ""))
        storage_type = str(output.get("type", ""))
        if storage_type != "output":
            raise AssetNotFound(f"Output has an unsupported storage type: {uri}")
        if not ExecutionService._safe_server_path(filename, nested=False):
            raise AssetNotFound(f"Output has an unsafe filename: {uri}")
        if subfolder and not ExecutionService._safe_server_path(subfolder, nested=True):
            raise AssetNotFound(f"Output has an unsafe subfolder: {uri}")
        comfyui_ref = f"{subfolder}/{filename}" if subfolder else filename
        return f"{comfyui_ref} [output]"

    @staticmethod
    def _safe_server_path(value: str, *, nested: bool) -> bool:
        if not value or "\\" in value or "\x00" in value or ":" in value:
            return False
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            return False
        if not nested and len(path.parts) != 1:
            return False
        return "/".join(path.parts) == value

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
                raise WorkflowArgumentsError(f'Workflow parameter "{name}" has an invalid target')
            inputs = node.get("inputs")
            if not isinstance(inputs, dict) or field not in inputs:
                raise WorkflowArgumentsError(f'Workflow parameter "{name}" targets a missing input')
            inputs[field] = value
        return result
