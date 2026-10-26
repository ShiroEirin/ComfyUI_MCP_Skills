"""The userdata reader must not leak decoder-only kwargs to the transport.

``_get_json_bounded`` forwards unknown keyword arguments straight to
``requests.Session.get``; passing ``description=`` there raised a ``TypeError``
that the ``RequestException`` handler could not catch, so every real
``workflow.import(source="server_userdata")`` read failed at the HTTP layer.
"""

from __future__ import annotations

from typing import Any

import pytest

from comfyui_mcp_skills.infrastructure.comfyui.userdata_client import UserdataClient


def test_read_userdata_workflow_does_not_forward_decoder_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object.__new__(UserdataClient)
    seen: dict[str, Any] = {}
    payload = {"1": {"class_type": "KSampler", "inputs": {}}}

    def fake_bounded(path: str, *, max_bytes: int, **kwargs: Any) -> object:
        seen["path"] = path
        seen["max_bytes"] = max_bytes
        seen["kwargs"] = kwargs
        return payload

    monkeypatch.setattr(client, "_get_json_bounded", fake_bounded)

    assert client.read_userdata_workflow("workflows/nooba/demo.json") == payload
    assert seen["path"] == "/userdata/workflows%2Fnooba%2Fdemo.json"
    assert seen["max_bytes"] == 2 * 1024 * 1024
    assert seen["kwargs"] == {}
