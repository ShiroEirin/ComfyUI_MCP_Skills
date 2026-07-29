"""Remote transport security and upload contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from pathlib import Path
from unittest.mock import MagicMock, patch

import anyio
import pytest
from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

from comfyui_mcp_skills.adapters.http.security import SafeHTTPSDownloader
from comfyui_mcp_skills.adapters.http.server import (
    RequestControlMiddleware,
    create_http_app,
)
from comfyui_mcp_skills.http_main import _default_allowed_hosts
from comfyui_mcp_skills.domain.errors import UnsafePath


def _project(root: Path) -> None:
    (root / "data" / "servers" / "local" / "workflows").mkdir(parents=True)
    (root / "config.json").write_text(
        '{"default_server":"local","servers":[{"id":"local","name":"Local",'
        '"url":"http://127.0.0.1:8188","enabled":true}]}',
        encoding="utf-8",
    )


def test_safe_downloader_rejects_non_https_and_private_networks(tmp_path: Path) -> None:
    downloader = SafeHTTPSDownloader(max_bytes=1024)

    with pytest.raises(UnsafePath):
        downloader.download("http://example.com/cat.png", tmp_path)
    with pytest.raises(UnsafePath):
        downloader.download("https://127.0.0.1/cat.png", tmp_path)


def test_http_upload_requires_token_and_origin(tmp_path: Path) -> None:
    _project(tmp_path)
    upload_root = tmp_path / "uploads"
    app = create_http_app(
        tmp_path,
        host="127.0.0.1",
        allowed_hosts=["testserver"],
        allowed_origins=["https://agent.example"],
        tokens={"secret": ["comfyui:execute"]},
        upload_root=upload_root,
    )

    with TestClient(app) as client:
        unauthorized = client.post(
            "/assets?server_id=local&filename=cat.png",
            content=b"not-an-image",
            headers={"content-type": "image/png", "origin": "https://agent.example"},
        )
        bad_origin = client.post(
            "/mcp",
            json=_modern_mcp_request("tools/list"),
            headers={
                **_modern_mcp_headers("tools/list"),
                "origin": "https://evil.example",
            },
        )

    assert unauthorized.status_code == 401
    assert bad_origin.status_code == 403


def test_http_upload_and_fetch_are_bounded(tmp_path: Path) -> None:
    _project(tmp_path)
    upload_root = tmp_path / "uploads"
    app = create_http_app(
        tmp_path,
        host="127.0.0.1",
        allowed_hosts=["testserver"],
        allowed_origins=["https://agent.example"],
        tokens={"secret": ["comfyui:execute"]},
        upload_root=upload_root,
        requests_per_minute=2,
    )
    gateway = MagicMock()
    gateway.upload_file.return_value = {
        "name": "cat.png", "subfolder": "agent", "type": "input"
    }
    remote = upload_root / "remote.png"
    headers = {
        "authorization": "Bearer secret",
        "content-type": "image/png",
        "origin": "https://agent.example",
    }
    with (
        patch(
            "comfyui_mcp_skills.adapters.http.server.create_gateway",
            return_value=gateway,
        ),
        patch.object(SafeHTTPSDownloader, "download", return_value=remote),
        TestClient(app) as client,
    ):
        uploaded = client.post(
            "/assets?server_id=local&filename=cat.png",
            content=b"\x89PNG\r\n\x1a\n",
            headers=headers,
        )
        remote.write_bytes(b"\x89PNG\r\n\x1a\n")
        fetched = client.post(
            "/assets/fetch",
            json={"server_id": "local", "url": "https://cdn.example/cat.png"},
            headers={
                "authorization": "Bearer secret",
                "origin": "https://agent.example",
            },
        )
        limited = client.post(
            "/assets?server_id=local&filename=cat.png",
            content=b"\x89PNG\r\n\x1a\n",
            headers=headers,
        )

    assert uploaded.status_code == 201
    assert uploaded.json()["resource_uri"].startswith("comfyui://assets/local/")
    assert fetched.status_code == 201
    assert limited.status_code == 429
    assert "x-request-id" in limited.headers


def test_https_downloader_connects_to_validated_ip(tmp_path: Path) -> None:
    response = MagicMock()
    response.status = 200
    response.headers = {"content-length": "8"}
    response.stream.return_value = [b"12345678"]
    pool = MagicMock()
    pool.request.return_value = response
    downloader = SafeHTTPSDownloader(
        allowed_hosts=["cdn.example"], max_bytes=16
    )

    with (
        patch(
            "comfyui_mcp_skills.adapters.http.security.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        ),
        patch(
            "comfyui_mcp_skills.adapters.http.security.urllib3.HTTPSConnectionPool",
            return_value=pool,
        ) as pool_class,
    ):
        downloaded = downloader.download(
            "https://cdn.example/cat.png?version=1", tmp_path
        )

    assert downloaded.read_bytes() == b"12345678"
    assert pool_class.call_args.args[0] == "93.184.216.34"
    assert pool.request.call_args.kwargs["headers"] == {"Host": "cdn.example"}
    response.release_conn.assert_called_once()
    pool.close.assert_called_once()


def test_http_unknown_server_and_fetch_body_limit(tmp_path: Path) -> None:
    _project(tmp_path)
    app = create_http_app(
        tmp_path,
        host="127.0.0.1",
        allowed_hosts=["testserver"],
        allowed_origins=["https://agent.example"],
        tokens={"secret": ["comfyui:execute"]},
        upload_root=tmp_path / "uploads",
        max_fetch_body_bytes=32,
    )
    headers = {
        "authorization": "Bearer secret",
        "origin": "https://agent.example",
    }
    with TestClient(app) as client:
        missing = client.post(
            "/assets?server_id=missing&filename=cat.png",
            content=b"\x89PNG\r\n\x1a\n",
            headers={**headers, "content-type": "image/png"},
        )
        oversized = client.post(
            "/assets/fetch",
            content=b"{" + b"x" * 64 + b"}",
            headers={**headers, "content-type": "application/json"},
        )

    assert missing.status_code == 404
    assert missing.json()["code"] == "SERVER_NOT_FOUND"
    assert oversized.status_code == 413
    allowed = set(_default_allowed_hosts("127.0.0.1", 8765))
    assert {"127.0.0.1", "127.0.0.1:8765", "localhost:8765"} <= allowed


def _modern_mcp_request(method: str, params: dict[str, object] | None = None) -> dict[str, object]:
    body_params = dict(params or {})
    body_params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
    }
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": body_params}


def _modern_mcp_headers(method: str) -> dict[str, str]:
    return {
        "authorization": "Bearer secret",
        "origin": "https://agent.example",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": method,
    }


def test_http_2026_is_stateless_and_rejects_missing_version_header(tmp_path: Path) -> None:
    _project(tmp_path)
    app = create_http_app(
        tmp_path,
        host="127.0.0.1",
        allowed_hosts=["testserver"],
        allowed_origins=["https://agent.example"],
        tokens={"secret": ["comfyui:execute"]},
        upload_root=tmp_path / "uploads",
    )
    request = _modern_mcp_request("tools/list")

    with TestClient(app) as client:
        modern = client.post(
            "/mcp", json=request, headers=_modern_mcp_headers("tools/list")
        )
        missing_version = client.post(
            "/mcp",
            json=request,
            headers={
                key: value
                for key, value in _modern_mcp_headers("tools/list").items()
                if key != "MCP-Protocol-Version"
            },
        )

    assert modern.status_code == 200
    assert modern.json()["result"]["resultType"] == "complete"
    assert "mcp-session-id" not in modern.headers
    assert missing_version.status_code == 400
    assert missing_version.json()["error"]["code"] == -32020
    assert "mcp-session-id" not in missing_version.headers


def test_static_token_mode_does_not_advertise_oauth_metadata(tmp_path: Path) -> None:
    _project(tmp_path)
    app = create_http_app(
        tmp_path,
        host="127.0.0.1",
        allowed_hosts=["testserver"],
        allowed_origins=["https://agent.example"],
        tokens={"secret": ["comfyui:execute"]},
        upload_root=tmp_path / "uploads",
    )

    with TestClient(app) as client:
        metadata = client.get("/.well-known/oauth-protected-resource/mcp")
        denied = client.post(
            "/mcp",
            json=_modern_mcp_request("tools/list"),
            headers={
                key: value
                for key, value in _modern_mcp_headers("tools/list").items()
                if key != "authorization"
            },
        )

    assert metadata.status_code == 404
    assert "resource_metadata" not in denied.headers.get("www-authenticate", "")


def test_http_2026_wire_headers_methods_and_resource_errors(tmp_path: Path) -> None:
    _project(tmp_path)

    app = create_http_app(
        tmp_path,
        host="127.0.0.1",
        allowed_hosts=["testserver"],
        allowed_origins=["https://agent.example"],
        tokens={"secret": ["comfyui:execute"]},
        upload_root=tmp_path / "uploads",
    )
    base_headers = _modern_mcp_headers("tools/list")
    missing_method_headers = dict(base_headers)
    missing_method_headers.pop("Mcp-Method")
    mismatched_name_headers = _modern_mcp_headers("tools/call")
    mismatched_name_headers["Mcp-Name"] = "comfyui.job.cancel"
    resource_uri = "comfyui://assets/local/asset_missing"
    resource_headers = _modern_mcp_headers("resources/read")
    resource_headers["Mcp-Name"] = resource_uri

    with TestClient(app) as client:
        missing_method = client.post(
            "/mcp",
            json=_modern_mcp_request("tools/list"),
            headers=missing_method_headers,
        )
        mismatched_name = client.post(
            "/mcp",
            json=_modern_mcp_request(
                "tools/call",
                {"name": "comfyui.job.get", "arguments": {}},
            ),
            headers=mismatched_name_headers,
        )
        missing_resource = client.post(
            "/mcp",
            json=_modern_mcp_request("resources/read", {"uri": resource_uri}),
            headers=resource_headers,
        )
        get_response = client.get("/mcp", headers=base_headers)
        delete_response = client.delete("/mcp", headers=base_headers)

    assert missing_method.status_code == 400
    assert missing_method.json()["error"]["code"] == -32020
    assert mismatched_name.status_code == 400
    assert mismatched_name.json()["error"]["code"] == -32020
    assert missing_resource.json()["error"]["code"] == -32602
    assert get_response.status_code == 405
    assert delete_response.status_code == 405


def test_missing_version_requests_are_rate_limited(tmp_path: Path) -> None:
    _project(tmp_path)
    app = create_http_app(
        tmp_path,
        host="127.0.0.1",
        allowed_hosts=["testserver"],
        allowed_origins=["https://agent.example"],
        tokens={"secret": ["comfyui:execute"]},
        upload_root=tmp_path / "uploads",
        requests_per_minute=1,
    )
    headers = {
        key: value
        for key, value in _modern_mcp_headers("tools/list").items()
        if key != "MCP-Protocol-Version"
    }

    with TestClient(app) as client:
        first = client.post(
            "/mcp", json=_modern_mcp_request("tools/list"), headers=headers
        )
        second = client.post(
            "/mcp", json=_modern_mcp_request("tools/list"), headers=headers
        )

    assert first.status_code == 400
    assert second.status_code == 429


def test_request_concurrency_slot_covers_stream_lifetime() -> None:
    first_started = Event()
    release = Event()
    overlap = Event()
    state_lock = Lock()
    active = 0

    async def endpoint(_request: object) -> StreamingResponse:
        async def body():
            nonlocal active
            with state_lock:
                active += 1
                if active > 1:
                    overlap.set()
                first_started.set()
            try:
                await anyio.to_thread.run_sync(release.wait)
                yield b"ok"
            finally:
                with state_lock:
                    active -= 1

        return StreamingResponse(body())

    app = Starlette(routes=[Route("/", endpoint)])
    app.add_middleware(
        RequestControlMiddleware,
        requests_per_minute=10,
        max_concurrent_requests=1,
        bearer_tokens=(),
    )
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.get, "/")
        assert first_started.wait(timeout=2)
        second = pool.submit(client.get, "/")
        did_overlap = overlap.wait(timeout=0.2)
        release.set()
        assert first.result(timeout=2).status_code == 200
        assert second.result(timeout=2).status_code == 200
        assert not did_overlap
