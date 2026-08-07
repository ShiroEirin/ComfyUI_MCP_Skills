"""Phase H capability-aware observability service and client contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from comfyui_mcp_skills.application.observability import ObservationService, normalize_observation
from comfyui_mcp_skills.application.servers import ServerRegistry
from comfyui_mcp_skills.domain.errors import ExecutionFailed, WorkflowArgumentsError
from comfyui_mcp_skills.domain.media import validate_media_locator
from comfyui_mcp_skills.domain.models import Asset
from comfyui_mcp_skills.infrastructure.comfyui.client import ComfyUIClient
from comfyui_mcp_skills.infrastructure.comfyui.gateway import ComfyUIGatewayAdapter


class _Response:
    def __init__(
        self,
        status_code: int,
        payload: Any = None,
        *,
        content_length: int | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.closed = False
        self._content = json.dumps(payload).encode("utf-8")
        length = len(self._content) if content_length is None else content_length
        self.headers = {"Content-Length": str(length)}

    def json(self) -> Any:
        return self._payload

    def iter_content(self, chunk_size: int) -> Any:
        for offset in range(0, len(self._content), chunk_size):
            yield self._content[offset : offset + chunk_size]

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            raise requests.HTTPError(response=response)

    def close(self) -> None:
        self.closed = True


class _Gateway:
    def __init__(self) -> None:
        self.free_requests: list[tuple[bool, bool]] = []

    def get_queue(self, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        del timeout_seconds
        return {
            "queue_running": [
                [1, "prompt-running", {"1": {"inputs": {"text": "private prompt"}}}],
            ],
            "queue_pending": [
                [2, "prompt-pending", {"2": {"inputs": {"password": "secret"}}}],
            ],
        }

    def get_logs(self) -> dict[str, Any]:
        return {
            "state": "supported",
            "data": {
                "entries": [
                    {
                        "t": "2026-07-31T12:00:00Z",
                        "m": (
                            "Authorization: Bearer secret-token\n"
                            "loaded C:\\Users\\alice\\private\\model.safetensors"
                        ),
                    },
                    {"t": "2026-07-31T12:00:01Z", "m": "ready"},
                ],
                "password": "must-not-escape",
            },
        }

    def get_workflow_templates(self) -> dict[str, Any]:
        return {
            "state": "supported",
            "data": {
                "template-one": {
                    "name": "Safe template",
                    "description": "A template",
                    "category": "image",
                    "source": "core",
                    "path": "/home/alice/templates/private.json",
                    "workflow": {"nodes": [{"prompt": "private"}]},
                    "token": "must-not-escape",
                }
            },
        }

    def get_subgraphs(self) -> dict[str, Any]:
        return {
            "state": "supported",
            "data": {
                "subgraph-one": {
                    "name": "Reusable graph",
                    "source": "custom_node",
                    "path": "C:\\private\\graph.json",
                    "info": {"node_pack": "example-pack", "secret": "no"},
                }
            },
        }

    def get_subgraph(self, subgraph_id: str) -> dict[str, Any]:
        assert subgraph_id == "subgraph-one"
        return {
            "state": "supported",
            "data": {
                "name": "Reusable graph",
                "source": "custom_node",
                "info": {"node_pack": "example-pack"},
                "data": json.dumps({"nodes": [{}, {}], "links": [[1, 2]]}),
                "path": "/private/graph.json",
            },
        }

    def get_capabilities(self) -> dict[str, Any]:
        supported = {"state": "supported"}
        return {
            "state": "supported",
            "data": {
                "jobs_api": supported,
                "userdata_v2": {"state": "unsupported"},
                "userdata_traditional": supported,
                "userdata": {"state": "supported", "variant": "traditional"},
                "node_replacements": {"state": "unauthorized", "token": "no"},
                "manager_queue_status": {"state": "temporarily_unavailable"},
                "manager_install": supported,
                "logs": supported,
                "workflow_templates": supported,
                "subgraphs": supported,
            },
        }

    def free_memory(self, *, unload_models: bool, free_memory: bool) -> dict[str, Any]:
        self.free_requests.append((unload_models, free_memory))
        return {"success": True, "auth": "must-not-escape"}


def _service(tmp_path: Path) -> tuple[ObservationService, _Gateway]:
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "default_server": "local",
                "servers": [
                    {
                        "id": "local",
                        "name": "Local",
                        "url": "http://127.0.0.1:8188",
                        "auth": "registry-secret",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    gateway = _Gateway()
    return ObservationService(ServerRegistry(tmp_path), lambda _config: gateway), gateway


def test_queue_list_removes_raw_prompts_and_uses_an_opaque_bounded_cursor(tmp_path: Path) -> None:
    service, _gateway = _service(tmp_path)

    first = service.queue("local", limit=1)
    second = service.queue("local", limit=1, cursor=first["next_cursor"])

    assert first == {
        "server_id": "local",
        "capability_state": "supported",
        "items": [{"state": "running", "queue_number": 1, "prompt_id": "prompt-running"}],
        "next_cursor": first["next_cursor"],
        "total": 2,
    }
    assert first["next_cursor"] not in {"", "1"}
    assert second["items"] == [
        {"state": "pending", "queue_number": 2, "prompt_id": "prompt-pending"}
    ]
    assert "private prompt" not in json.dumps([first, second])
    with pytest.raises(WorkflowArgumentsError):
        service.queue("local", limit=201)
    with pytest.raises(WorkflowArgumentsError):
        service.queue("local", cursor="not-an-opaque-cursor")


def test_log_read_counts_lines_and_redacts_credentials_and_file_paths(tmp_path: Path) -> None:
    service, _gateway = _service(tmp_path)

    result = service.logs("local", limit=3)

    assert [item["message"] for item in result["items"]] == [
        "ready",
        "loaded [REDACTED_PATH]",
        "[REDACTED]",
    ]
    serialized = json.dumps(result)
    assert "secret-token" not in serialized
    assert "alice" not in serialized
    assert "must-not-escape" not in serialized
    with pytest.raises(WorkflowArgumentsError):
        service.logs("local", limit=1001)


def test_template_and_subgraph_discovery_expose_metadata_not_graphs_or_paths(
    tmp_path: Path,
) -> None:
    service, _gateway = _service(tmp_path)

    templates = service.templates("local")
    subgraphs = service.subgraphs("local")
    subgraph = service.subgraph("local", "subgraph-one")

    assert templates["items"] == [
        {
            "template_id": "template-one",
            "name": "Safe template",
            "description": "A template",
            "category": "image",
            "source": "core",
        }
    ]
    assert subgraphs["items"] == [
        {
            "subgraph_id": "subgraph-one",
            "name": "Reusable graph",
            "source": "custom_node",
            "node_pack": "example-pack",
        }
    ]
    assert subgraph["subgraph"] == {
        "subgraph_id": "subgraph-one",
        "name": "Reusable graph",
        "source": "custom_node",
        "node_pack": "example-pack",
        "node_count": 2,
        "link_count": 1,
    }
    serialized = json.dumps([templates, subgraphs, subgraph])
    assert "private" not in serialized
    assert '"nodes"' not in serialized


def test_capabilities_preserve_each_optional_state_and_remove_extra_fields(tmp_path: Path) -> None:
    service, _gateway = _service(tmp_path)

    result = service.capabilities("local")

    assert result["capabilities"]["userdata"] == {
        "state": "supported",
        "variant": "traditional",
    }
    assert result["capabilities"]["node_replacements"] == {"state": "unauthorized"}
    assert result["capabilities"]["manager_queue_status"] == {"state": "temporarily_unavailable"}
    assert "token" not in json.dumps(result)


def test_redaction_handles_serialized_secrets_prompt_graphs_and_path_forms() -> None:
    value = normalize_observation(
        {
            "message": (
                '{"Authorization":"Basic dXNlcjpwYXNz","prompt":'
                '{"one":"private","two":"still-private"}}'
            ),
            "encoded": json.dumps(
                json.dumps({"api_key": "generic-secret", "prompt": "encoded-private"})
            ),
            "long_encoded": json.dumps(json.dumps({"prompt": "LONGSECRET" + "x" * 5000})),
            "long_plain": "INFO prompt=PLAINSECRET" + "x" * 5000,
            "details": (
                "C:/Users/alice/sensitive //server/share/sensitive "
                "\\\\server\\share\\sensitive data/models/private.safetensors "
                "workspace/private/file.json"
            ),
        }
    )
    rendered = json.dumps(value)
    for secret in (
        "dXNlcjpwYXNz",
        "generic-secret",
        "encoded-private",
        "LONGSECRET",
        "PLAINSECRET",
        "private",
        "still-private",
        "alice",
        "server/share",
        "data/models",
        "workspace/private",
    ):
        assert secret not in rendered
    assert "[REDACTED_PATH]" in rendered


def test_legacy_asset_public_projection_omits_unsafe_locator() -> None:
    asset = Asset(
        asset_id="asset_legacy",
        server_id="local",
        comfyui_ref="C:/Users/alice/private.png",
        name="C:/Users/alice/private.png",
        subfolder="",
        media_type="image",
        mime_type="image/png",
        size_bytes=1,
        sha256="0" * 64,
    )
    public = asset.to_public_dict()
    assert "name" not in public
    assert "subfolder" not in public
    assert "comfyui_ref" not in public


def test_observation_cursor_is_server_and_snapshot_bound(tmp_path: Path) -> None:
    service, gateway = _service(tmp_path)
    first = service.logs("local", limit=1)
    assert first["next_cursor"]
    original = gateway.get_logs
    gateway.get_logs = lambda: {
        "state": "supported",
        "data": {"entries": [{"m": "new"}, *original()["data"]["entries"]]},
    }
    with pytest.raises(WorkflowArgumentsError, match="cursor"):
        service.logs("local", limit=1, cursor=first["next_cursor"])


def test_media_locator_rejects_absolute_traversal_and_unc_paths() -> None:
    assert validate_media_locator("result.png", "nested/output") == (
        "result.png",
        "nested/output",
    )
    for name, folder in (
        ("C:/private.png", ""),
        ("result.png", "../private"),
        ("result.png", "C:/private"),
        ("result.png", "//server/share"),
    ):
        with pytest.raises(ValueError):
            validate_media_locator(name, folder)


def test_free_requires_effect_and_forwards_both_explicit_booleans(tmp_path: Path) -> None:
    service, gateway = _service(tmp_path)

    result = service.free("local", unload_models=False, free_memory=True)

    assert gateway.free_requests == [(False, True)]
    assert result == {
        "server_id": "local",
        "success": True,
        "unload_models": False,
        "free_memory": True,
        "impact": ["runtime_memory"],
    }
    with pytest.raises(WorkflowArgumentsError):
        service.free("local", unload_models=False, free_memory=False)
    gateway.free_memory = lambda **_kwargs: {}
    with pytest.raises(ExecutionFailed, match="outcome is unknown"):
        service.free("local", unload_models=True, free_memory=False)
    gateway.free_memory = lambda **_kwargs: {"success": False}
    failed = service.free("local", unload_models=True, free_memory=True)
    assert failed["success"] is False
    assert failed["impact"] == []


def test_client_probes_optional_endpoints_independently_and_uses_userdata_fallback() -> None:
    client = ComfyUIClient("http://127.0.0.1:8188")
    responses: dict[str, _Response | Exception] = {
        "/api/jobs": _Response(404),
        "/v2/userdata": _Response(503),
        "/userdata": _Response(200, []),
        "/node_replacements": _Response(403),
        "/manager/queue/status": _Response(500),
        "/manager/queue/install": _Response(405),
        "/internal/logs/raw": _Response(200, {}),
        "/workflow_templates": _Response(404),
        "/global_subgraphs": requests.Timeout(),
    }

    def fake_get(path: str, **_kwargs: Any) -> _Response:
        response = responses[path]
        if isinstance(response, Exception):
            raise response
        return response

    client._get = fake_get  # type: ignore[method-assign]

    result = client.probe_capabilities()

    assert result["jobs_api"] == {"state": "unsupported"}
    assert result["userdata_v2"] == {"state": "temporarily_unavailable"}
    assert result["userdata_traditional"] == {"state": "supported"}
    assert result["userdata"] == {"state": "supported", "variant": "traditional"}
    assert result["node_replacements"] == {"state": "unauthorized"}
    assert result["manager_queue_status"] == {"state": "temporarily_unavailable"}
    assert result["manager_install"] == {"state": "supported"}
    assert result["workflow_templates"] == {"state": "unsupported"}
    assert result["subgraphs"] == {"state": "temporarily_unavailable"}
    assert all(
        response.closed for response in responses.values() if isinstance(response, _Response)
    )


def test_client_bounds_optional_payloads_and_disables_redirects() -> None:
    client = ComfyUIClient("http://127.0.0.1:8188")
    oversized = _Response(200, {}, content_length=4 * 1024 * 1024 + 1)
    client._get = MagicMock(return_value=oversized)  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="too large"):
        client.get_logs()
    assert oversized.closed is True
    client._get.assert_called_once_with("/internal/logs/raw", stream=True, allow_redirects=False)
    queue_client = ComfyUIClient("http://127.0.0.1:8188")
    oversized_queue = _Response(200, {}, content_length=8 * 1024 * 1024 + 1)
    queue_client._get = MagicMock(return_value=oversized_queue)  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="too large"):
        queue_client.get_queue()
    assert oversized_queue.closed is True
    queue_client._get.assert_called_once_with(
        "/queue",
        stream=True,
        allow_redirects=False,
        timeout=30.0,
    )


@pytest.mark.parametrize(
    "payload",
    [
        b'{"number":NaN}',
        b'{"number":' + b"9" * 1000 + b"}",
        b"[" * 1100 + b"0" + b"]" * 1100,
    ],
)
def test_bounded_json_rejects_nonfinite_huge_and_deep_values(payload: bytes) -> None:
    client = ComfyUIClient("http://127.0.0.1:8188")
    response = _Response(200, {})
    response._content = payload
    response.headers = {"Content-Length": str(len(payload))}
    client._get = MagicMock(return_value=response)  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="invalid"):
        client.get_logs()
    assert response.closed is True


def test_queue_decode_failures_are_typed_as_upstream_execution_errors() -> None:
    gateway = ComfyUIGatewayAdapter({"url": "http://127.0.0.1:8188"})
    gateway._client = MagicMock()
    gateway._client.get_queue.side_effect = ValueError("invalid JSON")
    with pytest.raises(ExecutionFailed, match="queue response"):
        gateway.get_queue()


def test_gateway_returns_capability_state_instead_of_global_offline_for_optional_errors() -> None:
    gateway = ComfyUIGatewayAdapter({"url": "http://127.0.0.1:8188"})
    gateway._client = MagicMock()
    response = requests.Response()
    response.status_code = 401
    gateway._client.get_logs.side_effect = requests.HTTPError(response=response)

    result = gateway.get_logs()

    assert result == {"state": "unauthorized", "data": None}
