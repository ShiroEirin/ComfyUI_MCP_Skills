"""ComfyUI gateway error classification contracts."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from comfyui_mcp_skills.domain.errors import ExecutionFailed, ServerOffline
from comfyui_mcp_skills.infrastructure.comfyui.gateway import LegacyComfyUIGateway


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(response=response)


def test_gateway_does_not_expose_undeclared_client_methods() -> None:
    gateway = LegacyComfyUIGateway({"url": "http://127.0.0.1:8188"})
    assert not hasattr(gateway, "get_system_stats")


@pytest.mark.parametrize("status", [400, 422])
def test_queue_prompt_maps_4xx_to_execution_failed(status: int) -> None:
    gateway = LegacyComfyUIGateway({"url": "http://127.0.0.1:8188"})
    gateway._client = MagicMock()
    gateway._client.queue_prompt.side_effect = _http_error(status)

    with pytest.raises(ExecutionFailed):
        gateway.queue_prompt({})


@pytest.mark.parametrize("failure", [_http_error(500), _http_error(503), requests.Timeout()])
def test_queue_prompt_maps_unknown_outcome_to_server_offline(
    failure: Exception,
) -> None:
    gateway = LegacyComfyUIGateway({"url": "http://127.0.0.1:8188"})
    gateway._client = MagicMock()
    gateway._client.queue_prompt.side_effect = failure

    with pytest.raises(ServerOffline):
        gateway.queue_prompt({})
