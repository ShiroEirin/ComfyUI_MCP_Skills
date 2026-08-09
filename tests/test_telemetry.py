"""Optional OpenTelemetry tracing contracts."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from comfyui_mcp_skills.application.telemetry import (
    OTEL_ENDPOINT_ENV,
    NullMeter,
    NullTracer,
    OtelMeter,
    OtelTracer,
    meter_from_env,
    tracer_from_env,
)


class RecordingTracer:
    """Test double that records span starts without any SDK."""

    def __init__(self) -> None:
        self.spans: list[dict[str, Any]] = []

    @classmethod
    def _make_span(
        cls,
        records: list[dict[str, Any]],
        name: str,
        attributes: dict[str, Any] | None,
    ):
        span: dict[str, Any] = {"name": name, "attributes": dict(attributes or {})}

        class _Span:
            def set_attributes(self, values: dict[str, Any]) -> None:
                span["attributes"].update(values)

        records.append(span)
        return _Span()

    def span(self, name: str, attributes: dict[str, Any] | None = None):
        from contextlib import contextmanager

        @contextmanager
        def _wrap():
            yield self._make_span(self.spans, name, attributes)

        return _wrap()


def test_null_tracer_is_side_effect_free() -> None:
    tracer = NullTracer()
    with tracer.span("tool.call", {"tool": "x"}) as span:
        span.set_attributes({"duration_ms": 1.0})
    assert True


def test_tracer_from_env_returns_null_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OTEL_ENDPOINT_ENV, raising=False)
    assert isinstance(tracer_from_env(), NullTracer)


def test_tracer_from_env_fails_loudly_without_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured endpoint with the SDK unavailable must fail loudly."""
    import builtins

    real_import = builtins.__import__

    def blocked_otel(name: str, *args: object, **kwargs: object) -> object:
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ImportError("simulated missing otel extra")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_otel)
    monkeypatch.setenv(OTEL_ENDPOINT_ENV, "http://127.0.0.1:4317")
    with pytest.raises(RuntimeError, match="not installed"):
        tracer_from_env()
    with pytest.raises(RuntimeError, match="not installed"):
        meter_from_env()


def test_otel_tracer_records_span_with_sdk() -> None:
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = sdk.TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = OtelTracer(provider.get_tracer("test"))
    with tracer.span("tool.call", {"tool": "ping"}) as span:
        span.set_attributes({"duration_ms": 12.5})
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "tool.call"
    assert spans[0].attributes["tool"] == "ping"
    assert spans[0].attributes["duration_ms"] == 12.5


def test_recording_tracer_captures_server_tool_calls(tmp_path: Path) -> None:
    """The MCP server wraps tool dispatch in a span end to end."""
    import json

    from mcp.client import Client

    from comfyui_mcp_skills.adapters.mcp.server import create_server

    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "id": "local",
                        "url": "http://127.0.0.1:1",
                        "auth": {"type": "none"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    recorder = RecordingTracer()
    server = create_server(tmp_path, tracer=recorder)  # type: ignore[arg-type]

    async def exercise() -> None:
        async with Client(server) as client:
            await client.call_tool("comfyui.capability.search", {"query": "queue"})

    import anyio

    anyio.run(exercise)

    assert recorder.spans
    call = recorder.spans[0]
    assert call["name"] == "tool.call"
    assert call["attributes"]["tool"] == "comfyui.capability.search"
    assert call["attributes"]["owner"] == "local-stdio"
    assert "duration_ms" in call["attributes"]
    assert "error" not in call["attributes"]


def test_recording_tracer_captures_errors(tmp_path: Path) -> None:
    import anyio
    from mcp.client import Client

    from comfyui_mcp_skills.adapters.mcp.server import create_server

    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    recorder = RecordingTracer()
    server = create_server(tmp_path, tracer=recorder)  # type: ignore[arg-type]

    async def exercise() -> None:
        async with Client(server) as client:
            await client.call_tool("comfyui.unknown.tool", {})

    with pytest.raises(Exception):
        anyio.run(exercise)

    assert recorder.spans
    call = recorder.spans[0]
    assert call["attributes"]["tool"] == "comfyui.unknown.tool"
    assert call["attributes"].get("is_error") is True
    assert "duration_ms" in call["attributes"]


def test_otel_exporters_receive_signal_specific_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Traces and metrics must never share one OTLP path; base URL is expanded."""
    pytest.importorskip("opentelemetry.sdk.trace")
    from unittest.mock import patch

    from comfyui_mcp_skills.application import telemetry

    monkeypatch.setenv(OTEL_ENDPOINT_ENV, "http://127.0.0.1:4318")
    telemetry._tracer_cache.clear()
    telemetry._meter_cache.clear()
    with (
        patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"
        ) as span_exporter,
        patch(
            "opentelemetry.exporter.otlp.proto.http.metric_exporter.OTLPMetricExporter"
        ) as metric_exporter,
    ):
        tracer_from_env()
        meter_from_env()
    assert span_exporter.call_args.kwargs["endpoint"] == (
        "http://127.0.0.1:4318/v1/traces"
    )
    assert metric_exporter.call_args.kwargs["endpoint"] == (
        "http://127.0.0.1:4318/v1/metrics"
    )


def test_otel_exporters_strip_legacy_full_path_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy full /v1/traces value is normalized per signal, never shared."""
    pytest.importorskip("opentelemetry.sdk.trace")
    from unittest.mock import patch

    from comfyui_mcp_skills.application import telemetry

    monkeypatch.setenv(OTEL_ENDPOINT_ENV, "http://127.0.0.1:4318/v1/traces")
    telemetry._tracer_cache.clear()
    telemetry._meter_cache.clear()
    with (
        patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"
        ) as span_exporter,
        patch(
            "opentelemetry.exporter.otlp.proto.http.metric_exporter.OTLPMetricExporter"
        ) as metric_exporter,
    ):
        tracer_from_env()
        meter_from_env()
    assert span_exporter.call_args.kwargs["endpoint"] == (
        "http://127.0.0.1:4318/v1/traces"
    )
    assert metric_exporter.call_args.kwargs["endpoint"] == (
        "http://127.0.0.1:4318/v1/metrics"
    )


