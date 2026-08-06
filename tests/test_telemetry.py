"""Optional OpenTelemetry tracing contracts."""

from __future__ import annotations

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

            def record_error(self, error: BaseException) -> None:
                span["attributes"]["error"] = type(error).__name__

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
        span.record_error(ValueError("boom"))
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
    monkeypatch.setenv(OTEL_ENDPOINT_ENV, "http://127.0.0.1:4318")
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
    assert "error" in call["attributes"]
    assert "duration_ms" in call["attributes"]


def test_otel_exporters_receive_signal_specific_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Traces and metrics must never share one OTLP path; base URL is expanded."""
    pytest.importorskip("opentelemetry.sdk.trace")
    from unittest.mock import patch

    monkeypatch.setenv(OTEL_ENDPOINT_ENV, "http://127.0.0.1:4318")
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

    monkeypatch.setenv(OTEL_ENDPOINT_ENV, "http://127.0.0.1:4318/v1/traces")
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
