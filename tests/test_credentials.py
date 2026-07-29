"""Credential redaction and safe export behavior."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from comfyui_skills_cli.main import app


runner = CliRunner()


def _project(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "config.json").write_text(
        json.dumps({"servers": [], "default_server": ""}),
        encoding="utf-8",
    )


def test_server_add_never_returns_credentials(tmp_path: Path) -> None:
    _project(tmp_path)

    result = runner.invoke(
        app,
        [
            "--dir",
            str(tmp_path),
            "--json",
            "--no-update-check",
            "server",
            "add",
            "--id",
            "local",
            "--url",
            "http://127.0.0.1:8188",
            "--auth",
            "secret-token",
            "--api-key",
            "secret-api-key",
        ],
    )

    assert result.exit_code == 0
    assert "secret-token" not in result.stdout
    assert "secret-api-key" not in result.stdout
    payload = json.loads(result.stdout)
    assert "auth" not in payload
    assert "comfy_api_key" not in payload



def test_server_add_rejects_noncanonical_identifier(tmp_path: Path) -> None:
    _project(tmp_path)

    result = runner.invoke(
        app,
        [
            "--dir",
            str(tmp_path),
            "--json",
            "--no-update-check",
            "server",
            "add",
            "--id",
            "prod:1",
            "--url",
            "http://127.0.0.1:8188",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["code"] == "INVALID_ID"
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["servers"] == []

def test_config_export_excludes_credentials_by_default(tmp_path: Path) -> None:
    _project(tmp_path)
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "id": "local",
                        "name": "local",
                        "url": "http://127.0.0.1:8188",
                        "auth": "secret-token",
                        "comfy_api_key": "secret-api-key",
                    }
                ],
                "default_server": "local",
            }
        ),
        encoding="utf-8",
    )
    export_path = tmp_path / "bundle.json"

    result = runner.invoke(
        app,
        [
            "--dir",
            str(tmp_path),
            "--json",
            "--no-update-check",
            "config",
            "export",
            "--output",
            str(export_path),
        ],
    )

    assert result.exit_code == 0
    exported = export_path.read_text(encoding="utf-8")
    assert "secret-token" not in exported
    assert "secret-api-key" not in exported
    bundle = json.loads(exported)
    assert "url" not in bundle["config"]["servers"][0]
