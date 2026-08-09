"""Structured logging and lightweight process-local request metrics."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from comfyui_mcp_skills.application.telemetry import (
    CONTEXT_FIELDS,
    logging_handler_from_env,
)

_CONTEXT_FIELDS = CONTEXT_FIELDS


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        for field in _CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if value not in {None, ""}:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class RequestMetrics:
    def __init__(self) -> None:
        self._lock = Lock()
        self._requests = 0
        self._errors = 0
        self._rate_limited = 0
        self._duration = 0.0

    def record(self, *, status_code: int, duration_seconds: float) -> None:
        with self._lock:
            self._requests += 1
            self._duration += max(0.0, duration_seconds)
            if status_code >= 400:
                self._errors += 1
            if status_code == 429:
                self._rate_limited += 1

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            return {
                "requests_total": self._requests,
                "request_errors_total": self._errors,
                "rate_limit_rejections_total": self._rate_limited,
                "request_duration_seconds_total": round(self._duration, 6),
            }


REQUEST_METRICS = RequestMetrics()


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    otel_handler = logging_handler_from_env()
    if otel_handler is not None:
        root.addHandler(otel_handler)
    root.setLevel(level.upper())
