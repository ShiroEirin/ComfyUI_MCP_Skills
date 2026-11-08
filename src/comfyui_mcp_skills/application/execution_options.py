"""One validation contract for every submission surface.

``comfyui.workflow.run``, the dynamic ``comfyui.run.*`` tools, ``job.batch``
descriptors and repair/regenerate commits all reach the same queue, so they must
accept the same one-run inputs and reject the same invalid ones. Keeping the
rules here means a caller cannot gain a weaker check by choosing another entry
point that reaches the same execution service.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from comfyui_mcp_skills.application.planning import normalize_overrides

MAX_PARTIAL_TARGETS = 100
MAX_TARGET_LENGTH = 128
MAX_IDEMPOTENCY_KEY_LENGTH = 256
PRIORITY_MIN = -1000.0
PRIORITY_MAX = 1000.0
MAX_WAIT_SECONDS = 300.0
MAX_RETURNED_IMAGES = 4
DEFAULT_WAIT_SECONDS = 120.0

EXECUTION_FIELDS = frozenset(
    {
        "idempotency_key",
        "wait",
        "wait_timeout_seconds",
        "priority",
        "partial_execution_targets",
        "allow_risky_nodes",
        "return_images",
        "max_images",
    }
)


@dataclass(frozen=True, slots=True)
class ExecutionOptions:
    """Validated inputs for one submission.

    The waiting and image fields are adapter concerns -- nothing below the MCP
    layer waits for a Job or downloads a file -- so services consume only
    :meth:`submit_kwargs`, which carries exactly the
    :class:`~comfyui_mcp_skills.application.control_plane_ports.ExecutionSubmitter`
    keyword arguments.
    """

    idempotency_key: str = ""
    client_id: str = ""
    revision_id: str = ""
    deployment_id: str = ""
    content_digest: str = ""
    retry_of: str = ""
    priority: float | None = None
    targets: tuple[str, ...] = ()
    allow_risky_nodes: bool = False
    overrides: dict[str, Any] = field(default_factory=dict)
    wait: bool = False
    timeout_seconds: float = DEFAULT_WAIT_SECONDS
    return_images: bool = False
    max_images: int = 1

    def submit_kwargs(self) -> dict[str, Any]:
        """Keyword arguments accepted by ``ExecutionSubmitter.submit``."""
        kwargs: dict[str, Any] = {
            "idempotency_key": self.idempotency_key,
            "client_id": self.client_id,
            "revision_id": self.revision_id,
            "deployment_id": self.deployment_id,
            "content_digest": self.content_digest,
            "retry_of": self.retry_of,
        }
        if self.priority is not None:
            kwargs["priority"] = self.priority
        if self.targets:
            kwargs["targets"] = self.targets
        if self.allow_risky_nodes:
            kwargs["allow_risky_nodes"] = True
        if self.overrides:
            kwargs["overrides"] = dict(self.overrides)
        return kwargs


def parse_priority(value: object, *, field_name: str = "priority") -> float | None:
    """Return a finite queue priority inside the shared bounds, or ``None``."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    priority = float(value)
    if not math.isfinite(priority) or not PRIORITY_MIN <= priority <= PRIORITY_MAX:
        raise ValueError(f"{field_name} must be between {PRIORITY_MIN:g} and {PRIORITY_MAX:g}")
    return priority


def parse_targets(
    value: object,
    *,
    field_name: str = "partial_execution_targets",
    allow_empty: bool = True,
) -> tuple[str, ...]:
    """Return de-duplicated node IDs, preserving the caller's order."""
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, (list, tuple)):
        raise TypeError(f"{field_name} must be an array of node ids")
    if not value:
        if allow_empty:
            return ()
        raise TypeError(f"{field_name} must be a non-empty array")
    if len(value) > MAX_PARTIAL_TARGETS:
        raise ValueError(f"{field_name} must not exceed {MAX_PARTIAL_TARGETS} entries")
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or len(item) > MAX_TARGET_LENGTH
            or "\r" in item
            or "\n" in item
        ):
            raise TypeError(f"{field_name} entries must be node IDs without line breaks")
    return tuple(dict.fromkeys(value))


def parse_overrides(value: object) -> dict[str, Any]:
    """Validate one-run-only ``<node_id>.<input_field>`` overrides."""
    return normalize_overrides(value)


def parse_execution_options(raw: object) -> ExecutionOptions:
    """Validate the shared ``_execution`` block; unknown fields are refused."""
    if not isinstance(raw, dict):
        raise TypeError("_execution must be an object")
    unexpected = set(raw) - EXECUTION_FIELDS
    if unexpected:
        raise ValueError("Unexpected _execution fields: " + ", ".join(sorted(unexpected)))

    idempotency_key = raw.get("idempotency_key", "")
    if not isinstance(idempotency_key, str):
        raise TypeError("idempotency_key must be a string")
    if len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValueError(f"idempotency_key exceeds {MAX_IDEMPOTENCY_KEY_LENGTH} characters")

    wait = raw.get("wait", False)
    if not isinstance(wait, bool):
        raise TypeError("wait must be a boolean")

    timeout_raw = raw.get("wait_timeout_seconds", DEFAULT_WAIT_SECONDS)
    if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, (int, float)):
        raise TypeError("wait_timeout_seconds must be a number")
    timeout_seconds = float(timeout_raw)
    if not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= MAX_WAIT_SECONDS:
        raise ValueError(f"wait_timeout_seconds must be between 0 and {MAX_WAIT_SECONDS:g}")

    priority = parse_priority(raw.get("priority"))
    targets = parse_targets(raw.get("partial_execution_targets"), allow_empty=False)

    allow_risky_nodes = raw.get("allow_risky_nodes", False)
    if not isinstance(allow_risky_nodes, bool):
        raise TypeError("allow_risky_nodes must be a boolean")

    return_images = raw.get("return_images", False)
    if not isinstance(return_images, bool):
        raise TypeError("return_images must be a boolean")

    max_images = raw.get("max_images", 1)
    if isinstance(max_images, bool) or not isinstance(max_images, int):
        raise TypeError("max_images must be an integer")
    if not 1 <= max_images <= MAX_RETURNED_IMAGES:
        raise ValueError(f"max_images must be between 1 and {MAX_RETURNED_IMAGES}")

    return ExecutionOptions(
        idempotency_key=idempotency_key,
        wait=wait,
        timeout_seconds=timeout_seconds,
        priority=priority,
        targets=targets,
        allow_risky_nodes=allow_risky_nodes,
        return_images=return_images,
        max_images=max_images,
    )
