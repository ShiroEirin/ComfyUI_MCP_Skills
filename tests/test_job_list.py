"""Owner-bound SQLite job listing contracts."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from comfyui_mcp_skills.application.jobs import JobService
from comfyui_mcp_skills.application.pagination import encode_keyset_cursor
from comfyui_mcp_skills.domain.errors import WorkflowArgumentsError
from comfyui_mcp_skills.infrastructure.persistence.control_plane import SQLiteControlPlaneStore
from comfyui_mcp_skills.infrastructure.persistence.runs import FileRunRepository
from comfyui_mcp_skills.infrastructure.persistence.sqlite_runs import SQLiteRunRepository

_SAFE_KEYS = {
    "job_uri",
    "job_id",
    "workflow_id",
    "revision_id",
    "deployment_id",
    "server_id",
    "status",
    "created_at",
}


def _identity(kind: str, label: str) -> str:
    return f"{kind}_{hashlib.sha256(label.encode()).hexdigest()}"


def _insert_job(
    store: SQLiteControlPlaneStore,
    label: str,
    *,
    owner_id: str = "owner-a",
    workflow_id: str = "workflow-a",
    server_id: str = "local",
    status: str = "completed",
    created_at: str = "2026-07-31T12:00:00+00:00",
) -> dict[str, str]:
    revision_id = _identity("revision", f"revision:{label}")
    deployment_id = _identity("deployment", f"deployment:{label}")
    plan_id = _identity("plan", f"plan:{label}")
    job_id = _identity("job", f"job:{label}")
    attempt_id = _identity("attempt", f"attempt:{label}:1")
    content_digest = hashlib.sha256(f"content:{label}".encode()).hexdigest()
    input_digest = hashlib.sha256(f"input:{label}".encode()).hexdigest()
    plan_digest = hashlib.sha256(f"plan:{label}".encode()).hexdigest()
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT OR IGNORE INTO workflows(workflow_id, created_at) VALUES (?, ?)",
            (workflow_id, created_at),
        )
        connection.execute(
            """
            INSERT INTO workflow_revisions(
                revision_id, workflow_id, graph_json, parameter_schema_json,
                dependency_contract_json, content_digest, created_at
            ) VALUES (?, ?, '{}', '{}', '{}', ?, ?)
            """,
            (revision_id, workflow_id, content_digest, created_at),
        )
        connection.execute(
            """
            INSERT INTO workflow_deployments(
                deployment_id, workflow_id, revision_id, server_id, enabled,
                validation_status, published, created_at
            ) VALUES (?, ?, ?, ?, 1, 'valid', 0, ?)
            """,
            (deployment_id, workflow_id, revision_id, server_id, created_at),
        )
        connection.execute(
            """
            INSERT INTO execution_plans(
                plan_id, workflow_id, revision_id, deployment_id, server_id,
                resolved_inputs_json, input_digest, plan_digest, created_at
            ) VALUES (?, ?, ?, ?, ?, '{"raw_prompt":"never expose"}', ?, ?, ?)
            """,
            (
                plan_id,
                workflow_id,
                revision_id,
                deployment_id,
                server_id,
                input_digest,
                plan_digest,
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, workflow_id, plan_id, revision_id, deployment_id,
                owner_id, status, error, outputs_json, retry_of, created_at,
                created_at_source, legacy_migrated, execution_origin
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'secret error detail',
                      '[{"secret":"output"}]', NULL, ?, 'runtime', 0, 'planned')
            """,
            (
                job_id,
                workflow_id,
                plan_id,
                revision_id,
                deployment_id,
                owner_id,
                status,
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO execution_attempts(
                attempt_id, job_id, attempt, server_id, upstream_prompt_id,
                upstream_job_id, client_id, submission_state, created_at
            ) VALUES (?, ?, 1, ?, ?, NULL, 'secret-client', 'submitted', ?)
            """,
            (attempt_id, job_id, server_id, f"secret-prompt-{label}", created_at),
        )
    return {
        "job_uri": f"comfyui://jobs/{job_id}",
        "job_id": job_id,
        "workflow_id": workflow_id,
        "revision_id": revision_id,
        "deployment_id": deployment_id,
        "server_id": server_id,
        "status": status,
        "created_at": created_at,
    }


def _service(store: SQLiteControlPlaneStore) -> JobService:
    return JobService(MagicMock(), SQLiteRunRepository(store), MagicMock())


def _store(tmp_path: Path) -> SQLiteControlPlaneStore:
    store = SQLiteControlPlaneStore((tmp_path / "control-plane.sqlite3").resolve())
    store.initialize()
    return store


def test_job_list_is_owner_bound_and_returns_only_canonical_safe_metadata(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    visible = _insert_job(store, "visible", owner_id="owner-a")
    _insert_job(
        store,
        "other-owner",
        owner_id="owner-b",
        created_at="2026-07-31T13:00:00+00:00",
    )

    result = _service(store).list(owner_id="owner-a")

    assert result == {"items": [visible], "next_cursor": ""}
    assert set(result["items"][0]) == _SAFE_KEYS
    serialized = repr(result)
    assert "secret" not in serialized
    assert "raw_prompt" not in serialized
    assert "prompt_id" not in serialized
    assert "error" not in result["items"][0]


def test_job_list_filters_compose_over_status_workflow_server_and_created_time(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    expected_newest = _insert_job(
        store,
        "newest",
        workflow_id="workflow-a",
        server_id="local",
        status="completed",
        created_at="2026-07-31T05:00:00+00:00",
    )
    expected_older = _insert_job(
        store,
        "older",
        workflow_id="workflow-a",
        server_id="local",
        status="completed",
        created_at="2026-07-31T04:00:00+00:00",
    )
    _insert_job(
        store,
        "wrong-status",
        workflow_id="workflow-a",
        server_id="local",
        status="error",
        created_at="2026-07-31T06:00:00+00:00",
    )
    _insert_job(
        store,
        "wrong-workflow",
        workflow_id="workflow-b",
        server_id="local",
        status="completed",
        created_at="2026-07-31T06:00:00+00:00",
    )
    _insert_job(
        store,
        "wrong-server",
        workflow_id="workflow-a",
        server_id="remote",
        status="completed",
        created_at="2026-07-31T06:00:00+00:00",
    )
    _insert_job(
        store,
        "too-old",
        workflow_id="workflow-a",
        server_id="local",
        status="completed",
        created_at="2026-07-31T03:00:00+00:00",
    )

    result = _service(store).list(
        owner_id="owner-a",
        status="completed",
        workflow_id="workflow-a",
        server_id="local",
        created_after="2026-07-31T03:00:00Z",
    )

    assert result == {"items": [expected_newest, expected_older], "next_cursor": ""}


def test_job_list_uses_latest_attempt_server_for_output_and_filtering(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    job_id = _identity("job", "retried")
    created_at = "2026-07-31T07:00:00+00:00"
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, workflow_id, plan_id, revision_id, deployment_id,
                owner_id, status, error, outputs_json, retry_of, created_at,
                created_at_source, legacy_migrated, execution_origin
            ) VALUES (?, 'workflow-retried', NULL, NULL, NULL, 'owner-a',
                      'running', '', '[]', NULL, ?, 'runtime', 0, 'pre_g4_runtime')
            """,
            (job_id, created_at),
        )
        connection.executemany(
            """
            INSERT INTO execution_attempts(
                attempt_id, job_id, attempt, server_id, upstream_prompt_id,
                upstream_job_id, client_id, submission_state, created_at
            ) VALUES (?, ?, ?, ?, ?, NULL, 'secret-client', 'submitted', ?)
            """,
            [
                (
                    _identity("attempt", "retried:1"),
                    job_id,
                    1,
                    "old-server",
                    "secret-old-prompt",
                    created_at,
                ),
                (
                    _identity("attempt", "retried:2"),
                    job_id,
                    2,
                    "new-server",
                    "secret-new-prompt",
                    created_at,
                ),
            ],
        )
    service = _service(store)

    result = service.list(owner_id="owner-a", server_id="new-server")

    assert result == {
        "items": [
            {
                "job_uri": f"comfyui://jobs/{job_id}",
                "job_id": job_id,
                "workflow_id": "workflow-retried",
                "revision_id": "",
                "deployment_id": "",
                "server_id": "new-server",
                "status": "running",
                "created_at": created_at,
            }
        ],
        "next_cursor": "",
    }
    assert service.list(owner_id="owner-a", server_id="old-server") == {
        "items": [],
        "next_cursor": "",
    }


