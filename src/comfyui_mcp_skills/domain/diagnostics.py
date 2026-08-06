"""Versioned deterministic diagnostic rules independent from transports and persistence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from re import Pattern

DIAGNOSTIC_REGISTRY_VERSION = "diagnostic-rules-v1"


@dataclass(frozen=True, slots=True)
class DiagnosticAction:
    tool: str
    name: str
    required_arguments: tuple[str, ...]
    risk: str


@dataclass(frozen=True, slots=True)
class DiagnosticMatch:
    rule_id: str
    classification: str
    retryable: bool
    safe_actions: tuple[DiagnosticAction, ...]
    approval_actions: tuple[DiagnosticAction, ...]


@dataclass(frozen=True, slots=True)
class DiagnosticRule:
    rule_id: str
    classification: str
    retryable: bool
    pattern: Pattern[str]
    priority: int
    legacy_ordinal: int | None = None
    safe_actions: tuple[DiagnosticAction, ...] = ()
    approval_actions: tuple[DiagnosticAction, ...] = ()

    def matches(self, evidence_text: str) -> bool:
        return self.pattern.search(evidence_text) is not None

    def result(self) -> DiagnosticMatch:
        return DiagnosticMatch(
            self.rule_id,
            self.classification,
            self.retryable,
            self.safe_actions,
            self.approval_actions,
        )


def _action(tool: str, name: str, *arguments: str, risk: str = "safe") -> DiagnosticAction:
    return DiagnosticAction(tool, name, arguments, risk)


_DEPENDENCIES = _action(
    "comfyui.workflow.dependencies.check",
    "inspect_workflow_dependencies",
    "server_id",
    "workflow_id",
)
_SERVER_DIAGNOSE = _action(
    "comfyui.server.diagnose", "inspect_server_health_and_failures", "server_id"
)
_SERVER_CAPABILITIES = _action(
    "comfyui.server.capabilities", "inspect_server_capabilities", "server_id"
)
_WORKFLOW_DESCRIBE = _action(
    "comfyui.workflow.describe", "inspect_pinned_workflow", "server_id", "workflow_id"
)
_LOG_READ = _action("comfyui.log.read", "read_redacted_log_window", "server_id")
_RETRY_PLAN = _action(
    "comfyui.job.retry.plan", "plan_retry_from_original_arguments", "job_id", "changes"
)


def _regex(expression: str) -> Pattern[str]:
    return re.compile(expression, re.IGNORECASE)


def _literal(value: str) -> Pattern[str]:
    return re.compile(re.escape(value), re.IGNORECASE)


# Legacy ordinals preserve the exact order of the fourteen tuples in error_hints.py.
_RULES: tuple[DiagnosticRule, ...] = (
    DiagnosticRule(
        "legacy.cloud_api_unauthorized",
        "cloud_api_unauthorized",
        False,
        _literal("Unauthorized: Please login first"),
        10,
        0,
        (_SERVER_CAPABILITIES, _SERVER_DIAGNOSE),
    ),
    DiagnosticRule(
        "legacy.missing_vae",
        "missing_vae_model",
        False,
        _regex(r"vae.*(?:not found|no such file)"),
        20,
        1,
        (_DEPENDENCIES,),
    ),
    DiagnosticRule(
        "legacy.missing_clip",
        "missing_clip_model",
        False,
        _regex(r"clip.*(?:not found|no such file)"),
        30,
        2,
        (_DEPENDENCIES,),
    ),
    DiagnosticRule(
        "legacy.missing_lora",
        "missing_lora_model",
        False,
        _regex(r"lora.*(?:not found|no such file)"),
        40,
        3,
        (_DEPENDENCIES,),
    ),
    DiagnosticRule(
        "phase_n.missing_input",
        "missing_input",
        False,
        _regex(
            r"(?:input|upload|image|mask|audio|video).*(?:not found|no such file|does not exist)"
        ),
        45,
        safe_actions=(_WORKFLOW_DESCRIBE, _RETRY_PLAN),
    ),
    DiagnosticRule(
        "legacy.missing_ckpt", "missing_model", False, _literal("ckpt"), 50, 4, (_DEPENDENCIES,)
    ),
    DiagnosticRule(
        "legacy.missing_safetensors",
        "missing_model",
        False,
        _literal("safetensors"),
        60,
        5,
        (_DEPENDENCIES,),
    ),
    DiagnosticRule(
        "legacy.missing_custom_node",
        "missing_node",
        False,
        _regex(r"cannot find class|class_type not found|node not found"),
        70,
        6,
        (_DEPENDENCIES,),
    ),
    DiagnosticRule(
        "legacy.invalid_prompt",
        "invalid_prompt",
        False,
        _regex(r"prompt is not valid|invalid prompt"),
        80,
        7,
        (_WORKFLOW_DESCRIBE,),
    ),
    DiagnosticRule(
        "phase_n.interrupted",
        "interrupted",
        True,
        _regex(r"\b(?:execution (?:was )?interrupted|interrupted|cancelled)\b"),
        85,
        safe_actions=(_RETRY_PLAN,),
    ),
    DiagnosticRule(
        "legacy.connection_refused",
        "server_offline",
        True,
        _literal("Connection refused"),
        90,
        8,
        (_SERVER_DIAGNOSE,),
    ),
    DiagnosticRule(
        "phase_n.server_offline",
        "server_offline",
        True,
        _regex(r"\b(?:server is )?(?:offline|unreachable)\b"),
        95,
        safe_actions=(_SERVER_DIAGNOSE,),
    ),
    DiagnosticRule(
        "legacy.connection_timeout",
        "server_timeout",
        True,
        _regex(r"timed?\s*out|timeout"),
        100,
        9,
        (_SERVER_DIAGNOSE,),
    ),
    DiagnosticRule(
        "legacy.cuda_oom",
        "out_of_memory",
        True,
        _literal("CUDA out of memory"),
        110,
        10,
        (_RETRY_PLAN,),
    ),
    DiagnosticRule(
        "legacy.mps_oom",
        "out_of_memory",
        True,
        _literal("MPS out of memory"),
        120,
        11,
        (_RETRY_PLAN,),
    ),
    DiagnosticRule(
        "legacy.cuda_driver",
        "gpu_driver_error",
        False,
        _regex(r"CUDA error|CUDA driver|no CUDA GPUs"),
        130,
        12,
        (_SERVER_DIAGNOSE,),
    ),
    DiagnosticRule(
        "phase_n.type_mismatch",
        "type_mismatch",
        False,
        _regex(r"type mismatch|invalid (?:input )?type|expected\s+[^\n;]+\s+(?:but\s+)?got"),
        135,
        safe_actions=(_WORKFLOW_DESCRIBE, _RETRY_PLAN),
    ),
    DiagnosticRule(
        "legacy.file_not_found",
        "missing_input",
        False,
        _regex(r"FileNotFoundError|No such file or directory"),
        140,
        13,
        (_WORKFLOW_DESCRIBE, _RETRY_PLAN),
    ),
)

_UNKNOWN = DiagnosticMatch(
    "phase_n.unknown_failure", "unknown_failure", False, (_LOG_READ, _SERVER_DIAGNOSE), ()
)


class DiagnosticRuleRegistry:
    """Immutable ordered registry with deterministic first-match classification."""

    def __init__(
        self,
        rules: tuple[DiagnosticRule, ...],
        *,
        version: str = DIAGNOSTIC_REGISTRY_VERSION,
    ) -> None:
        if version != DIAGNOSTIC_REGISTRY_VERSION:
            raise ValueError("unsupported diagnostic registry version")
        if not rules or tuple(sorted(rules, key=lambda rule: rule.priority)) != rules:
            raise ValueError("diagnostic rules must have ascending priority")
        if len({rule.priority for rule in rules}) != len(rules):
            raise ValueError("diagnostic rule priorities must be unique")
        legacy_ordinals = tuple(
            rule.legacy_ordinal for rule in rules if rule.legacy_ordinal is not None
        )
        if legacy_ordinals != tuple(range(14)):
            raise ValueError("diagnostic registry must preserve all fourteen ordered legacy rules")
        self._rules = rules
        self._version = version

    @classmethod
    def default(cls) -> DiagnosticRuleRegistry:
        return cls(_RULES)

    @property
    def version(self) -> str:
        return self._version

    @property
    def rules(self) -> tuple[DiagnosticRule, ...]:
        return self._rules

    @property
    def legacy_rules(self) -> tuple[DiagnosticRule, ...]:
        return tuple(rule for rule in self._rules if rule.legacy_ordinal is not None)

    def classify(self, evidence_text: str) -> DiagnosticMatch:
        if not isinstance(evidence_text, str):
            raise TypeError("diagnostic evidence text must be a string")
        for rule in self._rules:
            if rule.matches(evidence_text):
                return rule.result()
        return _UNKNOWN
