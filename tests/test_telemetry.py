"""Optional OpenTelemetry tracing contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from comfyui_mcp_skills.application.telemetry import (
    OTEL_ENDPOINT_ENV,
    NullTracer,
    OtelTracer,
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
    monkeypatch.setenv(OTEL_ENDPOINT_ENV, "http://127.0.0.1:4318/v1/traces")
    with pytest.raises(RuntimeError, match="not installed"):
        tracer_from_env()


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
