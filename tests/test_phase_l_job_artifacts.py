"""Phase L Job output observation and Artifact URI contracts."""

from __future__ import annotations

from typing import Any

from comfyui_mcp_skills.application.jobs import JobService
from comfyui_mcp_skills.domain.control_plane import derive_legacy_artifact_id
from comfyui_mcp_skills.domain.models import Job


class _Servers:
    def connection(self, server_id: str) -> dict[str, str]:
        return {"id": server_id, "url": "http://local.invalid"}


class _Runs:
    def __init__(self, job: Job) -> None:
        self.job = job
        self.save_calls: list[Job] = []

    def get(self, server_id: str, prompt_id: str) -> Job | None:
        if (server_id, prompt_id) == (self.job.server_id, self.job.prompt_id):
            return self.job
        return None

    def save(self, job: Job, *, lease_token: str = "") -> None:
        self.save_calls.append(job)
        self.job = job


class _Gateway:
    def __init__(self, history: dict[str, Any]) -> None:
        self.history = history

    def get_history(
        self, prompt_id: str, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        return self.history


class _Artifacts:
    def __init__(self) -> None:
        self.calls: list[tuple[Job, tuple[dict[str, Any], ...]]] = []

    def terminalize(
        self,
        job: Job,
        observations: tuple[dict[str, Any], ...],
        *,
        failure_injector: Any = None,
    ) -> tuple[Any, ...]:
        self.calls.append((job, observations))
        return ()


def _history() -> dict[str, Any]:
    return {
        "status": {"completed": True, "status_str": "success"},
        "outputs": {
            "10": {
                "video": [
                    {
                        "filename": "trailer.mp4",
                        "subfolder": "movies",
                        "type": "output",
                    }
                ]
            },
            "2": {
                "images": [{"filename": "still.png", "subfolder": "", "type": "output"}],
                "gifs": [
                    {
                        "filename": "animation.gif",
                        "subfolder": "animations",
                        "type": "output",
                    }
                ],
                "audio": [{"filename": "sound.mp3", "subfolder": "audio", "type": "output"}],
                "video": [{"filename": "clip.webm", "subfolder": "video", "type": "output"}],
            },
        },
    }


def test_completion_preserves_source_coordinates_and_records_artifacts() -> None:
    job_id = "job_" + "a" * 64
    runs = _Runs(
        Job(
            "prompt-mixed",
            "local",
            "producer",
            "submitted",
            owner_id="owner-a",
            job_id=job_id,
        )
    )
    gateway = _Gateway(_history())
    artifacts = _Artifacts()
    service = JobService(_Servers(), runs, lambda _config: gateway, artifacts=artifacts)

    completed = service.get("local", "prompt-mixed", owner_id="owner-a")

    assert [
        (
            output["upstream_node_id"],
            output["output_key"],
            output["upstream_output_index"],
            output["legacy_index"],
            output["media_type"],
            output["mime_type"],
        )
        for output in completed.outputs
    ] == [
        ("10", "video", 0, 0, "video", "video/mp4"),
        ("2", "images", 0, 1, "image", "image/png"),
        ("2", "gifs", 0, 2, "image", "image/gif"),
        ("2", "audio", 0, 3, "audio", "audio/mpeg"),
        ("2", "video", 0, 4, "video", "video/webm"),
    ]
    for output in completed.outputs:
        artifact_id = derive_legacy_artifact_id(
            job_id,
            output["upstream_node_id"],
            output["output_key"],
            output["upstream_output_index"],
            output["filename"],
            output["subfolder"],
            output["type"],
        )
        assert output["artifact_id"] == artifact_id
        assert output["resource_uri"] == f"comfyui://artifacts/{artifact_id}"
        assert output["legacy_uri"] == (
            f"comfyui://outputs/local/prompt-mixed/{output['legacy_index']}"
        )
    assert artifacts.calls == [(completed, completed.outputs)]
    assert runs.save_calls == []


def test_video_only_history_uses_zero_legacy_index_without_artifact_repository() -> None:
    runs = _Runs(Job("prompt-video", "local", "producer", "submitted"))
    history = {
        "status": {"completed": True, "status_str": "success"},
        "outputs": {
            "9": {
                "video": [
                    {
                        "filename": "render.webm",
                        "subfolder": "video",
                        "type": "output",
                    }
                ]
            }
        },
    }
    service = JobService(_Servers(), runs, lambda _config: _Gateway(history))

    completed = service.get("local", "prompt-video")

    output = completed.outputs[0]
    assert (
        output["upstream_node_id"],
        output["output_key"],
        output["upstream_output_index"],
        output["legacy_index"],
    ) == ("9", "video", 0, 0)
    assert output["media_type"] == "video"
    assert output["mime_type"] == "video/webm"
    assert output["resource_uri"] == "comfyui://outputs/local/prompt-video/0"
    assert output["legacy_uri"] == output["resource_uri"]
    assert output["canonical_uri"].startswith("comfyui://artifacts/artifact_")
