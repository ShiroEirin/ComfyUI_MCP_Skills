"""Submit validated workflows with explicit assets and durable idempotency."""

from __future__ import annotations

import copy
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from comfyui_mcp_skills.application.catalog import WorkflowCatalog
from comfyui_mcp_skills.application.planning import (
    ExecutionIdentity,
    ExecutionPlanningService,
    normalize_overrides,
)
from comfyui_mcp_skills.application.ports import (
    ArtifactRepository,
    AssetRepository,
    ComfyUIGateway,
    RunRepository,
)
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.control_plane import (
    parse_legacy_resource_uri,
    validate_control_plane_id,
)
from comfyui_mcp_skills.domain.errors import (
    AssetNotFound,
    ExecutionInProgress,
    IdempotencyConflict,
    ServerOffline,
    WorkflowArgumentsError,
)
from comfyui_mcp_skills.domain.models import Job
from comfyui_mcp_skills.domain.workflow_schema import validate_arguments

GatewayFactory = Callable[[dict[str, Any]], ComfyUIGateway]
DIRECT_OUTPUT_COMPATIBILITY_REGISTRY_VERSION = 1
_DIRECT_OUTPUT_COMPATIBILITY = frozenset(
    {
        ("LoadImageOutput", "image", "image", "output"),
    }
)
logger = logging.getLogger(__name__)


def direct_output_compatible(
    consumer_class: str,
    parameter_type: str,
    field: str,
    storage_type: str,
) -> bool:
    """Return whether fixed compatibility evidence permits direct output reuse."""
    return (
        consumer_class,
        parameter_type,
        field,
        storage_type,
    ) in _DIRECT_OUTPUT_COMPATIBILITY


@dataclass(frozen=True, slots=True)
class RiskyNodeRule:
    """One extendable rule that flags a workflow node before submission.

    ``class_types`` matches exact class types (case-insensitive), ``class_tokens``
    matches whole identifier words of the class type, and ``input_tokens`` matches
    whole identifier words of the node's input names. Adding a category is one
    table entry instead of a new branch in the submission path.
    """

    category: str
    severity: str
    reason: str
    class_types: frozenset[str]
    class_tokens: frozenset[str]
    input_tokens: frozenset[str] = frozenset()


RISKY_NODE_RULES: tuple[RiskyNodeRule, ...] = (
    RiskyNodeRule(
        category="code_execution",
        severity="high",
        reason="executes arbitrary code or expressions on the ComfyUI host",
        class_types=frozenset(
            {
                "pythonnode",
                "pythonscript",
                "pythoncode",
                "pythoneval",
                "executepython",
                "evalnode",
                "eval",
                "shellexecute",
                "wasnodesuiteexec",
            }
        ),
        # "execute" and bare "code" were too broad: they flagged local-only
        # nodes such as ExecutePrompt or a node with an input named "code".
        class_tokens=frozenset({"python", "pyscript", "eval", "exec"}),
        input_tokens=frozenset(
            {"python_code", "source_code", "code_text", "script", "expression", "exec_command"}
        ),
    ),
    RiskyNodeRule(
        category="network_request",
        severity="high",
        reason="performs an outbound network request from the ComfyUI host",
        class_types=frozenset(
            {
                "httprequest",
                "http_request",
                "webrequest",
                "requests",
                "urlfetch",
                "urlfetcher",
                "fetchurl",
                "downloadurl",
                "downloadandload",
                "loadimagefromurl",
                "loadimageurl",
                "webhook",
            }
        ),
        class_tokens=frozenset(
            {"http", "https", "url", "fetch", "download", "webhook", "request"}
        ),
        input_tokens=frozenset({"url", "urls", "uri", "endpoint", "webhook", "proxy"}),
    ),
    RiskyNodeRule(
        category="external_api",
        severity="high",
        reason="calls a paid or external model API instead of the local ComfyUI runtime",
        class_types=frozenset(
            {
                "openai",
                "openaiapi",
                "claude",
                "gemini",
                "fluxapi",
                "replicate",
                "runway",
                "kling",
                "midjourney",
                "externalapi",
            }
        ),
        # Bare "api" matched local nodes like APIImageLoader; the vendor names
        # below still catch every external provider this rule targets.
        class_tokens=frozenset(
            {
                "openai",
                "anthropic",
                "claude",
                "gemini",
                "replicate",
                "fal",
                "runway",
                "kling",
                "midjourney",
                "cloud",
            }
        ),
        # Input names are tokenized on underscores, so ``api_key`` arrives as
        # ``api`` + ``key``; the credential input itself is the signal here.
        input_tokens=frozenset({"api", "apikey"}),
    ),
)


