"""Server-side ComfyUI output reuse contracts."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from comfyui_mcp_skills.domain.errors import AssetNotFound, JobNotFound
from comfyui_mcp_skills.application.execution import ExecutionService
from comfyui_mcp_skills.domain.models import Job, Workflow


class _Catalog:
    def __init__(self, media_type: str = "image") -> None:
        self.workflow = Workflow(
            server_id="local",
            workflow_id="reuse",
            description="Reuse an existing output",
            parameters={
                "media": {
                    "type": media_type,
                    "required": True,
                    "node_id": "1",
                    "field": "media",
                    "storage_type": "output",
                }
            },
            graph={"1": {"class_type": "LoadMedia", "inputs": {"media": "old"}}},
        )

    def get(self, server_id: str, workflow_id: str) -> Workflow:
        assert (server_id, workflow_id) == ("local", "reuse")
        return self.workflow


class _Servers:
    def connection(self, server_id: str) -> dict[str, Any]:
        return {"id": server_id, "url": f"http://{server_id}.invalid"}


class _Runs:
    def __init__(self, source_owner: str = "owner-a") -> None:
        self.jobs = {
            ("local", "source-prompt"): Job(
                prompt_id="source-prompt",
                server_id="local",
                workflow_id="producer",
                status="completed",
                owner_id=source_owner,
            )
        }

    def request_digest(self, workflow_id: str, arguments: dict[str, Any]) -> str:
        return f"{workflow_id}:{arguments!r}"

    def get(self, server_id: str, prompt_id: str) -> Job | None:
        return self.jobs.get((server_id, prompt_id))

    def save(self, job: Job, *, lease_token: str = "") -> None:
        self.jobs[(job.server_id, job.prompt_id)] = job


class _Assets:
    def get(self, _asset_id: str) -> None:
        return None


class _Gateway:
    def __init__(self, *, output_media_type: str = "image") -> None:
        output_key = {
            "image": "images",
            "audio": "audio",
            "video": "gifs",
        }[output_media_type]
        filename = {
            "image": "render.png",
            "audio": "render.wav",
            "video": "render.mp4",
        }[output_media_type]
        self.history = {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {
                "9": {
                    output_key: [
                        {
                            "filename": filename,
                            "subfolder": "renders/final",
                            "type": "output",
                        }
                    ]
                }
            },
        }
        self.queued: list[dict[str, Any]] = []
        self.download_output = MagicMock(side_effect=AssertionError("must not download output"))

    def get_history(
        self, prompt_id: str, timeout_seconds: float | None = None
    ) -> dict[str, Any] | None:
        return self.history if prompt_id == "source-prompt" else None

    def get_queue(self, timeout_seconds: float | None = None) -> dict[str, Any]:
        return {"queue_running": [], "queue_pending": []}

    def queue_prompt(self, workflow: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        self.queued.append(workflow)
        return {"prompt_id": "reused-prompt"}


def _service(
    *,
    parameter_media_type: str = "image",
    output_media_type: str = "image",
    source_owner: str = "owner-a",
) -> tuple[ExecutionService, _Gateway]:
    gateway = _Gateway(output_media_type=output_media_type)
    service = ExecutionService(
        _Catalog(parameter_media_type),
        _Servers(),
        _Runs(source_owner),
        _Assets(),
        lambda _connection: gateway,
    )
    return service, gateway


def test_reuses_owned_output_on_same_server_without_downloading() -> None:
    service, gateway = _service()

    submitted = service.submit(
        "local",
        "reuse",
        {"media": "comfyui://outputs/local/source-prompt/0"},
        owner_id="owner-a",
    )

    assert submitted.prompt_id == "reused-prompt"
    assert gateway.queued[0]["1"]["inputs"]["media"] == ("renders/final/render.png [output]")
    gateway.download_output.assert_not_called()


def test_output_only_parameter_rejects_plain_input_reference() -> None:
    service, gateway = _service()

    with pytest.raises(AssetNotFound, match="requires an output URI"):
        service.submit("local", "reuse", {"media": "uploaded.png"})

    assert gateway.queued == []


def test_rejects_output_from_another_server() -> None:
    service, gateway = _service()

    with pytest.raises(AssetNotFound, match="does not belong to server"):
        service.submit(
            "local",
            "reuse",
            {"media": "comfyui://outputs/remote/source-prompt/0"},
            owner_id="owner-a",
        )

    assert gateway.queued == []
    gateway.download_output.assert_not_called()


def test_rejects_output_owned_by_another_principal() -> None:
    service, gateway = _service(source_owner="owner-b")

    with pytest.raises(JobNotFound):
        service.submit(
            "local",
            "reuse",
            {"media": "comfyui://outputs/local/source-prompt/0"},
            owner_id="owner-a",
        )

    assert gateway.queued == []
    gateway.download_output.assert_not_called()


def test_rejects_output_index_out_of_range() -> None:
    service, gateway = _service()

    with pytest.raises(AssetNotFound, match="Output not found"):
        service.submit(
            "local",
            "reuse",
            {"media": "comfyui://outputs/local/source-prompt/1"},
            owner_id="owner-a",
        )

    assert gateway.queued == []
    gateway.download_output.assert_not_called()


def test_rejects_output_with_incompatible_media_type() -> None:
    service, gateway = _service(parameter_media_type="audio", output_media_type="image")

    with pytest.raises(AssetNotFound, match="is image, expected audio"):
        service.submit(
            "local",
            "reuse",
            {"media": "comfyui://outputs/local/source-prompt/0"},
            owner_id="owner-a",
        )

    assert gateway.queued == []
    gateway.download_output.assert_not_called()


def test_rejects_output_uri_with_unsafe_prompt_identifier() -> None:
    service, gateway = _service()

    with pytest.raises(AssetNotFound, match="Invalid output URI"):
        service.submit(
            "local",
            "reuse",
            {"media": "comfyui://outputs/local/../0"},
            owner_id="owner-a",
        )

    assert gateway.queued == []
    gateway.download_output.assert_not_called()


def test_reuses_output_with_empty_subfolder_as_annotated_filename() -> None:
    service, gateway = _service()
    gateway.history["outputs"]["9"]["images"][0]["subfolder"] = ""

    service.submit(
        "local",
        "reuse",
        {"media": "comfyui://outputs/local/source-prompt/0"},
        owner_id="owner-a",
    )

    assert gateway.queued[0]["1"]["inputs"]["media"] == "render.png [output]"
    gateway.download_output.assert_not_called()


@pytest.mark.parametrize(
    "uri",
    [
        "comfyui://outputs/local/source-prompt/-1",
        "comfyui://outputs/local/source-prompt/0?download=true",
        "comfyui://outputs/local/source-prompt/0/extra",
        "comfyui://outputs/local//0",
    ],
)
def test_rejects_malformed_output_uri(uri: str) -> None:
    service, gateway = _service()

    with pytest.raises(AssetNotFound, match="Invalid output URI"):
        service.submit("local", "reuse", {"media": uri}, owner_id="owner-a")

    assert gateway.queued == []
    gateway.download_output.assert_not_called()


@pytest.mark.parametrize(
    ("filename", "subfolder", "storage_type"),
    [
        ("", "", "output"),
        ("../render.png", "", "output"),
        ("/render.png", "", "output"),
        ("render.png", "../renders", "output"),
        ("render.png", "/renders", "output"),
        ("render.png", "", "input"),
    ],
)
def test_rejects_unsafe_or_non_output_server_reference(
    filename: str,
    subfolder: str,
    storage_type: str,
) -> None:
    service, gateway = _service()
    output = gateway.history["outputs"]["9"]["images"][0]
    output.update(filename=filename, subfolder=subfolder, type=storage_type)

    with pytest.raises(AssetNotFound, match="unsafe|unsupported"):
        service.submit(
            "local",
            "reuse",
            {"media": "comfyui://outputs/local/source-prompt/0"},
            owner_id="owner-a",
        )

    assert gateway.queued == []
    gateway.download_output.assert_not_called()
