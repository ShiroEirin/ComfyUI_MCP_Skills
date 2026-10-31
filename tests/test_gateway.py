"""ComfyUI gateway error classification contracts."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from comfyui_mcp_skills.domain.errors import ExecutionFailed, ServerOffline, UploadFailed
from comfyui_mcp_skills.infrastructure.comfyui.gateway import ComfyUIGatewayAdapter


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(response=response)


def test_gateway_does_not_expose_undeclared_client_methods() -> None:
    gateway = ComfyUIGatewayAdapter({"url": "http://127.0.0.1:8188"})
    assert not hasattr(gateway, "get_extensions")


def test_gateway_reports_output_to_input_copy_as_unsupported() -> None:
    gateway = ComfyUIGatewayAdapter({"url": "http://127.0.0.1:8188"})
    gateway._client = MagicMock()
    gateway._client.probe_capabilities.return_value = {"jobs_api": {"state": "supported"}}

    result = gateway.get_capabilities()

    assert result["state"] == "supported"
    assert result["data"]["output_to_input_copy"] == {"state": "unsupported"}
    assert gateway.supports_output_to_input_copy() is False
    assert not hasattr(gateway, "copy_output_to_input")


@pytest.mark.parametrize(
    "failure",
    [
        requests.ConnectionError("POST http://secret.internal/private failed"),
        ValueError("invalid response from https://secret.internal/private"),
        OSError("C:\\private\\staged.png is unavailable"),
    ],
)
def test_upload_errors_do_not_expose_upstream_urls_or_paths(failure: Exception) -> None:
    gateway = ComfyUIGatewayAdapter({"url": "http://127.0.0.1:8188"})
    gateway._client = MagicMock()
    gateway._client.upload_file.side_effect = failure

    with pytest.raises(UploadFailed) as raised:
        gateway.upload_file("C:\\private\\staged.png", purpose="image", original_ref="")

    assert str(raised.value) == "ComfyUI upload failed"
    assert "secret.internal" not in str(raised.value)
    assert "private" not in str(raised.value)


@pytest.mark.parametrize("status", [400, 422])
def test_queue_prompt_maps_4xx_to_execution_failed(status: int) -> None:
    gateway = ComfyUIGatewayAdapter({"url": "http://127.0.0.1:8188"})
    gateway._client = MagicMock()
    gateway._client.queue_prompt.side_effect = _http_error(status)

    with pytest.raises(ExecutionFailed):
        gateway.queue_prompt({})


@pytest.mark.parametrize("failure", [_http_error(500), _http_error(503), requests.Timeout()])
def test_queue_prompt_maps_unknown_outcome_to_server_offline(
    failure: Exception,
) -> None:
    gateway = ComfyUIGatewayAdapter({"url": "http://127.0.0.1:8188"})
    gateway._client = MagicMock()
    gateway._client.queue_prompt.side_effect = failure

    with pytest.raises(ServerOffline):
        gateway.queue_prompt({})


def test_download_output_to_forwards_bounded_streaming_contract(tmp_path: Path) -> None:
    gateway = ComfyUIGatewayAdapter({"url": "http://127.0.0.1:8188"})
    gateway._client = MagicMock()
    receipt = {"size_bytes": 3, "sha256": "a" * 64}
    gateway._client.download_output_to.return_value = receipt
    destination = tmp_path / "artifact.bin"

    result = gateway.download_output_to(
        "artifact.bin",
        destination,
        subfolder="renders",
        storage_type="output",
        max_bytes=1024,
    )

    assert result == receipt
    gateway._client.download_output_to.assert_called_once_with(
        "artifact.bin",
        destination,
        "renders",
        "output",
        max_bytes=1024,
    )


def _http_error_with_payload(status: int, payload: object) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    response._content = (
        payload.encode("utf-8")
        if isinstance(payload, str)
        else json.dumps(payload).encode("utf-8")
    )
    return requests.HTTPError(response=response)


def test_queue_prompt_surfaces_upstream_node_errors() -> None:
    gateway = ComfyUIGatewayAdapter({"url": "http://127.0.0.1:8188"})
    gateway._client = MagicMock()
    gateway._client.queue_prompt.side_effect = _http_error_with_payload(
        400,
        {
            "error": {
                "type": "prompt_outputs_failed_validation",
                "message": "Prompt outputs failed validation",
            },
            "node_errors": {
                "60": {
                    "errors": [
                        {
                            "type": "required_input_missing",
                            "message": "Required input is missing",
                            "details": "image_encoding",
                        }
                    ],
                    "class_type": "RTXVideoSuperResolution",
                }
            },
        },
    )

    with pytest.raises(ExecutionFailed) as raised:
        gateway.queue_prompt({})

    details = raised.value.details
    assert details["http_status"] == 400
    assert details["rejected_nodes"] == ["60"]
    assert details["node_errors"]["60"][0]["details"] == "image_encoding"
    assert "image_encoding" in str(raised.value)
    assert "node 60" in str(raised.value)


def test_queue_prompt_bounds_upstream_rejection_payload() -> None:
    node_errors = {
        str(node): {"errors": [{"type": "t", "message": "m", "details": "d"}] * 12}
        for node in range(40)
    }
    gateway = ComfyUIGatewayAdapter({"url": "http://127.0.0.1:8188"})
    gateway._client = MagicMock()
    gateway._client.queue_prompt.side_effect = _http_error_with_payload(
        400, {"node_errors": node_errors}
    )

    with pytest.raises(ExecutionFailed) as raised:
        gateway.queue_prompt({})

    details = raised.value.details
    assert len(details["rejected_nodes"]) == 32
    assert all(len(items) == 8 for items in details["node_errors"].values())


def test_queue_prompt_surfaces_plain_text_rejection() -> None:
    gateway = ComfyUIGatewayAdapter({"url": "http://127.0.0.1:8188"})
    gateway._client = MagicMock()
    gateway._client.queue_prompt.side_effect = _http_error_with_payload(400, "bad prompt body")

    with pytest.raises(ExecutionFailed) as raised:
        gateway.queue_prompt({})

    assert raised.value.details["upstream_body"] == "bad prompt body"
    assert "bad prompt body" in str(raised.value)
