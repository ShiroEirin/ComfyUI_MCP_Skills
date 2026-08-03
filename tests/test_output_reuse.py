"""Server-side ComfyUI output reuse contracts."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from comfyui_mcp_skills.application.execution import (
    DIRECT_OUTPUT_COMPATIBILITY_REGISTRY_VERSION,
    ExecutionService,
)
from comfyui_mcp_skills.domain.errors import AssetLibraryConflict, AssetNotFound
from comfyui_mcp_skills.domain.models import Asset, Job, Workflow


class _Catalog:
    def __init__(
        self,
        media_type: str = "image",
        consumer_class: str = "LoadImageOutput",
        *,
        field: str = "image",
        storage_type: str = "output",
    ) -> None:
        self.workflow = Workflow(
            server_id="local",
            workflow_id="reuse",
            description="Reuse an existing output",
            parameters={
                "media": {
                    "type": media_type,
                    "required": True,
                    "node_id": "1",
                    "field": field,
                    "storage_type": storage_type,
                }
            },
            graph={"1": {"class_type": consumer_class, "inputs": {field: "old"}}},
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
    def __init__(self) -> None:
        self.asset = Asset(
            asset_id="asset_" + "d" * 32,
            server_id="local",
            comfyui_ref="inputs/render.png",
            name="render.png",
            subfolder="inputs",
            media_type="image",
            mime_type="image/png",
            size_bytes=3,
            sha256="e" * 64,
            owner_id="owner-a",
        )

    def get(self, asset_id: str) -> Asset | None:
        return self.asset if asset_id == self.asset.asset_id else None


class _ArtifactRecord:
    artifact_id = "artifact_" + "c" * 64

    def __init__(
        self,
        *,
        filename: str = "render.png",
        subfolder: str = "renders/final",
        storage_type: str = "output",
        media_type: str = "image",
    ) -> None:
        self.server_id = "local"
        self.filename = filename
        self.subfolder = subfolder
        self.storage_type = storage_type
        self.media_type = media_type


class _Artifacts:
    def __init__(
        self,
        *,
        state: str = "available",
        owner_id: str = "owner-a",
        record: _ArtifactRecord | None = None,
    ) -> None:
        self.state = state
        self.owner_id = owner_id
        self.record = record or _ArtifactRecord()

    def record_artifacts(self, job: Job, observations: Any) -> tuple[Any, ...]:
        return ()

    def terminalize(
        self,
        job: Job,
        observations: Any,
        *,
        failure_injector: Any = None,
    ) -> tuple[Any, ...]:
        if failure_injector is not None:
            failure_injector()
        return self.record_artifacts(job, observations)

    def get_artifact(self, artifact_id: str, owner_id: str) -> _ArtifactRecord | None:
        if self.state == "backfill_pending":
            raise AssetLibraryConflict(
                "Phase L evidence is incomplete",
                details={"reason": "backfill_pending"},
            )
        if (
            self.state == "available"
            and artifact_id == _ArtifactRecord.artifact_id
            and owner_id == self.owner_id
        ):
            return self.record
        return None

    def resolve_artifact_alias(self, uri: str, owner_id: str) -> _ArtifactRecord | None:
        if self.state == "backfill_pending":
            raise AssetLibraryConflict(
                "Phase L evidence is incomplete",
                details={"reason": "backfill_pending"},
            )
        if (
            self.state == "available"
            and uri == "comfyui://outputs/local/source-prompt/0"
            and owner_id == self.owner_id
        ):
            return self.record
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
    consumer_class: str = "LoadImageOutput",
    field: str = "image",
    storage_type: str = "output",
    artifact_state: str = "available",
    artifact_filename: str = "render.png",
    artifact_subfolder: str = "renders/final",
    artifact_storage_type: str = "output",
    artifact_media_type: str = "image",
) -> tuple[ExecutionService, _Gateway]:
    gateway = _Gateway(output_media_type=output_media_type)
    service = ExecutionService(
        _Catalog(
            parameter_media_type,
            consumer_class,
            field=field,
            storage_type=storage_type,
        ),
        _Servers(),
        _Runs(source_owner),
        _Assets(),
        lambda _connection: gateway,
        artifacts=_Artifacts(
            state=artifact_state,
            owner_id=source_owner,
            record=_ArtifactRecord(
                filename=artifact_filename,
                subfolder=artifact_subfolder,
                storage_type=artifact_storage_type,
                media_type=artifact_media_type,
            ),
        ),
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
    assert gateway.queued[0]["1"]["inputs"]["image"] == ("renders/final/render.png [output]")
    gateway.download_output.assert_not_called()


def test_legacy_output_alias_uses_canonical_artifact_facts_not_raw_job_outputs() -> None:
    service, gateway = _service()
    gateway.history["outputs"]["9"]["images"][0].update(
        filename="raw-job-output.png",
        subfolder="stale",
    )

    service.submit(
        "local",
        "reuse",
        {"media": "comfyui://outputs/local/source-prompt/0"},
        owner_id="owner-a",
    )

    assert gateway.queued[0]["1"]["inputs"]["image"] == ("renders/final/render.png [output]")


@pytest.mark.parametrize("artifact_state", ["archived", "deleted", "backfill_pending"])
@pytest.mark.parametrize(
    "uri",
    [
        "comfyui://outputs/local/source-prompt/0",
        f"comfyui://artifacts/{_ArtifactRecord.artifact_id}",
    ],
)
def test_canonical_and_legacy_output_reuse_reject_incomplete_or_unavailable_artifacts(
    artifact_state: str,
    uri: str,
) -> None:
    service, gateway = _service(artifact_state=artifact_state)

    error_type = AssetLibraryConflict if artifact_state == "backfill_pending" else AssetNotFound
    with pytest.raises(error_type) as raised:
        service.submit("local", "reuse", {"media": uri}, owner_id="owner-a")
    if artifact_state == "backfill_pending":
        assert raised.value.details == {"reason": "backfill_pending"}

    assert gateway.queued == []


def test_reuses_canonical_artifact_on_verified_output_consumer() -> None:
    service, gateway = _service()

    service.submit(
        "local",
        "reuse",
        {"media": f"comfyui://artifacts/{_ArtifactRecord.artifact_id}"},
        owner_id="owner-a",
    )

    assert gateway.queued[0]["1"]["inputs"]["image"] == "renders/final/render.png [output]"
    gateway.download_output.assert_not_called()


def test_direct_output_compatibility_registry_is_fixed_and_field_bound() -> None:
    assert DIRECT_OUTPUT_COMPATIBILITY_REGISTRY_VERSION == 1
    service, gateway = _service(field="media")

    with pytest.raises(AssetNotFound, match="must use an imported Asset"):
        service.submit(
            "local",
            "reuse",
            {"media": f"comfyui://artifacts/{_ArtifactRecord.artifact_id}"},
            owner_id="owner-a",
        )

    assert gateway.queued == []


def test_unknown_consumer_never_gains_direct_output_reuse() -> None:
    service, gateway = _service(consumer_class="CustomImageLoader")

    with pytest.raises(AssetNotFound, match="must use an imported Asset"):
        service.submit(
            "local",
            "reuse",
            {"media": f"comfyui://artifacts/{_ArtifactRecord.artifact_id}"},
            owner_id="owner-a",
        )

    assert gateway.queued == []


def test_load_image_output_requires_output_storage_semantics() -> None:
    service, gateway = _service(storage_type="input")

    with pytest.raises(AssetNotFound, match="must use an imported Asset"):
        service.submit(
            "local",
            "reuse",
            {"media": f"comfyui://artifacts/{_ArtifactRecord.artifact_id}"},
            owner_id="owner-a",
        )

    assert gateway.queued == []


def test_forged_output_metadata_cannot_direct_reuse_through_load_image() -> None:
    service, gateway = _service(consumer_class="LoadImage")

    with pytest.raises(AssetNotFound, match="must use an imported Asset"):
        service.submit(
            "local",
            "reuse",
            {"media": "comfyui://outputs/local/source-prompt/0"},
            owner_id="owner-a",
        )

    assert gateway.queued == []
    gateway.download_output.assert_not_called()


def test_load_image_accepts_imported_asset_despite_forged_output_metadata() -> None:
    service, gateway = _service(consumer_class="LoadImage")

    service.submit(
        "local",
        "reuse",
        {"media": "asset_" + "d" * 32},
        owner_id="owner-a",
    )

    assert gateway.queued[0]["1"]["inputs"]["image"] == "inputs/render.png"
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

    with pytest.raises(AssetNotFound):
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

    with pytest.raises(AssetNotFound, match="must use an imported Asset"):
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
    service, gateway = _service(artifact_subfolder="")

    service.submit(
        "local",
        "reuse",
        {"media": "comfyui://outputs/local/source-prompt/0"},
        owner_id="owner-a",
    )

    assert gateway.queued[0]["1"]["inputs"]["image"] == "render.png [output]"
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
    service, gateway = _service(
        artifact_filename=filename,
        artifact_subfolder=subfolder,
        artifact_storage_type=storage_type,
    )

    with pytest.raises(AssetNotFound, match="Output not found|unsafe|unsupported"):
        service.submit(
            "local",
            "reuse",
            {"media": "comfyui://outputs/local/source-prompt/0"},
            owner_id="owner-a",
        )

    assert gateway.queued == []
    gateway.download_output.assert_not_called()
