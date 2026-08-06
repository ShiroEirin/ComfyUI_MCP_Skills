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


def tracer_from_env() -> Tracer:
    """Build the configured tracer, or a null tracer when unconfigured."""
    endpoint = os.environ.get(OTEL_ENDPOINT_ENV, "").strip()
    if not endpoint:
        return NullTracer()
    service_name = os.environ.get(OTEL_SERVICE_NAME_ENV, "").strip() or "comfyui-mcp"
    return _otel_tracer(endpoint, service_name)


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


__all__ = ["NullTracer", "OtelTracer", "Span", "Tracer", "tracer_from_env"]
