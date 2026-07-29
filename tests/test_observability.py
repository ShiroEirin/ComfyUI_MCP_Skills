"""Structured observability contracts."""

from __future__ import annotations

import json
import logging

from comfyui_mcp_skills.observability import JsonFormatter, RequestMetrics


def test_json_formatter_and_metrics_avoid_sensitive_payloads() -> None:
    metrics = RequestMetrics()
    metrics.record(status_code=200, duration_seconds=0.25)
    metrics.record(status_code=429, duration_seconds=0.01)

    snapshot = metrics.snapshot()
    assert snapshot == {
        "requests_total": 2,
        "request_errors_total": 1,
        "rate_limit_rejections_total": 1,
        "request_duration_seconds_total": 0.26,
    }

    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "request_complete",
        (),
        None,
    )
    record.request_id = "request-1"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "request_complete"
    assert payload["request_id"] == "request-1"
    assert "token" not in payload
