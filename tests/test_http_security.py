"""Remote transport security and upload contracts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from comfyui_mcp_skills.adapters.http.security import SafeHTTPSDownloader
from comfyui_mcp_skills.adapters.http.server import create_http_app
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
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={
                "authorization": "Bearer secret",
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
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