def test_job_list_keyset_cursor_has_no_duplicates_or_skips_and_is_filter_bound(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    expected = [
        _insert_job(
            store,
            label,
            status="completed",
            created_at=created_at,
        )
        for label, created_at in (
            ("first", "2026-07-31T05:00:00+00:00"),
            ("tie-a", "2026-07-31T04:00:00+00:00"),
            ("tie-b", "2026-07-31T04:00:00+00:00"),
            ("tie-c", "2026-07-31T04:00:00+00:00"),
            ("last", "2026-07-31T03:00:00+00:00"),
        )
    ]
    expected.sort(key=lambda item: item["job_id"])
    expected.sort(key=lambda item: item["created_at"], reverse=True)
    service = _service(store)

    first = service.list(owner_id="owner-a", limit=2)
    second = service.list(owner_id="owner-a", limit=2, cursor=first["next_cursor"])
    third = service.list(owner_id="owner-a", limit=2, cursor=second["next_cursor"])

    assert first["next_cursor"] and second["next_cursor"]
    assert third["next_cursor"] == ""
    assert first["next_cursor"].isascii()
    assert "2026-07-31" not in first["next_cursor"]
    assert first["items"] + second["items"] + third["items"] == expected
    offset_cursor = encode_keyset_cursor(
        "2026-07-31T06:00:00+02:00",
        expected[1]["job_id"],
        filters={
            "owner_id": "owner-a",
            "status": "",
            "workflow_id": "",
            "server_id": "",
            "created_after": "",
        },
    )
    assert (
        service.list(owner_id="owner-a", limit=2, cursor=offset_cursor)["items"][0] == expected[2]
    )
    assert len({item["job_id"] for item in expected}) == len(expected)
    with pytest.raises(WorkflowArgumentsError, match="cursor"):
        service.list(owner_id="owner-a", status="completed", cursor=first["next_cursor"])
    with pytest.raises(WorkflowArgumentsError, match="cursor"):
        service.list(owner_id="owner-b", cursor=first["next_cursor"])
    with pytest.raises(WorkflowArgumentsError, match="cursor"):
        service.list(owner_id="owner-a", cursor="not-a-valid-cursor")
    with pytest.raises(WorkflowArgumentsError, match="cursor"):
        service.list(owner_id="owner-a", cursor=None)  # type: ignore[arg-type]


@pytest.mark.parametrize("limit", [True, 0, -1, 101, 1.5, "10"])
def test_job_list_rejects_unbounded_or_non_integer_limits(tmp_path: Path, limit: Any) -> None:
    with pytest.raises(WorkflowArgumentsError, match="limit"):
        _service(_store(tmp_path)).list(owner_id="owner-a", limit=limit)


@pytest.mark.parametrize(
    ("filters", "message"),
    [
        ({"status": "unknown"}, "status"),
        ({"workflow_id": "../unsafe"}, "workflow_id"),
        ({"server_id": "local/path"}, "server_id"),
        ({"created_after": "yesterday"}, "created_after"),
        ({"created_after": "2026-07-31T00:00:00"}, "created_after"),
    ],
)
def test_job_list_rejects_invalid_filters(
    tmp_path: Path, filters: dict[str, Any], message: str
) -> None:
    with pytest.raises(WorkflowArgumentsError, match=message):
        _service(_store(tmp_path)).list(owner_id="owner-a", **filters)


def test_file_run_repository_explicitly_rejects_listing_without_scanning(
    tmp_path: Path,
) -> None:
    repository = FileRunRepository(tmp_path)
    repository._read_record = MagicMock(side_effect=AssertionError("must not scan"))  # type: ignore[method-assign]

    with pytest.raises(NotImplementedError, match="unsupported"):
        repository.list_jobs("owner-a", limit=10)

    repository._read_record.assert_not_called()  # type: ignore[attr-defined]