def test_factories_cache_single_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated factory calls construct exporters once and reuse the instance."""
    pytest.importorskip("opentelemetry.sdk.trace")
    from unittest.mock import patch

    from comfyui_mcp_skills.application import telemetry

    monkeypatch.setenv(OTEL_ENDPOINT_ENV, "http://127.0.0.1:4319")
    telemetry._tracer_cache.clear()
    telemetry._meter_cache.clear()
    with (
        patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"
        ) as span_exporter,
        patch(
            "opentelemetry.exporter.otlp.proto.http.metric_exporter.OTLPMetricExporter"
        ) as metric_exporter,
    ):
        first_tracer = tracer_from_env()
        second_tracer = tracer_from_env()
        first_meter = meter_from_env()
        second_meter = meter_from_env()
    assert span_exporter.call_count == 1
    assert metric_exporter.call_count == 1
    assert first_tracer is second_tracer
    assert first_meter is second_meter


def test_signal_endpoint_appends_paths_and_strips_legacy_suffix() -> None:
    from comfyui_mcp_skills.application.telemetry import _signal_endpoint

    assert _signal_endpoint("http://127.0.0.1:4318", "traces") == (
        "http://127.0.0.1:4318/v1/traces"
    )
    assert _signal_endpoint("http://127.0.0.1:4318", "metrics") == (
        "http://127.0.0.1:4318/v1/metrics"
    )
    assert _signal_endpoint("http://127.0.0.1:4318/", "traces") == (
        "http://127.0.0.1:4318/v1/traces"
    )
    assert _signal_endpoint("http://127.0.0.1:4318/v1/traces", "metrics") == (
        "http://127.0.0.1:4318/v1/metrics"
    )
    assert _signal_endpoint("http://127.0.0.1:4318/v1/metrics", "traces") == (
        "http://127.0.0.1:4318/v1/traces"
    )
    assert _signal_endpoint("http://127.0.0.1:4318/v1/traces/", "traces") == (
        "http://127.0.0.1:4318/v1/traces"
    )


def test_server_otel_tracer_survives_tool_errors(tmp_path: Path) -> None:
    """A raising tool with a real SDK tracer records the exception, no masking."""
    pytest.importorskip("opentelemetry.sdk.trace")
    import anyio
    from mcp.client import Client
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from comfyui_mcp_skills.adapters.mcp.server import create_server
    from comfyui_mcp_skills.application.telemetry import OtelTracer

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    server = create_server(tmp_path, tracer=OtelTracer(provider.get_tracer("test")))

    async def exercise() -> None:
        async with Client(server) as client:
            await client.call_tool("comfyui.unknown.tool", {})

    with pytest.raises(Exception):
        anyio.run(exercise)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "tool.call"
    assert spans[0].attributes["tool"] == "comfyui.unknown.tool"
    assert spans[0].attributes.get("is_error") is True
    exceptions = [event for event in spans[0].events if event.name == "exception"]
    assert len(exceptions) == 1


class RecordingMeter:
    def __init__(self) -> None:
        self.counters: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        self.histograms: dict[str, list[tuple[float, dict[str, Any]]]] = {}

    def counter(self, name: str, *, unit: str = "", description: str = ""):
        del unit, description
        records = self.counters.setdefault(name, [])

        class _Counter:
            def add(self, amount: int | float, attributes: dict[str, Any] | None = None) -> None:
                records.append((amount, dict(attributes or {})))

        return _Counter()

    def histogram(self, name: str, *, unit: str = "", description: str = ""):
        del unit, description
        records = self.histograms.setdefault(name, [])

        class _Histogram:
            def record(
                self, amount: int | float, attributes: dict[str, Any] | None = None
            ) -> None:
                records.append((float(amount), dict(attributes or {})))

        return _Histogram()


def test_null_meter_is_side_effect_free() -> None:
    meter = NullMeter()
    counter = meter.counter("mcp.tool.calls")
    counter.add(1, {"tool": "x"})
    histogram = meter.histogram("mcp.tool.duration")
    histogram.record(0.5, {"tool": "x"})
    assert True


def test_meter_from_env_returns_null_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(OTEL_ENDPOINT_ENV, raising=False)
    assert isinstance(meter_from_env(), NullMeter)


def test_otel_meter_records_counter_and_histogram_with_sdk() -> None:
    pytest.importorskip("opentelemetry.sdk.metrics")
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = OtelMeter(provider.get_meter("test"))
    counter = meter.counter("mcp.tool.calls", unit="{call}")
    counter.add(2, {"tool": "ping"})
    histogram = meter.histogram("mcp.tool.duration", unit="s")
    histogram.record(0.25, {"tool": "ping"})

    data = reader.get_metrics_data()
    names = {
        metric.name
        for resource_metrics in data.resource_metrics
        for metric in resource_metrics.scope_metrics[0].metrics
    }
    assert names == {"mcp.tool.calls", "mcp.tool.duration"}


def test_recording_meter_captures_server_tool_calls(tmp_path: Path) -> None:
    """The MCP server records invocation counters and duration histograms."""
    import json

    import anyio
    from mcp.client import Client

    from comfyui_mcp_skills.adapters.mcp.server import create_server

    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "id": "local",
                        "url": "http://127.0.0.1:1",
                        "auth": {"type": "none"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    recorder = RecordingMeter()
    server = create_server(tmp_path, meter=recorder)  # type: ignore[arg-type]

    async def exercise() -> None:
        async with Client(server) as client:
            await client.call_tool("comfyui.capability.search", {"query": "queue"})
            await client.call_tool("comfyui.capability.search", {"query": "job"})

    anyio.run(exercise)

    calls = recorder.counters["mcp.tool.calls"]
    assert len(calls) == 2
    assert all(attributes["tool"] == "comfyui.capability.search" for _, attributes in calls)
    assert all(attributes["owner"] == "local-stdio" for _, attributes in calls)
    durations = recorder.histograms["mcp.tool.duration"]
    assert len(durations) == 2
    assert all(duration >= 0.0 for duration, _ in durations)
    assert recorder.counters["mcp.tool.errors"] == []


def test_recording_meter_captures_errors(tmp_path: Path) -> None:
    import anyio
    from mcp.client import Client

    from comfyui_mcp_skills.adapters.mcp.server import create_server

    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    recorder = RecordingMeter()
    server = create_server(tmp_path, meter=recorder)  # type: ignore[arg-type]

    async def exercise() -> None:
        async with Client(server) as client:
            await client.call_tool("comfyui.unknown.tool", {})

    with pytest.raises(Exception):
        anyio.run(exercise)

    errors = recorder.counters["mcp.tool.errors"]
    assert len(errors) == 1
    assert errors[0][0] == 1
    assert errors[0][1]["tool"] == "comfyui.unknown.tool"
    assert len(recorder.counters["mcp.tool.calls"]) == 1


def test_recording_meter_counts_is_error_results(tmp_path: Path) -> None:
    """Tool failures converted to is_error results still count as errors."""
    import anyio
    from mcp.client import Client

    from comfyui_mcp_skills.adapters.mcp.server import create_server

    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    recorder = RecordingMeter()
    server = create_server(tmp_path, meter=recorder)  # type: ignore[arg-type]

    async def exercise() -> None:
        async with Client(server) as client:
            # job.get without required arguments converts to an is_error result
            await client.call_tool("comfyui.job.get", {})

    anyio.run(exercise)

    errors = recorder.counters["mcp.tool.errors"]
    assert len(errors) == 1
    assert errors[0][1]["tool"] == "comfyui.job.get"
    assert len(recorder.counters["mcp.tool.calls"]) == 1
    assert len(recorder.histograms["mcp.tool.duration"]) == 1


def test_recording_tracer_marks_is_error_results(tmp_path: Path) -> None:
    """Span is_error is set for converted is_error results, not only raised errors."""
    import anyio
    from mcp.client import Client

    from comfyui_mcp_skills.adapters.mcp.server import create_server

    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    recorder = RecordingTracer()
    server = create_server(tmp_path, tracer=recorder)  # type: ignore[arg-type]

    async def exercise() -> None:
        async with Client(server) as client:
            await client.call_tool("comfyui.job.get", {})

    anyio.run(exercise)

    assert recorder.spans
    call = recorder.spans[0]
    assert call["attributes"]["tool"] == "comfyui.job.get"
    assert call["attributes"].get("is_error") is True
    assert "duration_ms" in call["attributes"]


def test_admin_recording_meter_counts_is_error_results(tmp_path: Path) -> None:
    """Admin tool failures converted to is_error results count as errors too."""
    import json

    import anyio
    from mcp.client import Client

    from comfyui_mcp_skills.adapters.mcp.admin import create_admin_server

    (tmp_path / "data" / "local" / "txt2img").mkdir(parents=True)
    (tmp_path / "data" / "local" / "txt2img" / "schema.json").write_text(
        json.dumps({"enabled": True, "parameters": {}}), encoding="utf-8"
    )
    (tmp_path / "data" / "local" / "txt2img" / "workflow.json").write_text(
        "{}", encoding="utf-8"
    )
    recorder = RecordingMeter()
    server = create_admin_server(
        tmp_path, enabled=True, meter=recorder  # type: ignore[arg-type]
    )

    async def exercise() -> None:
        async with Client(server) as client:
            await client.call_tool(
                "comfyui.admin.workflow.set_enabled",
                {"server_id": "local", "workflow_id": "txt2img", "enabled": "false"},
            )

    anyio.run(exercise)

    errors = recorder.counters["mcp.tool.errors"]
    assert len(errors) == 1
    assert errors[0][1]["tool"] == "comfyui.admin.workflow.set_enabled"
    assert len(recorder.counters["mcp.tool.calls"]) == 1


# ---------------------------------------------------------------------------
# OTel logs: logging handler from environment
# ---------------------------------------------------------------------------


class _FakeLogExporter:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _FakeBatchProcessor:
    def __init__(self, exporter: Any) -> None:
        self.exporter = exporter


class _FakeLoggerProvider:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.processor: Any = None

    def add_log_record_processor(self, processor: Any) -> None:
        self.processor = processor


class _FakeLoggingHandler(logging.Handler):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(level=logging.NOTSET)
        self.kwargs = kwargs
        self._filters: list[Any] = []

    def addFilter(self, f: Any) -> None:
        self._filters.append(f)

    def filter(self, record: logging.LogRecord) -> bool:
        for entry in self._filters:
            if callable(entry):
                if not entry(record):
                    return False
            elif not entry.filter(record):
                return False
        return True


class _FakeResource:
    @staticmethod
    def create(attributes: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return attributes


def _install_fake_otel(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import sys

    fake_modules = {
        "opentelemetry": type(sys)("opentelemetry"),
        "opentelemetry.exporter": type(sys)("opentelemetry.exporter"),
        "opentelemetry.exporter.otlp": type(sys)("opentelemetry.exporter.otlp"),
        "opentelemetry.exporter.otlp.proto": type(sys)("opentelemetry.exporter.otlp.proto"),
        "opentelemetry.exporter.otlp.proto.http": type(sys)(
            "opentelemetry.exporter.otlp.proto.http"
        ),
        "opentelemetry.exporter.otlp.proto.http._log_exporter": type(sys)(
            "opentelemetry.exporter.otlp.proto.http._log_exporter"
        ),
        "opentelemetry.sdk": type(sys)("opentelemetry.sdk"),
        "opentelemetry.sdk._logs": type(sys)("opentelemetry.sdk._logs"),
        "opentelemetry.sdk._logs.export": type(sys)("opentelemetry.sdk._logs.export"),
        "opentelemetry.sdk.resources": type(sys)("opentelemetry.sdk.resources"),
    }
    fake_modules["opentelemetry.sdk._logs"].LoggerProvider = _FakeLoggerProvider
    fake_modules["opentelemetry.sdk._logs"].LoggingHandler = _FakeLoggingHandler
    fake_modules["opentelemetry.sdk._logs.export"].BatchLogRecordProcessor = _FakeBatchProcessor
    fake_modules["opentelemetry.exporter.otlp.proto.http._log_exporter"].OTLPLogExporter = (
        _FakeLogExporter
    )
    fake_modules["opentelemetry.sdk.resources"].Resource = _FakeResource
    for name, module in fake_modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return fake_modules


def test_logging_handler_from_env_returns_none_without_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_mcp_skills.application import telemetry

    monkeypatch.delenv(telemetry.OTEL_ENDPOINT_ENV, raising=False)
    assert telemetry.logging_handler_from_env() is None


def test_logging_handler_from_env_builds_handler_with_logs_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_mcp_skills.application import telemetry

    _install_fake_otel(monkeypatch)
    monkeypatch.setenv(telemetry.OTEL_ENDPOINT_ENV, "http://127.0.0.1:4318")
    key = ("http://127.0.0.1:4318", "comfyui-mcp")
    telemetry._log_handler_cache.pop(key, None)

    handler = telemetry.logging_handler_from_env()

    assert isinstance(handler, telemetry._ProjectingLogHandler)
    provider = handler._inner.kwargs["logger_provider"]
    assert isinstance(provider, _FakeLoggerProvider)
    exporter = provider.processor.exporter
    assert exporter.kwargs["endpoint"] == "http://127.0.0.1:4318/v1/logs"


def test_logging_handler_from_env_caches_per_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_mcp_skills.application import telemetry

    _install_fake_otel(monkeypatch)
    monkeypatch.setenv(telemetry.OTEL_ENDPOINT_ENV, "http://127.0.0.1:4318")
    telemetry._log_handler_cache.pop(("http://127.0.0.1:4318", "comfyui-mcp"), None)

    first = telemetry.logging_handler_from_env()
    second = telemetry.logging_handler_from_env()
    assert first is second

    monkeypatch.setenv(telemetry.OTEL_ENDPOINT_ENV, "http://127.0.0.1:4319")
    other = telemetry.logging_handler_from_env()
    assert other is not first


def test_logging_handler_from_env_fails_loudly_without_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    from comfyui_mcp_skills.application import telemetry

    real_import = builtins.__import__

    def blocked_otel(name: str, *args: object, **kwargs: object) -> object:
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ImportError("simulated missing otel extra")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_otel)
    monkeypatch.setenv(telemetry.OTEL_ENDPOINT_ENV, "http://127.0.0.1:4317")
    with pytest.raises(RuntimeError, match="not installed"):
        telemetry.logging_handler_from_env()


def test_logging_handler_strips_legacy_traces_path_from_logs_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_mcp_skills.application import telemetry

    _install_fake_otel(monkeypatch)
    monkeypatch.setenv(
        telemetry.OTEL_ENDPOINT_ENV, "http://127.0.0.1:4318/v1/traces"
    )
    key = ("http://127.0.0.1:4318/v1/traces", "comfyui-mcp")
    telemetry._log_handler_cache.pop(key, None)

    handler = telemetry.logging_handler_from_env()

    exporter = handler._inner.kwargs["logger_provider"].processor.exporter  # type: ignore[attr-defined]
    assert exporter.kwargs["endpoint"] == "http://127.0.0.1:4318/v1/logs"


def test_logging_handler_filter_rejects_otel_internal_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_mcp_skills.application import telemetry

    handler = _FakeLoggingHandler()
    handler.addFilter(telemetry._otel_filter)

    internal = logging.LogRecord(
        "opentelemetry.sdk._logs", logging.WARNING, "x", 1, "boom", (), None
    )
    assert handler.filter(internal) is False

    normal = logging.LogRecord(
        "comfyui_mcp_skills.adapters.mcp.server", logging.INFO, "x", 1, "ok", (), None
    )
    assert handler.filter(normal) is True


def test_configure_logging_attaches_single_otel_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_mcp_skills.application import telemetry
    from comfyui_mcp_skills.observability import configure_logging

    _install_fake_otel(monkeypatch)
    monkeypatch.setenv(telemetry.OTEL_ENDPOINT_ENV, "http://127.0.0.1:4318")
    telemetry._log_handler_cache.pop(("http://127.0.0.1:4318", "comfyui-mcp"), None)

    configure_logging("INFO")
    configure_logging("INFO")  # repeated calls keep exactly one OTel handler

    root = logging.getLogger()
    otel_handlers = [
        h for h in root.handlers if isinstance(h, telemetry._ProjectingLogHandler)
    ]
    assert len(otel_handlers) == 1
    root.handlers.clear()


def test_logging_handler_projects_allowlisted_fields_and_strips_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_mcp_skills.application import telemetry

    captured: list[logging.LogRecord] = []

    class _CaptureInner(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    _install_fake_otel(monkeypatch)
    monkeypatch.setenv(telemetry.OTEL_ENDPOINT_ENV, "http://127.0.0.1:4318")
    telemetry._log_handler_cache.pop(("http://127.0.0.1:4318", "comfyui-mcp"), None)

    handler = telemetry.logging_handler_from_env()
    handler._inner = _CaptureInner()  # type: ignore[attr-defined]

    secret = logging.LogRecord(
        "comfyui_mcp_skills.adapters.mcp.server",
        logging.INFO,
        "path/file.py",
        42,
        "ok",
        (),
        None,
    )
    secret.funcName = "handler"
    secret.request_id = "req-1"
    secret.Authorization = "Bearer secret-token"
    secret.client_id = "client-x"
    secret._secret_underscore = "hidden"
    secret.created = 1234567890.25
    handler.handle(secret)

    assert len(captured) == 1
    safe = captured[0]
    assert safe.request_id == "req-1"
    assert safe.client_id == "client-x"
    assert not hasattr(safe, "Authorization")
    assert safe.getMessage() == "ok"
    assert safe.created == 1234567890.25  # event timestamp preserved
    assert safe.funcName == "handler"
    assert safe.lineno == 42
    assert not hasattr(safe, "_secret_underscore")  # underscore extras also stripped


def test_logging_handler_cache_keys_on_service_name_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_mcp_skills.application import telemetry

    _install_fake_otel(monkeypatch)
    monkeypatch.setenv(telemetry.OTEL_ENDPOINT_ENV, "http://127.0.0.1:4318")
    monkeypatch.setenv(telemetry.OTEL_SERVICE_NAME_ENV, "svc-a")
    telemetry._log_handler_cache.pop(("http://127.0.0.1:4318", "svc-a"), None)
    telemetry._log_handler_cache.pop(("http://127.0.0.1:4318", "svc-b"), None)

    first = telemetry.logging_handler_from_env()
    monkeypatch.setenv(telemetry.OTEL_SERVICE_NAME_ENV, "svc-b")
    second = telemetry.logging_handler_from_env()

    assert second is not first
    assert (
        first._inner.kwargs["logger_provider"].kwargs["resource"]["service.name"]  # type: ignore[attr-defined]
        == "svc-a"
    )
    assert (
        second._inner.kwargs["logger_provider"].kwargs["resource"]["service.name"]  # type: ignore[attr-defined]
        == "svc-b"
    )


def test_otel_log_handler_exports_real_sdk_records_with_projection() -> None:
    """Real SDK integration: emit through the projecting handler and assert the
    exported record carries only allowlisted extras after force_flush."""
    pytest.importorskip("opentelemetry.sdk._logs")
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import (
        BatchLogRecordProcessor,
        InMemoryLogExporter,
    )
    from opentelemetry.sdk.resources import Resource

    from comfyui_mcp_skills.application import telemetry

    exporter = InMemoryLogExporter()
    provider = LoggerProvider(resource=Resource.create({"service.name": "it-test"}))
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    inner = LoggingHandler(level=logging.NOTSET, logger_provider=provider)
    inner.addFilter(telemetry._otel_filter)
    handler = telemetry._ProjectingLogHandler(inner)

    record = logging.LogRecord(
        "comfyui_mcp_skills.adapters.mcp.server",
        logging.INFO,
        "x",
        1,
        "real sdk log",
        (),
        None,
    )
    record.request_id = "req-99"
    record.Authorization = "Bearer secret"
    handler.handle(record)
    provider.force_flush()

    finished = exporter.get_finished_logs()
    assert len(finished) == 1
    body = finished[0].log_record.body
    assert "real sdk log" in str(body)
    attrs = dict(finished[0].log_record.attributes or {})
    assert attrs.get("request_id") == "req-99"
    assert "Authorization" not in attrs
