"""Optional OpenTelemetry tracing for MCP tool calls.

The SDK is an optional extra: without ``COMFYUI_MCP_OTEL_ENDPOINT`` the tracer
is a zero-cost null implementation and no OpenTelemetry package is imported.
With the endpoint set but the packages missing, construction fails loudly so a
deployer who asked for telemetry never silently loses spans.
"""

from __future__ import annotations

import copy
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

OTEL_ENDPOINT_ENV = "COMFYUI_MCP_OTEL_ENDPOINT"
OTEL_SERVICE_NAME_ENV = "COMFYUI_MCP_OTEL_SERVICE_NAME"

# Structured log fields allowed on every output path (console and OTLP). Any
# other record attribute (tokens, credentials, request internals) is stripped
# before export so the telemetry path can never leak sensitive extras.
CONTEXT_FIELDS = (
    "request_id",
    "method",
    "path",
    "status",
    "duration_ms",
    "client_id",
    "server_id",
    "workflow_id",
    "prompt_id",
    "error_code",
)


class Span(Protocol):
    def set_attributes(self, attributes: dict[str, Any]) -> None: ...


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


class _OtelSpan:
    """Adapter exposing our Span protocol over a native OpenTelemetry span.

    Exception recording is delegated to the SDK: ``start_as_current_span``
    records an ``exception`` event automatically when an exception escapes the
    span block, so the wrapper never records manually and never duplicates.
    """

    def __init__(self, span: Any) -> None:
        self._span = span

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        self._span.set_attributes(attributes)


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
            yield _OtelSpan(span)


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


_tracer_cache: dict[tuple[str, str], Tracer] = {}
_meter_cache: dict[tuple[str, str], Meter] = {}


def tracer_from_env() -> Tracer:
    """Build the configured tracer once per endpoint/service, else a null tracer.

    The underlying provider, span processor, and exporter are constructed
    exactly once per (endpoint, service name) and reused afterwards so repeated
    server construction never leaks background exporter resources.
    """
    endpoint = os.environ.get(OTEL_ENDPOINT_ENV, "").strip()
    if not endpoint:
        return NullTracer()
    service_name = os.environ.get(OTEL_SERVICE_NAME_ENV, "").strip() or "comfyui-mcp"
    key = (endpoint, service_name)
    if key not in _tracer_cache:
        _tracer_cache[key] = _otel_tracer(
            _signal_endpoint(endpoint, "traces"), service_name
        )
    return _tracer_cache[key]


def meter_from_env() -> Meter:
    """Build the configured meter once per endpoint/service, else a null meter.

    Mirrors :func:`tracer_from_env` so a metric reader and exporter are never
    constructed twice for the same collector configuration.
    """
    endpoint = os.environ.get(OTEL_ENDPOINT_ENV, "").strip()
    if not endpoint:
        return NullMeter()
    service_name = os.environ.get(OTEL_SERVICE_NAME_ENV, "").strip() or "comfyui-mcp"
    key = (endpoint, service_name)
    if key not in _meter_cache:
        _meter_cache[key] = _otel_meter(
            _signal_endpoint(endpoint, "metrics"), service_name
        )
    return _meter_cache[key]


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


def _otel_filter(record: logging.LogRecord) -> bool:
    # Never feed OpenTelemetry SDK/exporter diagnostics back into the same
    # OTLP handler: a failing collector would otherwise create a self-feeding
    # export loop.
    return not str(record.name).startswith("opentelemetry")


_log_handler_cache: dict[tuple[str, str], logging.Handler] = {}


def logging_handler_from_env() -> logging.Handler | None:
    """Build the configured OTLP log handler once per endpoint/service.

    Returns ``None`` when no endpoint is configured so a deployment without
    telemetry never imports OpenTelemetry packages. Mirrors
    :func:`tracer_from_env`/:func:`meter_from_env` caching so repeated server
    construction never leaks background exporter resources.
    """
    endpoint = os.environ.get(OTEL_ENDPOINT_ENV, "").strip()
    if not endpoint:
        return None
    service_name = os.environ.get(OTEL_SERVICE_NAME_ENV, "").strip() or "comfyui-mcp"
    key = (endpoint, service_name)
    if key not in _log_handler_cache:
        _log_handler_cache[key] = _otel_log_handler(
            _signal_endpoint(endpoint, "logs"), service_name
        )
    return _log_handler_cache[key]


def _otel_log_handler(endpoint: str, service_name: str) -> logging.Handler:
    try:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
    except ImportError as exc:
        raise RuntimeError(
            f"{OTEL_ENDPOINT_ENV} is set but the OpenTelemetry packages are not "
            "installed; install the optional 'otel' extra "
            "(pip install 'comfyui-mcp-skills[otel]')"
        ) from exc
    provider = LoggerProvider(
        resource=Resource.create({"service.name": service_name})
    )
    provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint))
    )
    # The handler carries its own provider explicitly; no global
    # set_logger_provider call, so per-config providers never clash with the
    # single-set global provider constraint.
    inner = LoggingHandler(level=logging.NOTSET, logger_provider=provider)
    inner.addFilter(_otel_filter)
    return _ProjectingLogHandler(inner)


# Standard LogRecord attributes that always survive the projection; everything
# else outside CONTEXT_FIELDS is a caller extra and gets stripped.
_STANDARD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
    }
)


class _ProjectingLogHandler(logging.Handler):
    """Project records onto the shared field allowlist before the OTLP bridge.

    The OpenTelemetry LoggingHandler converts record extras into exported
    attributes, so anything outside CONTEXT_FIELDS (tokens, credentials,
    request internals) must be stripped here — the console JsonFormatter
    allowlist alone cannot protect the telemetry path. Standard LogRecord
    metadata (timestamps, funcName, stack_info, thread/process) is preserved
    by copying the record and deleting only the non-standard extras.
    """

    def __init__(self, inner: logging.Handler) -> None:
        super().__init__(level=logging.NOTSET)
        self._inner = inner

    def emit(self, record: logging.LogRecord) -> None:
        safe = copy.copy(record)
        for key in list(safe.__dict__):
            if key in _STANDARD_ATTRS or key in CONTEXT_FIELDS:
                continue
            delattr(safe, key)
        self._inner.handle(safe)

    def filter(self, record: logging.LogRecord) -> bool:
        return self._inner.filter(record)


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
    "logging_handler_from_env",
    "meter_from_env",
    "tracer_from_env",
]
