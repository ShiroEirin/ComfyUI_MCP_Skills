"""Production file-store cutover entry-point contracts."""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest

from comfyui_mcp_skills.cutover_main import CONFIRMATION, main
from comfyui_mcp_skills.infrastructure.persistence.repository_factory import (
    create_repository_bundle,
)


@pytest.fixture
def private_backup_dir() -> Path:
    path = Path.home() / f".comfyui-mcp-cutover-test-{uuid.uuid4().hex}"
    path.mkdir(mode=0o700)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _project(root: Path) -> None:
    workflow = root / "data" / "local" / "portrait"
    workflow.mkdir(parents=True)
    (workflow / "schema.json").write_text(
        json.dumps({"description": "Portrait", "enabled": True, "parameters": {}}),
        encoding="utf-8",
    )
    (workflow / "workflow.json").write_text(
        json.dumps({"1": {"class_type": "SaveImage", "inputs": {}}}),
        encoding="utf-8",
    )


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    backup: Path,
    *,
    confirmation: str = CONFIRMATION,
) -> None:
    monkeypatch.setenv("COMFYUI_MCP_DIR", str(project))
    monkeypatch.setenv("COMFYUI_MCP_MIGRATION_BACKUP", str(backup))
    monkeypatch.setenv("COMFYUI_MCP_MIGRATION_CONFIRM", confirmation)
    monkeypatch.delenv("COMFYUI_MCP_MIGRATION_EVIDENCE", raising=False)


def test_cutover_requires_exact_confirmation_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    _project(project)
    _configure(monkeypatch, project, tmp_path / "backup", confirmation="yes")

    assert main() == 3

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "migration_confirmation_required"
    assert not (project / "data" / "control-plane.sqlite3").exists()


def test_cutover_switches_all_file_store_groups_and_is_idempotent(
    tmp_path: Path,
    private_backup_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    backup = private_backup_dir
    _project(project)
    _configure(monkeypatch, project, backup)

    assert main() == 0
    first = json.loads(capsys.readouterr().out)

    assert first["ok"] is True
    assert first["writes_performed"] is True
    assert first["backup"]["verified"] is True
    assert first["groups"] == {
        "asset": "switched",
        "job": "switched",
        "workflow": "switched",
    }
    bundle = create_repository_bundle(project)
    assert (bundle.asset_store, bundle.run_store, bundle.workflow_store) == (
        "sqlite",
        "sqlite",
        "sqlite",
    )
    with sqlite3.connect(project / "data" / "control-plane.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM workflows").fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM store_migrations WHERE status='switched'"
        ).fetchone() == (8,)

    monkeypatch.setenv("COMFYUI_MCP_MIGRATION_EVIDENCE", first["backup"]["destination"])
    assert main() == 0
    second = json.loads(capsys.readouterr().out)
    assert second["groups"] == {
        "asset": "already_switched",
        "job": "already_switched",
        "workflow": "already_switched",
    }


def test_cutover_rejects_source_drift_against_reused_evidence(
    tmp_path: Path,
    private_backup_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    backup = private_backup_dir
    _project(project)
    _configure(monkeypatch, project, backup)
    assert main() == 0
    evidence = json.loads(capsys.readouterr().out)["backup"]["destination"]

    schema = project / "data" / "local" / "portrait" / "schema.json"
    schema.write_text(
        json.dumps({"description": "Changed", "enabled": True, "parameters": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("COMFYUI_MCP_MIGRATION_EVIDENCE", evidence)

    assert main() == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "migration_cutover_failed"
    assert "source drift" in payload["error"]["message"]



def test_cutover_failure_reports_durable_partial_switch(
    tmp_path: Path,
    private_backup_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    _project(project)
    _configure(monkeypatch, project, private_backup_dir)

    def fail_workflow(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected workflow cutover failure")

    monkeypatch.setattr(
        "comfyui_mcp_skills.cutover_main.cutover_g3_import_plan", fail_workflow
    )

    assert main() == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["writes_performed"] is True
    assert payload["groups"] == {
        "asset": "switched",
        "job": "switched",
        "workflow": "not_switched",
    }
    assert payload["recovery"]["evidence"] == payload["backup"]["destination"]