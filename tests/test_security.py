"""Security regression tests for imported bundles and file-backed identifiers."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import typer

from comfyui_mcp_skills.infrastructure.persistence.workflows import FileWorkflowRepository
from comfyui_skills_cli.commands.config import config_import
from comfyui_skills_cli.commands.run import _download_outputs
from comfyui_skills_cli.history_writer import (
    claim_job,
    find_existing_run,
    release_job_claim,
    renew_job_claim,
    save_run_record,
)


def _ctx(base_dir: Path) -> typer.Context:
    ctx = MagicMock(spec=typer.Context)
    ctx.obj = {"base_dir": str(base_dir), "output_format": "json"}
    return ctx


def test_config_import_rejects_workflow_path_traversal(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    bundle_path = tmp_path / "malicious.json"
    bundle_path.write_text(
        json.dumps(
            {
                "config": {"servers": []},
                "workflows": {
                    "local/../../../escaped": {
                        "workflow": {"1": {"class_type": "SaveImage", "inputs": {}}},
                        "schema": {"parameters": {}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(typer.Exit):
        config_import(_ctx(tmp_path), str(bundle_path), False, False, False)

    assert not (tmp_path.parent / "escaped").exists()


def test_job_id_is_not_used_as_a_file_path(tmp_path: Path) -> None:
    (tmp_path / "data" / "local" / "flow").mkdir(parents=True)

    save_run_record(
        tmp_path,
        "local",
        "flow",
        "prompt-1",
        {},
        "completed",
        job_id="../../escaped",
    )

    assert find_existing_run(tmp_path, "local", "flow", "../../escaped") is not None
    assert not (tmp_path / "escaped.json").exists()


def test_job_claim_is_atomic_for_concurrent_retries(tmp_path: Path) -> None:
    (tmp_path / "data" / "local" / "flow").mkdir(parents=True)

    def claim() -> str | bool:
        return claim_job(tmp_path, "local", "flow", "same-key", {"prompt": "cat"})

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _index: claim(), range(16)))

    assert sum(bool(result) for result in results) == 1
    assert results.count(False) == 15


def test_job_claim_renewal_rejects_released_lease(tmp_path: Path) -> None:
    (tmp_path / "data" / "local" / "flow").mkdir(parents=True)
    token = claim_job(tmp_path, "local", "flow", "same-key", {"prompt": "cat"})
    assert isinstance(token, str)

    renew_job_claim(tmp_path, "local", "flow", "same-key", token)
    release_job_claim(tmp_path, "local", "flow", "same-key", token)

    with pytest.raises(RuntimeError, match="lease is no longer owned"):
        renew_job_claim(tmp_path, "local", "flow", "same-key", token)


def test_workflow_enabled_string_fails_closed(tmp_path: Path) -> None:
    workflow_dir = tmp_path / "data" / "local" / "flow"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "schema.json").write_text(
        json.dumps({"enabled": "true", "parameters": {}}), encoding="utf-8"
    )
    (workflow_dir / "workflow.json").write_text("{}", encoding="utf-8")

    workflows = FileWorkflowRepository(tmp_path).list()
    assert len(workflows) == 1
    assert workflows[0].enabled is False


def test_output_download_rejects_path_traversal(tmp_path: Path) -> None:
    client = MagicMock()
    client.download_output.return_value = b"untrusted"
    outputs = [
        {
            "filename": "../../escaped.bin",
            "subfolder": "",
            "type": "output",
            "media_type": "image",
        }
    ]

    result = _download_outputs(
        client,
        outputs,
        tmp_path,
        {"output_dir": "outputs"},
    )

    assert result[0]["local_path"] == ""
    assert not (tmp_path / "escaped.bin").exists()


def test_config_import_rejects_non_object_parameters(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    bundle_path = tmp_path / "invalid-schema.json"
    bundle_path.write_text(
        json.dumps(
            {
                "config": {"servers": []},
                "workflows": {
                    "local/bad": {
                        "workflow": {},
                        "schema": {"enabled": True, "parameters": None},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(typer.Exit):
        config_import(_ctx(tmp_path), str(bundle_path), False, False, False)

    assert not (tmp_path / "data" / "local" / "bad").exists()