class RiskyWorkflowNodesError(WorkflowArgumentsError):
    """Submission found nodes that execute code, reach the network, or call paid APIs."""

    code = "RISKY_WORKFLOW_NODES"

    def __init__(self, findings: tuple[dict[str, str], ...]) -> None:
        summary = ", ".join(
            f"{item['node_id']}({item['class_type']}:{item['category']})"
            for item in findings
        )
        super().__init__(
            "Workflow contains nodes outside the local runtime contract: "
            f"{summary}. Pass allow_risky_nodes=true to submit anyway.",
            details={"risky_nodes": [dict(item) for item in findings], "allow_risky_nodes": True},
        )


def _identifier_tokens(value: str) -> tuple[str, ...]:
    """Split one class or field identifier into lowercase whole-word tokens."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
    return tuple(token.casefold() for token in re.findall(r"[A-Za-z0-9]+", spaced))


def _node_sort_key(value: object) -> tuple[int, int | str]:
    text = str(value)
    return (0, int(text)) if text.isdigit() else (1, text)


def scan_risky_nodes(graph: dict[str, Any]) -> tuple[dict[str, str], ...]:
    """Return structured findings for nodes outside the local runtime contract.

    Pure and deterministic: no I/O, sorted by node id, driven entirely by
    ``RISKY_NODE_RULES`` so the policy stays unit-testable on its own.
    """
    findings: list[dict[str, str]] = []
    for raw_node_id in sorted(graph, key=_node_sort_key):
        node = graph[raw_node_id]
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type", "")).strip()
        if not class_type:
            continue
        class_key = class_type.casefold()
        class_tokens = set(_identifier_tokens(class_type))
        inputs = node.get("inputs")
        input_tokens: set[str] = set()
        if isinstance(inputs, dict):
            for field in inputs:
                input_tokens.update(_identifier_tokens(str(field)))
        for rule in RISKY_NODE_RULES:
            matched = class_type if class_key in rule.class_types else ""
            if not matched:
                shared = sorted(class_tokens & rule.class_tokens)
                if shared:
                    matched = shared[0]
            if not matched:
                shared_inputs = sorted(input_tokens & rule.input_tokens)
                if shared_inputs:
                    matched = f"input:{shared_inputs[0]}"
            if matched:
                findings.append(
                    {
                        "node_id": str(raw_node_id),
                        "class_type": class_type,
                        "category": rule.category,
                        "severity": rule.severity,
                        "reason": rule.reason,
                        "matched": matched,
                    }
                )
                break
    return tuple(findings)


class ExecutionService:
    def __init__(
        self,
        catalog: WorkflowCatalog,
        servers: ServerRegistry,
        runs: RunRepository,
        assets: AssetRepository,
        gateway_factory: GatewayFactory,
        planning: ExecutionPlanningService | None = None,
        artifacts: ArtifactRepository | None = None,
        risky_node_audit: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._catalog = catalog
        self._servers = servers
        self._runs = runs
        self._assets = assets
        self._gateway_factory = gateway_factory
        self._planning = planning
        self._artifacts = artifacts
        self._risky_node_audit = risky_node_audit

    def submit(
        self,
        server_id: str,
        workflow_id: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str = "",
        owner_id: str = "",
        client_id: str = "",
        revision_id: str = "",
        deployment_id: str = "",
        content_digest: str = "",
        retry_of: str = "",
        server_connection: dict[str, Any] | None = None,
        priority: float | None = None,
        targets: tuple[str, ...] = (),
        overrides: dict[str, Any] | None = None,
        allow_risky_nodes: bool = False,
    ) -> Job:
        if not isinstance(idempotency_key, str) or len(idempotency_key) > 256:
            raise ValueError("idempotency_key must be a string up to 256 characters")
        if idempotency_key and (
            idempotency_key.strip() != idempotency_key
            or any(ord(char) < 33 or ord(char) == 127 for char in idempotency_key)
        ):
            raise ValueError("idempotency_key must contain printable non-whitespace characters")
        if not isinstance(client_id, str) or len(client_id) > 128:
            raise ValueError("client_id must be a string up to 128 characters")
        if client_id and (
            client_id.strip() != client_id
            or any(ord(char) < 33 or ord(char) == 127 for char in client_id)
        ):
            raise ValueError("client_id must contain printable non-whitespace characters")
        # Canonicalize once: the same normalized mapping feeds the request digest,
        # the executed graph, and the immutable Plan snapshot.
        normalized_overrides = normalize_overrides(overrides)
        pin = (revision_id, deployment_id, content_digest)
        if any(pin) and not all(pin):
            raise ValueError("execution pin is incomplete")
        if all(pin):
            if self._planning is None:
                raise RuntimeError("pinned execution planning is unavailable")
            workflow = self._planning.pinned_workflow(
                server_id=server_id,
                workflow_id=workflow_id,
                revision_id=revision_id,
                deployment_id=deployment_id,
                content_digest=content_digest,
                owner_id=owner_id,
            )
        else:
            workflow = self._catalog.get(server_id, workflow_id)
        validate_arguments(workflow.parameters, arguments)
        resolved = self._resolve_assets(
            server_id, workflow.parameters, workflow.graph, arguments, owner_id
        )
        digest_arguments = arguments
        if priority is not None or targets or normalized_overrides:
            # Normalize before hashing: priority is float, targets are set-like
            # (order is meaningless to ComfyUI), so semantically identical
            # requests always produce the same digest regardless of caller.
            digest_arguments = {
                **arguments,
                "_execution": {
                    **(
                        {"priority": float(priority)}
                        if priority is not None
                        else {}
                    ),
                    **(
                        {"partial_execution_targets": sorted(targets)}
                        if targets
                        else {}
                    ),
                },
            }
            if normalized_overrides:
                digest_arguments["_overrides"] = normalized_overrides
        request_digest = self._runs.request_digest(workflow_id, digest_arguments)
        client_id = client_id or uuid.uuid4().hex
        connection = (
            self._servers.connection(server_id) if server_connection is None else server_connection
        )
        gateway = self._gateway_factory(connection)
        graph = self._inject(workflow.graph, workflow.parameters, resolved)
        if normalized_overrides:
            _apply_overrides(graph, normalized_overrides, parameters=workflow.parameters)
        if targets and any(target not in graph for target in targets):
            raise ValueError(
                "partial_execution_targets references a node missing from the "
                "resolved workflow graph"
            )
        risky_findings = scan_risky_nodes(graph)
        if risky_findings:
            if not allow_risky_nodes:
                raise RiskyWorkflowNodesError(risky_findings)
            self._record_risky_allowance(
                server_id=server_id,
                workflow_id=workflow_id,
                owner_id=owner_id,
                client_id=client_id,
                findings=risky_findings,
            )
        admission_token = self._runs.admit(server_id)
        try:
            lease_token = ""
            if idempotency_key:
                claimed = self._runs.claim(
                    server_id,
                    workflow_id,
                    idempotency_key,
                    arguments,
                    owner_id=owner_id,
                    client_id=client_id,
                    request_digest=request_digest,
                )
                if claimed is None:
                    existing = self._runs.get_by_idempotency(server_id, idempotency_key, owner_id)
                    claim = self._runs.get_claim(server_id, idempotency_key, owner_id)
                    if (
                        claim is None
                        or claim.get("workflow_id") != workflow_id
                        or claim.get("request_digest") != request_digest
                    ):
                        raise IdempotencyConflict(
                            "Idempotency key was already used for a different request"
                        )
                    if existing is not None:
                        self._verify_pins(
                            existing,
                            revision_id=revision_id,
                            deployment_id=deployment_id,
                        )
                        return existing
                    try:
                        recovered_prompt = self._find_prompt_by_client_id(
                            gateway, str(claim.get("client_id", ""))
                        )
                    except Exception as exc:
                        raise self._submission_unknown(
                            server_id,
                            idempotency_key,
                            str(claim.get("lease_token") or ""),
                            owner_id,
                            None,
                            exc,
                        ) from exc
                    if recovered_prompt:
                        if self._planning is not None:
                            recovered_identity = self._planning.identity_for_client(
                                server_id, str(claim.get("client_id", ""))
                            )
                            if recovered_identity is None:
                                raise ExecutionInProgress(
                                    "Recovered upstream submission has no canonical G4 identity"
                                )
                            if revision_id and (
                                recovered_identity.revision_id != revision_id
                                or recovered_identity.deployment_id != deployment_id
                            ):
                                raise IdempotencyConflict(
                                    "Recovered upstream submission has different execution pins"
                                )
                            try:
                                self._planning.finalize_submission(
                                    recovered_identity,
                                    upstream_prompt_id=recovered_prompt,
                                    idempotency_key=idempotency_key,
                                    request_digest=request_digest,
                                    lease_token=str(claim.get("lease_token") or ""),
                                )
                            except Exception as exc:
                                raise self._submission_unknown(
                                    server_id,
                                    idempotency_key,
                                    str(claim.get("lease_token") or ""),
                                    owner_id,
                                    recovered_identity,
                                    exc,
                                ) from exc
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
                        try:
                            self._runs.save(
                                recovered,
                                lease_token=str(claim.get("lease_token") or ""),
                            )
                        except Exception as exc:
                            raise self._submission_unknown(
                                server_id,
                                idempotency_key,
                                str(claim.get("lease_token") or ""),
                                owner_id,
                                None,
                                exc,
                            ) from exc
                        return recovered
                    raise ExecutionInProgress(
                        "submission outcome is unknown; retry status reconciliation"
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
                        revision_id=revision_id,
                        deployment_id=deployment_id,
                        content_digest=content_digest,
                        retry_of=retry_of,
                        overrides=normalized_overrides or None,
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
            try:
                queued = gateway.queue_prompt(
                    graph,
                    client_id=client_id,
                    targets=list(targets) if targets else None,
                    priority=priority,
                )
            except Exception as exc:
                if execution_identity is not None:
                    raise self._submission_unknown(
                        server_id,
                        idempotency_key,
                        lease_token,
                        owner_id,
                        execution_identity,
                        exc,
                    ) from exc
                if isinstance(exc, ServerOffline):
                    # ServerOffline here is ambiguous (connection refused vs
                    # timeout vs 5xx all collapse into it); the key must stay
                    # in submission_unknown so client-id reconciliation or the
                    # reconciler can later resolve or expire it safely.
                    self._runs.mark_submission_unknown(
                        server_id, idempotency_key, lease_token, owner_id
                    )
                    raise
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
                if execution_identity is not None:
                    unknown = ServerOffline("ComfyUI submission outcome is unknown")
                    raise self._submission_unknown(
                        server_id,
                        idempotency_key,
                        lease_token,
                        owner_id,
                        execution_identity,
                        unknown,
                    ) from unknown
                self._runs.mark_submission_unknown(
                    server_id, idempotency_key, lease_token, owner_id
                )
                raise ServerOffline("ComfyUI submission outcome is unknown")
            if execution_identity is not None:
                assert self._planning is not None
                try:
                    self._planning.finalize_submission(
                        execution_identity,
                        upstream_prompt_id=prompt_id,
                        upstream_job_id=upstream_job_id,
                        idempotency_key=idempotency_key,
                        request_digest=request_digest,
                        lease_token=lease_token,
                    )
                except Exception as exc:
                    raise self._submission_unknown(
                        server_id,
                        idempotency_key,
                        lease_token,
                        owner_id,
                        execution_identity,
                        exc,
                    ) from exc
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
        finally:
            self._runs.release_admission(admission_token)

    @staticmethod
    def _verify_pins(job: Job, *, revision_id: str, deployment_id: str) -> None:
        if revision_id and (job.revision_id != revision_id or job.deployment_id != deployment_id):
            raise IdempotencyConflict("Idempotency key resolved to different execution pins")

    def _record_risky_allowance(
        self,
        *,
        server_id: str,
        workflow_id: str,
        owner_id: str,
        client_id: str,
        findings: tuple[dict[str, str], ...],
    ) -> None:
        """Record an explicit ``allow_risky_nodes`` decision before submission.

        The structured event is always logged; an injected audit sink (for example
        the admin audit log) receives the same payload so the allowance never
        disappears from the trail.
        """
        event = {
            "event": "risky_nodes_allowed",
            "server_id": server_id,
            "workflow_id": workflow_id,
            "owner_id": owner_id,
            "client_id": client_id,
            "risky_nodes": [dict(item) for item in findings],
        }
        logger.warning("risky workflow nodes explicitly allowed: %s", event)
        if self._risky_node_audit is not None:
            try:
                self._risky_node_audit(event)
            except Exception:
                logger.exception("risky node audit sink failed")

    def _submission_unknown(
        self,
        server_id: str,
        idempotency_key: str,
        lease_token: str,
        owner_id: str,
        identity: ExecutionIdentity | None,
        error: Exception,
    ) -> ExecutionInProgress:
        try:
            self._runs.mark_submission_unknown(server_id, idempotency_key, lease_token, owner_id)
        except Exception:
            pass
        if identity is not None and self._planning is not None:
            try:
                self._planning.mark_submission_unknown(identity, str(error))
            except Exception:
                pass
        return ExecutionInProgress(
            f"submission outcome is unknown for idempotency key "
            f"{idempotency_key!r} (job_id={getattr(identity, 'job_id', '') or 'pending'}) "
            "on server {server_id!r}; poll job status or retry the same key with the "
            "same arguments to reconcile".format(server_id=server_id)
        )

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
        graph: dict[str, Any],
        arguments: dict[str, Any],
        owner_id: str = "",
    ) -> dict[str, Any]:
        resolved = dict(arguments)
        for name, value in arguments.items():
            metadata = parameters.get(name, {})
            parameter_type = str(metadata.get("type", ""))
            if parameter_type not in {"image", "mask", "audio", "video"}:
                continue
            consumer_class = self._consumer_class(graph, metadata, name)
            direct_output_consumer = self._direct_output_compatible(
                consumer_class, metadata, parameter_type
            )
            output_reference = isinstance(value, str) and value.startswith(
                ("comfyui://outputs/", "comfyui://artifacts/")
            )
            if output_reference:
                if not direct_output_consumer:
                    raise AssetNotFound(
                        f'Media parameter "{name}" must use an imported Asset for {consumer_class}'
                    )
                resolved[name] = self._resolve_output(
                    server_id,
                    value,
                    parameter_type,
                    owner_id,
                )
                continue
            if direct_output_consumer:
                raise AssetNotFound(f'Media parameter "{name}" requires an output URI')
            if not isinstance(value, str) or not value.startswith("asset_"):
                if owner_id or consumer_class == "LoadImage":
                    raise AssetNotFound(f'Media parameter "{name}" requires an authorized asset_id')
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
    def _consumer_class(
        graph: dict[str, Any], metadata: dict[str, Any], parameter_name: str
    ) -> str:
        node_id = str(metadata.get("node_id", ""))
        field = str(metadata.get("field", ""))
        node = graph.get(node_id)
        inputs = node.get("inputs") if isinstance(node, dict) else None
        consumer_class = str(node.get("class_type", "")) if isinstance(node, dict) else ""
        if (
            not node_id
            or not field
            or not consumer_class
            or not isinstance(inputs, dict)
            or field not in inputs
        ):
            raise WorkflowArgumentsError(
                f'Workflow parameter "{parameter_name}" has an invalid target'
            )
        return consumer_class

    @staticmethod
    def _direct_output_compatible(
        consumer_class: str, metadata: dict[str, Any], parameter_type: str
    ) -> bool:
        return direct_output_compatible(
            consumer_class,
            parameter_type,
            str(metadata.get("field", "")),
            str(metadata.get("storage_type", "")),
        )

    def _resolve_output(
        self,
        server_id: str,
        uri: str,
        parameter_type: str,
        owner_id: str,
    ) -> str:
        legacy = parse_legacy_resource_uri(uri)
        artifact_match = re.fullmatch(
            r"comfyui://artifacts/(artifact_[0-9a-f]+)", uri, flags=re.ASCII
        )
        if artifact_match is not None:
            artifact_id = artifact_match.group(1)
            try:
                validate_control_plane_id("artifact", artifact_id)
            except ValueError as exc:
                raise AssetNotFound(f"Invalid output URI: {uri}") from exc
            artifact = (
                self._artifacts.get_artifact(artifact_id, owner_id)
                if self._artifacts is not None
                else None
            )
            if artifact is None:
                raise AssetNotFound(f"Artifact not found: {artifact_id}")
            if artifact.server_id != server_id:
                raise AssetNotFound(f"Output does not belong to server: {server_id}")
            output = {
                "filename": artifact.filename,
                "subfolder": artifact.subfolder,
                "storage_type": artifact.storage_type,
                "media_type": artifact.media_type,
            }
        elif legacy is not None and legacy.kind == "output" and legacy.index is not None:
            if legacy.server_id != server_id:
                raise AssetNotFound(f"Output does not belong to server: {server_id}")
            artifact = (
                self._artifacts.resolve_artifact_alias(uri, owner_id)
                if self._artifacts is not None
                else None
            )
            if artifact is None:
                raise AssetNotFound(f"Output not found: {uri}")
            if artifact.server_id != server_id:
                raise AssetNotFound(f"Output does not belong to server: {server_id}")
            output = {
                "filename": artifact.filename,
                "subfolder": artifact.subfolder,
                "storage_type": artifact.storage_type,
                "media_type": artifact.media_type,
            }
        else:
            raise AssetNotFound(f"Invalid output URI: {uri}")
        expected_media = "image" if parameter_type == "mask" else parameter_type
        media_type = str(output.get("media_type", ""))
        if media_type != expected_media:
            raise AssetNotFound(f"Output is {media_type or 'unknown'}, expected {expected_media}")
        return self._comfyui_output_ref(uri, output)

    @staticmethod
    def _comfyui_output_ref(uri: str, output: dict[str, Any]) -> str:
        filename = str(output.get("filename", ""))
        subfolder = str(output.get("subfolder", ""))
        storage_type = str(output.get("storage_type", output.get("type", "")))
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


_MEDIA_PARAMETER_TYPES = frozenset({"image", "mask", "audio", "video"})
_MEDIA_FIELD_TOKENS = frozenset(
    {"image", "images", "image2", "mask", "masks", "video", "videos", "audio", "audios"}
)
_MEDIA_REFERENCE_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".bmp",
        ".gif",
        ".tif",
        ".tiff",
        ".mp4",
        ".webm",
        ".mov",
        ".mkv",
        ".avi",
        ".wav",
        ".mp3",
        ".flac",
        ".ogg",
        ".m4a",
    }
)
_MEDIA_OVERRIDE_MESSAGE = (
    'Override target "{target}" targets media input "{field}" ({kind}); media inputs '
    "cannot be set through _overrides. Upload the file through the asset upload channel "
    "(comfyui.asset.upload) or reuse an existing output URI (comfyui://artifacts/... or "
    "comfyui://outputs/...), then pass the returned asset_id through the workflow parameter."
)


def _looks_like_media_reference(value: str) -> bool:
    if value.startswith(("asset_", "comfyui://")):
        return True
    if value.endswith(" [output]"):
        return True
    return PurePosixPath(value.replace("\\", "/")).suffix.casefold() in _MEDIA_REFERENCE_SUFFIXES


def _media_override_kind(
    node_id: str,
    field: str,
    current_value: Any,
    parameters: dict[str, Any],
) -> str:
    """Return the media kind when an override targets an asset-reference input.

    Declared media parameters win, so a renamed input still resolves through the
    published schema; the remaining heuristics cover undeclared third-party
    loaders where the graph only exposes the raw field name or value.
    """
    for name in sorted(parameters):
        metadata = parameters[name]
        if not isinstance(metadata, dict):
            continue
        parameter_type = str(metadata.get("type", "")).casefold()
        if parameter_type not in _MEDIA_PARAMETER_TYPES:
            continue
        if str(metadata.get("node_id", "")) == node_id and str(metadata.get("field", "")) == field:
            return parameter_type
    for token in _identifier_tokens(field):
        if token in _MEDIA_FIELD_TOKENS:
            for kind in ("image", "mask", "video", "audio"):
                if token.startswith(kind):
                    return kind
    if isinstance(current_value, str) and _looks_like_media_reference(current_value):
        return "media"
    return ""


def _apply_overrides(
    graph: dict[str, Any],
    overrides: dict[str, Any],
    *,
    parameters: dict[str, Any] | None = None,
) -> None:
    """Apply one-run-only node input overrides to an injected graph copy.

    Targets use the ``<node_id>.<input_field>`` form and accept scalars only, so
    a one-off run can swap a checkpoint or a LoRA weight without publishing a new
    Revision. Rewiring a graph stays the job of the Revisions change surface.

    Media inputs are asset references and are rejected here: they must pass the
    same owner/server/media/lineage checks as ``ExecutionService._resolve_assets``,
    which only the workflow parameter channel performs.
    """
    normalized = normalize_overrides(overrides)
    declared = parameters or {}
    for target, value in normalized.items():
        node_id, _separator, field = target.partition(".")
        node = graph.get(node_id)
        if not isinstance(node, dict):
            raise WorkflowArgumentsError(
                f'Override target "{target}" must name "<node_id>.<input_field>" '
                "of an existing node"
            )
        inputs = node.get("inputs")
        if not isinstance(inputs, dict) or field not in inputs:
            raise WorkflowArgumentsError(
                f'Override target "{target}" references a missing input'
            )
        media_kind = _media_override_kind(node_id, field, inputs.get(field), declared)
        if media_kind:
            raise WorkflowArgumentsError(
                _MEDIA_OVERRIDE_MESSAGE.format(target=target, field=field, kind=media_kind),
                details={"target": target, "field": field, "parameter_type": media_kind},
            )
        inputs[field] = value
