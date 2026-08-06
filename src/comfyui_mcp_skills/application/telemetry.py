"""Optional OpenTelemetry tracing for MCP tool calls.

The SDK is an optional extra: without ``COMFYUI_MCP_OTEL_ENDPOINT`` the tracer
is a zero-cost null implementation and no OpenTelemetry package is imported.
With the endpoint set but the packages missing, construction fails loudly so a
deployer who asked for telemetry never silently loses spans.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

OTEL_ENDPOINT_ENV = "COMFYUI_MCP_OTEL_ENDPOINT"
OTEL_SERVICE_NAME_ENV = "COMFYUI_MCP_OTEL_SERVICE_NAME"


class Span(Protocol):
    def set_attributes(self, attributes: dict[str, Any]) -> None: ...

    def record_error(self, error: BaseException) -> None: ...


class Tracer(Protocol):
    @contextmanager
    def span(
        self, name: str, attributes: dict[str, Any] | None = None
    ) -> Iterator[Span]: ...


class Counter(Protocol):
    def add(self, amount: int | float, attributes: dict[str, Any] | None = None) -> None: ...


class Histogram(Protocol):
    def record(
        self, amount: int | float, attributes: dict[str, Any] | None = None
    ) -> None: ...


class Meter(Protocol):
    def counter(self, name: str, *, unit: str = "", description: str = "") -> Counter: ...

    def histogram(
        self, name: str, *, unit: str = "", description: str = ""
    ) -> Histogram: ...


class _NullSpan:
    def set_attributes(self, attributes: dict[str, Any]) -> None:
        del attributes

    def record_error(self, error: BaseException) -> None:
        del error


class NullTracer:
    """No-op tracer used when OpenTelemetry is not configured."""

    @staticmethod
    @contextmanager
    def span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Span]:
        del name, attributes
        yield _NullSpan()


class _NullCounter:
    def add(self, amount: int | float, attributes: dict[str, Any] | None = None) -> None:
        del amount, attributes


class _NullHistogram:
    def record(
        self, amount: int | float, attributes: dict[str, Any] | None = None
    ) -> None:
        del amount, attributes


class NullMeter:
    """No-op meter used when OpenTelemetry is not configured."""

    @staticmethod
    def counter(
        name: str, *, unit: str = "", description: str = ""
    ) -> Counter:
        del name, unit, description
        return _NullCounter()

    @staticmethod
    def histogram(
        name: str, *, unit: str = "", description: str = ""
    ) -> Histogram:
        del name, unit, description
        return _NullHistogram()


class OtelMeter:
    """Thin adapter over an already-configured OpenTelemetry meter."""

    def __init__(self, meter: Any) -> None:
        self._meter = meter

    def counter(self, name: str, *, unit: str = "", description: str = "") -> Counter:
        return self._meter.create_counter(name, unit=unit, description=description)

    def histogram(
        self, name: str, *, unit: str = "", description: str = ""
    ) -> Histogram:
        return self._meter.create_histogram(name, unit=unit, description=description)


class OtelTracer:
    """Thin adapter over an already-configured OpenTelemetry tracer."""

    def __init__(self, tracer: Any) -> None:
        self._tracer = tracer

    @contextmanager
    def span(
        self, name: str, attributes: dict[str, Any] | None = None
    ) -> Iterator[Span]:
        with self._tracer.start_as_current_span(name) as span:
            if attributes:
                span.set_attributes(attributes)
            yield span


def _signal_endpoint(base: str, signal: str) -> str:
    """Append the OTLP/HTTP signal path to a collector base URL.

    Accepts a base URL (``http://host:4318``) or a legacy full signal path
    (``http://host:4318/v1/traces``); a legacy path is stripped before the
    requested signal path is appended so traces and metrics never share one
    endpoint.
    """
    normalized = base.rstrip("/")
    for existing in ("/v1/traces", "/v1/metrics"):
        if normalized.endswith(existing):
            normalized = normalized[: -len(existing)].rstrip("/")
            break
    return f"{normalized}/v1/{signal}"


def tracer_from_env() -> Tracer:
    """Build the configured tracer, or a null tracer when unconfigured."""
    endpoint = os.environ.get(OTEL_ENDPOINT_ENV, "").strip()
    if not endpoint:
        return NullTracer()
    service_name = os.environ.get(OTEL_SERVICE_NAME_ENV, "").strip() or "comfyui-mcp"
    return _otel_tracer(_signal_endpoint(endpoint, "traces"), service_name)


def meter_from_env() -> Meter:
    """Build the configured meter, or a null meter when unconfigured."""
    endpoint = os.environ.get(OTEL_ENDPOINT_ENV, "").strip()
    if not endpoint:
        return NullMeter()
    service_name = os.environ.get(OTEL_SERVICE_NAME_ENV, "").strip() or "comfyui-mcp"
    return _otel_meter(_signal_endpoint(endpoint, "metrics"), service_name)


def _otel_tracer(endpoint: str, service_name: str) -> Tracer:
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        raise RuntimeError(
            f"{OTEL_ENDPOINT_ENV} is set but the OpenTelemetry packages are not "
            "installed; install the optional 'otel' extra "
            "(pip install 'comfyui-mcp-skills[otel]')"
        ) from exc
    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name})
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    return OtelTracer(trace.get_tracer("comfyui-mcp-skills"))


def _otel_meter(endpoint: str, service_name: str) -> Meter:
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.metrics import get_meter_provider, set_meter_provider
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
    except ImportError as exc:
        raise RuntimeError(
            f"{OTEL_ENDPOINT_ENV} is set but the OpenTelemetry packages are not "
            "installed; install the optional 'otel' extra "
            "(pip install 'comfyui-mcp-skills[otel]')"
        ) from exc
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=endpoint),
        export_interval_millis=30_000,
    )
    provider = MeterProvider(
        metric_readers=[reader],
        resource=Resource.create({"service.name": service_name}),
    )
    set_meter_provider(provider)
    return OtelMeter(get_meter_provider().get_meter("comfyui-mcp-skills"))


__all__ = [
    "Counter",
    "Histogram",
    "Meter",
    "NullMeter",
    "NullTracer",
    "OtelMeter",
    "OtelTracer",
    "Span",
    "Tracer",
    "meter_from_env",
    "tracer_from_env",
]
