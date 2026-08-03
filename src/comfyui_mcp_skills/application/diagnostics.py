"""Owner-bound deterministic diagnostic reports."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from comfyui_mcp_skills.application.diagnostic_ports import DiagnosticRepository
from comfyui_mcp_skills.domain.control_plane import validate_control_plane_id
from comfyui_mcp_skills.domain.diagnostics import (
    DiagnosticAction,
    DiagnosticRuleRegistry,
)
from comfyui_mcp_skills.domain.errors import DiagnosticNotFound
from comfyui_mcp_skills.domain.identifiers import validate_identifier

_MAX_EVENTS = 8
_MAX_LOG_LINES = 8
_MAX_EVIDENCE_TEXT = 32_768
_MAX_FIELD = 512
_AUTHORIZATION = re.compile(
    r"""(?ix)(["']?authorization["']?\s*[:=]\s*["']?)(?:basic|bearer)\s+[A-Za-z0-9._~+/=-]+["']?"""
)
_SECRET = re.compile(
    r"""(?ix)(["']?(?:api[_-]?key|token|password|passwd|secret)["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}\]]+)"""
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_WINDOWS_PATH = re.compile(r"(?i)(?:[A-Za-z]:\\|\\\\)[^\s'\"]+")
_POSIX_PATH = re.compile(r"(?<![A-Za-z0-9])/(?:[^\s'\"]+/)+[^\s'\"]*")


def _owner(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
        raise ValueError("owner_id must contain between 1 and 256 characters")
    return value


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat()


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _redact(value: object) -> str:
    text = str(value).replace("\x00", "")
    text = _AUTHORIZATION.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
    text = _SECRET.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _WINDOWS_PATH.sub("[PATH]", text)
    text = _POSIX_PATH.sub("[PATH]", text)
    return text[:_MAX_FIELD]


def _action_dict(action: DiagnosticAction, context: Mapping[str, Any]) -> dict[str, Any] | None:
    values: dict[str, Any] = {}
    for name in action.required_arguments:
        if name == "changes":
            values[name] = {}
        elif name == "free_memory":
            values[name] = True
        elif name in context and context[name] not in (None, ""):
            values[name] = context[name]
        else:
            return None
    return {
        "tool": action.tool,
        "name": action.name,
        "required_arguments": values,
        "risk": action.risk,
    }


def _public_report(report: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "diagnostic_id",
        "registry_version",
        "subject_uri",
        "classification",
        "rule_id",
        "retryable",
        "evidence",
        "safe_actions",
        "approval_actions",
        "created_at",
        "resource_uri",
    )
    return {key: copy.deepcopy(report[key]) for key in keys}


class DiagnosticService:
    def __init__(
        self,
        repository: DiagnosticRepository,
        registry: DiagnosticRuleRegistry | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._registry = registry or DiagnosticRuleRegistry.default()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def diagnose_job(self, job_id: str, owner_id: str) -> dict[str, Any]:
        job_id = validate_control_plane_id("job", job_id)
        owner_id = _owner(owner_id)
        context = self._repository.get_job_diagnostic_context(
            job_id,
            owner_id,
            event_limit=_MAX_EVENTS,
            log_line_limit=_MAX_LOG_LINES,
        )
        if context is None:
            raise DiagnosticNotFound(
                "Job diagnostic subject was not found", details={"job_id": job_id}
            )
        return self._build_report(context, f"comfyui://jobs/{job_id}", owner_id)

    def diagnose_server(self, server_id: str, owner_id: str) -> dict[str, Any]:
        server_id = validate_identifier(server_id, field="server_id")
        owner_id = _owner(owner_id)
        context = self._repository.get_server_diagnostic_context(
            server_id,
            owner_id,
            event_limit=_MAX_EVENTS,
            log_line_limit=_MAX_LOG_LINES,
        )
        if context is None:
            raise DiagnosticNotFound(
                "Server diagnostic subject was not found", details={"server_id": server_id}
            )
        return self._build_report(context, f"comfyui://servers/{server_id}", owner_id)

    def get(self, diagnostic_id: str, owner_id: str) -> dict[str, Any]:
        diagnostic_id = validate_identifier(diagnostic_id, field="diagnostic_id")
        owner_id = _owner(owner_id)
        report = self._repository.get_diagnostic(diagnostic_id, owner_id)
        if report is None:
            raise DiagnosticNotFound(
                "Diagnostic report was not found", details={"diagnostic_id": diagnostic_id}
            )
        return _public_report(report)

    def _build_report(
        self, context: Mapping[str, Any], subject_uri: str, owner_id: str
    ) -> dict[str, Any]:
        failed_node_raw = context.get("failed_node")
        failed_node: dict[str, str] = {}
        if isinstance(failed_node_raw, Mapping):
            failed_node = {
                key: _redact(failed_node_raw[key])
                for key in ("node_id", "class_type", "error_type", "message")
                if key in failed_node_raw
            }
        raw_events = context.get("events", [])
        events = raw_events if isinstance(raw_events, list) else []
        event_rows = [
            {
                "event_type": _redact(item.get("event_type", "")),
                "occurred_at": _redact(item.get("occurred_at", "")),
                "message": _redact(item.get("message", "")),
            }
            for item in events[:_MAX_EVENTS]
            if isinstance(item, Mapping)
        ]
        raw_logs = context.get("log_lines", [])
        logs = raw_logs if isinstance(raw_logs, list) else []
        log_window = [_redact(item) for item in logs[-_MAX_LOG_LINES:]]
        evidence = {
            "status": _redact(context.get("status", "")),
            "failed_node": failed_node,
            "events": event_rows,
            "log_window": log_window,
        }
        primary_parts = [
            str(context.get("classification_text", context.get("error", ""))),
            str(context.get("status", "")),
        ]
        if isinstance(failed_node_raw, Mapping):
            primary_parts.extend(str(value) for value in failed_node_raw.values())
        match = self._registry.classify("\n".join(primary_parts)[:_MAX_EVIDENCE_TEXT])
        if match.rule_id == "phase_n.unknown_failure":
            secondary_parts = [
                *(str(item.get("message", "")) for item in events if isinstance(item, Mapping)),
                *(str(item) for item in logs),
            ]
            match = self._registry.classify("\n".join(secondary_parts)[:_MAX_EVIDENCE_TEXT])
        if str(context.get("status", "")).lower() == "interrupted":
            match = self._registry.classify("execution was interrupted")
        action_context = {
            "job_id": context.get("job_id"),
            "server_id": context.get("server_id"),
            "workflow_id": context.get("workflow_id"),
        }
        safe_actions = [
            hydrated
            for action in match.safe_actions
            if (hydrated := _action_dict(action, action_context)) is not None
        ]
        approval_actions = [
            hydrated
            for action in match.approval_actions
            if (hydrated := _action_dict(action, action_context)) is not None
        ]
        created_at = _time(self._clock())
        report_id = "diagnostic_" + _digest(
            [owner_id, subject_uri, self._registry.version, match.rule_id, evidence, created_at]
        )
        report = {
            "diagnostic_id": report_id,
            "owner_id": owner_id,
            "registry_version": self._registry.version,
            "subject_uri": subject_uri,
            "classification": match.classification,
            "rule_id": match.rule_id,
            "retryable": match.retryable,
            "evidence": evidence,
            "safe_actions": safe_actions,
            "approval_actions": approval_actions,
            "created_at": created_at,
            "resource_uri": f"comfyui://diagnostics/{report_id}",
        }
        return _public_report(self._repository.save_diagnostic(report))


from comfyui_mcp_skills.application.retries import RetryService  # noqa: E402

__all__ = ["DiagnosticService", "RetryService"]
