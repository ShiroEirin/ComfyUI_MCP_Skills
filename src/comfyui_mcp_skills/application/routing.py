"""Deterministic multi-server routing and digest-bound execution plans."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from comfyui_mcp_skills.domain.errors import WorkflowArgumentsError
from comfyui_mcp_skills.domain.identifiers import validate_identifier
from comfyui_mcp_skills.domain.models import Job
from comfyui_mcp_skills.domain.workflow_schema import validate_arguments


class RoutingRepository(Protocol):
    def list_routing_contexts(self, owner_id: str, workflow_id: str) -> list[dict[str, Any]]: ...
    def resolve_server_connection(
        self, owner_id: str, server_id: str, revision: int, config_digest: str
    ) -> dict[str, Any] | None: ...

    def save_routing_plan(self, plan: dict[str, Any]) -> dict[str, Any]: ...

    def get_routing_plan(self, plan_id: str, owner_id: str) -> dict[str, Any] | None: ...

    def claim_routing_commit(
        self, plan_id: str, plan_digest: str, owner_id: str, idempotency_digest: str
    ) -> None: ...
    def mark_routing_plan_committed(
        self, plan_id: str, plan_digest: str, owner_id: str, job_id: str
    ) -> dict[str, Any]: ...


class ExecutionSubmitter(Protocol):
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
    ) -> Job | dict[str, Any]: ...


RoutingProbe = Callable[[dict[str, Any]], dict[str, Any]]


class RoutingService:
    """Select one compatible Deployment and commit exactly the reviewed plan."""

    def __init__(
        self,
        repository: RoutingRepository,
        executor: ExecutionSubmitter,
        *,
        probe: RoutingProbe | None = None,
    ) -> None:
        self._repository = repository
        self._executor = executor
        self._probe = probe

    def plan(
        self,
        owner_id: str,
        workflow_id: str,
        arguments: dict[str, Any],
        *,
        server_id: str = "",
        policy: Mapping[str, Any] | None = None,
        submission_window: int = 0,
        request_id: str = "",
    ) -> dict[str, Any]:
        owner_id = _identity(owner_id, "owner_id")
        workflow_id = validate_identifier(workflow_id, field="workflow_id")
        locked_server = validate_identifier(server_id, field="server_id") if server_id else ""
        if not isinstance(arguments, dict):
            raise TypeError("arguments must be an object")
        if isinstance(submission_window, bool) or not isinstance(submission_window, int):
            raise TypeError("submission_window must be an integer")
        if not 0 <= submission_window <= 10_000:
            raise ValueError("submission_window must be between 0 and 10000")
        request_id = _identity(request_id or uuid.uuid4().hex, "request_id")
        normalized_policy = _policy(policy or {})
        violations = _policy_violations(arguments, normalized_policy)
        if violations:
            raise ValueError("Policy rejected execution: " + ", ".join(violations))
        request_payload = {
            "owner_id": owner_id,
            "request_id": request_id,
            "workflow_id": workflow_id,
            "arguments": _json_copy(arguments),
            "policy": normalized_policy,
            "locked_server_id": locked_server,
            "submission_window": submission_window,
        }
        request_input_digest = _digest(request_payload)
        plan_id = "routing_plan_" + _digest({"owner_id": owner_id, "request_id": request_id})
        existing = self._repository.get_routing_plan(plan_id, owner_id)
        if existing is not None:
            if existing.get("request_input_digest") != request_input_digest:
                raise ValueError("Routing request_id was reused with different inputs")
            return existing

        contexts = self._repository.list_routing_contexts(owner_id, workflow_id)
        if self._probe is not None:
            contexts = [self._probe(context) for context in contexts]
        candidates = [
            _candidate(context, arguments, normalized_policy, locked_server) for context in contexts
        ]
        candidates.sort(key=_candidate_order)
        eligible = [item for item in candidates if not item["exclusion_reasons"]]
        if not eligible:
            raise LookupError("No compatible published Deployment is available")
        selected = eligible[0]
        if locked_server and selected["server_id"] != locked_server:
            raise LookupError("Caller-locked Server is unavailable")
        slots = selected["execution_slots"]
        if submission_window > int(selected["subject_submission_quota"]):
            raise ValueError("submission_window exceeds subject submission quota")
        payload = {
            "owner_id": owner_id,
            "request_id": request_id,
            "request_input_digest": request_input_digest,
            "workflow_id": workflow_id,
            "arguments": _json_copy(arguments),
            "policy": normalized_policy,
            "selected_server_id": selected["server_id"],
            "revision_id": selected["revision_id"],
            "deployment_id": selected["deployment_id"],
            "content_digest": selected["content_digest"],
            "server_revision": selected["server_revision"],
            "server_config_digest": selected["server_config_digest"],
            "execution_slots": slots,
            "submission_window": submission_window,
            "reuse_mode": selected["reuse_mode"],
            "selection_reason": (
                "caller_locked_server" if locked_server else "lowest_eligible_queue_pressure"
            ),
            "estimate_available": False,
            "estimate": None,
            "candidates": candidates,
            "status": "planned",
        }
        plan_digest = _digest(payload)
        plan = {
            **payload,
            "plan_id": plan_id,
            "plan_digest": plan_digest,
            "resource_uri": "comfyui://plans/" + plan_id,
            "job_id": "",
        }
        return self._repository.save_routing_plan(plan)

    def explain(self, plan_id: str, owner_id: str) -> dict[str, Any]:
        plan = self._repository.get_routing_plan(plan_id, _identity(owner_id, "owner_id"))
        if plan is None:
            raise LookupError("Routing plan was not found")
        return plan

    def evaluate_policy(
        self, arguments: dict[str, Any], policy: Mapping[str, Any]
    ) -> dict[str, Any]:
        normalized = _policy(policy)
        violations = _policy_violations(arguments, normalized)
        return {
            "allowed": not violations,
            "violations": violations,
            "policy": normalized,
        }

    def commit(
        self,
        plan_id: str,
        plan_digest: str,
        owner_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        owner_id = _identity(owner_id, "owner_id")
        idempotency_key = _identity(idempotency_key, "idempotency_key")
        plan = self.explain(plan_id, owner_id)
        if plan.get("plan_digest") != plan_digest:
            raise ValueError("Routing plan digest conflict")
        idempotency_digest = _digest({"owner_id": owner_id, "key": idempotency_key})
        self._repository.claim_routing_commit(plan_id, plan_digest, owner_id, idempotency_digest)
        if plan.get("status") == "committed":
            return plan
        server_connection = self._repository.resolve_server_connection(
            owner_id,
            plan["selected_server_id"],
            int(plan["server_revision"]),
            str(plan["server_config_digest"]),
        )
        job = self._executor.submit(
            server_id=plan["selected_server_id"],
            workflow_id=plan["workflow_id"],
            arguments=_json_copy(plan["arguments"]),
            owner_id=owner_id,
            idempotency_key="routing-" + idempotency_digest,
            revision_id=plan["revision_id"],
            deployment_id=plan["deployment_id"],
            content_digest=plan["content_digest"],
            server_connection=server_connection,
        )
        job_id = job.job_id if isinstance(job, Job) else str(job.get("job_id", ""))
        if not job_id:
            raise RuntimeError("Execution commit did not return a canonical Job")
        return self._repository.mark_routing_plan_committed(plan_id, plan_digest, owner_id, job_id)


def _candidate(
    raw: Mapping[str, Any],
    arguments: dict[str, Any],
    policy: dict[str, Any],
    locked_server: str,
) -> dict[str, Any]:
    server_id = validate_identifier(str(raw.get("server_id", "")), field="server_id")
    parameters = raw.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("Routing candidate parameters are invalid")
    try:
        validate_arguments(parameters, arguments)
        argument_compatible = True
    except WorkflowArgumentsError:
        argument_compatible = False
    queue_depth = _integer(raw.get("queue_depth", 0), "queue_depth", minimum=0)
    slots = _integer(raw.get("execution_slots", 1), "execution_slots", minimum=1, maximum=64)
    quota = _integer(
        raw.get("subject_submission_quota", 0),
        "subject_submission_quota",
        minimum=0,
        maximum=10_000,
    )
    available_vram = _integer(raw.get("available_vram_bytes", 0), "available_vram_bytes", minimum=0)
    required_vram = _integer(raw.get("required_vram_bytes", 0), "required_vram_bytes", minimum=0)
    missing = raw.get("missing_dependencies", [])
    if not isinstance(missing, list) or any(not isinstance(item, str) for item in missing):
        raise ValueError("Routing candidate dependencies are invalid")
    exclusions: list[str] = []
    if raw.get("health_available", True) is not True:
        exclusions.append("server_health_unavailable")
    if not argument_compatible:
        exclusions.append("argument_schema_mismatch")
    if locked_server and server_id != locked_server:
        exclusions.append("caller_locked_other_server")
    if missing:
        exclusions.append("missing_dependencies")
    if required_vram > available_vram:
        exclusions.append("insufficient_vram")
    maximum_queue = policy.get("max_queue_depth")
    if isinstance(maximum_queue, int) and queue_depth > maximum_queue:
        exclusions.append("queue_depth_policy")
    reuse_mode = str(raw.get("reuse_mode", "upload"))
    server_revision = _integer(raw.get("server_revision", 0), "server_revision", minimum=0)
    server_config_digest = str(raw.get("server_config_digest", ""))
    if (server_revision == 0) != (server_config_digest == ""):
        raise ValueError("Routing candidate Server revision pin is incomplete")
    if server_config_digest and (
        len(server_config_digest) != 64
        or any(char not in "0123456789abcdef" for char in server_config_digest)
    ):
        raise ValueError("Routing candidate Server config digest is invalid")
    if reuse_mode not in {"direct", "copy", "upload", "none"}:
        raise ValueError("Routing candidate reuse_mode is invalid")
    return {
        "server_id": server_id,
        "revision_id": str(raw.get("revision_id", "")),
        "deployment_id": str(raw.get("deployment_id", "")),
        "content_digest": str(raw.get("content_digest", "")),
        "server_revision": server_revision,
        "server_config_digest": server_config_digest,
        "eligible": not exclusions,
        "exclusion_reasons": exclusions,
        "queue_depth": queue_depth,
        "execution_slots": slots,
        "subject_submission_quota": quota,
        "available_vram_bytes": available_vram,
        "required_vram_bytes": required_vram,
        "reuse_mode": reuse_mode,
        "score": queue_depth / slots,
    }


def _candidate_order(item: dict[str, Any]) -> tuple[bool, float, str]:
    return bool(item["exclusion_reasons"]), float(item["score"]), str(item["server_id"])


def _policy(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"max_steps", "max_queue_depth", "max_pixels", "max_outputs"}
    unexpected = set(value) - allowed
    if unexpected:
        raise ValueError("Unknown Policy fields: " + ", ".join(sorted(unexpected)))
    result: dict[str, Any] = {}
    for key in sorted(value):
        result[key] = _integer(value[key], key, minimum=0)
    return result


def _policy_violations(arguments: Mapping[str, Any], policy: Mapping[str, Any]) -> list[str]:
    checks = {
        "max_steps": arguments.get("steps"),
        "max_pixels": arguments.get("pixels"),
        "max_outputs": arguments.get("batch_size", arguments.get("outputs")),
    }
    return [
        key
        for key, actual in checks.items()
        if key in policy
        and isinstance(actual, (int, float))
        and not isinstance(actual, bool)
        and actual > policy[key]
    ]


def _integer(value: object, field: str, *, minimum: int, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


def _identity(value: object, field: str) -> str:
    text = str(value)
    if not text or len(text) > 256 or any(ord(char) < 33 or ord(char) == 127 for char in text):
        raise ValueError(f"{field} is invalid")
    return text


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _json_copy(value: Any) -> Any:
    return json.loads(_canonical_json(value))
