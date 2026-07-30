"""Control-plane identity and resource URI contracts."""

from __future__ import annotations

import pytest

from comfyui_mcp_skills.domain.control_plane import (
    CanonicalResourceKind,
    ControlPlaneKind,
    LegacyResourceRef,
    canonical_resource_uri,
    derive_legacy_artifact_id,
    derive_legacy_conflicting_workflow_id,
    derive_legacy_job_id,
    derive_legacy_revision_id,
    derive_legacy_unknown_job_id,
    derived_control_plane_id,
    new_control_plane_id,
    parse_legacy_resource_uri,
    validate_control_plane_id,
    workflow_revision_uri,
)


@pytest.mark.parametrize(
    ("kind", "prefix"),
    [
        ("workflow", "workflow_"),
        ("revision", "revision_"),
        ("deployment", "deployment_"),
        ("plan", "plan_"),
        ("job", "job_"),
        ("attempt", "attempt_"),
        ("asset", "asset_"),
        ("artifact", "artifact_"),
    ],
)
def test_new_control_plane_ids_are_typed_and_unique(kind: ControlPlaneKind, prefix: str) -> None:
    first = new_control_plane_id(kind)
    second = new_control_plane_id(kind)

    assert first.startswith(prefix)
    assert first != second
    assert validate_control_plane_id(kind, first) == first


def test_derived_ids_are_deterministic_and_domain_separated() -> None:
    first = derived_control_plane_id("job", "legacy-job-v1", ["local", "prompt-123"])
    repeated = derived_control_plane_id("job", "legacy-job-v1", ["local", "prompt-123"])
    other_namespace = derived_control_plane_id("job", "legacy-unknown-v1", ["local", "prompt-123"])
    other_kind = derived_control_plane_id("artifact", "legacy-job-v1", ["local", "prompt-123"])

    assert first == repeated
    assert first == "job_f51f030dc57a0bf7195884d44eb2a41541ea6acb44593c6de793293c1ca619d6"
    assert first != other_namespace
    assert first.removeprefix("job_") != other_kind.removeprefix("artifact_")
    assert len(first.removeprefix("job_")) == 64


def test_derived_id_preserves_component_types() -> None:
    numeric = derived_control_plane_id("artifact", "test-v1", [1])
    textual = derived_control_plane_id("artifact", "test-v1", ["1"])

    assert numeric != textual


def test_derived_id_encodes_unicode_components_as_utf8() -> None:
    derived = derived_control_plane_id("workflow", "legacy-workflow-v1", ["本地", "图生图"])

    assert derived == ("workflow_5c782c76f0cb1a6a7e159f6a5861a701c8191753d7233a69fa5e437721031fd1")


def test_legacy_object_derivation_helpers_freeze_component_order() -> None:
    job_id = derive_legacy_job_id("local", "prompt-123")

    assert job_id == "job_f51f030dc57a0bf7195884d44eb2a41541ea6acb44593c6de793293c1ca619d6"
    assert (
        derive_legacy_artifact_id(job_id, "9", "images", 0, "结果.png", "", "output")
        == "artifact_17ef1ac69b0be3e063709873218e770b054a18c6bc64d6c2f3932dd3bbb0b6f0"
    )
    assert derive_legacy_conflicting_workflow_id("local", "portrait") == (
        "workflow_e2c645b54149c1d364c37667025b3d53852bc9e447f17bca356b305890a208b9"
    )
    revision_id = derive_legacy_revision_id("portrait", "sha256:" + "0" * 64)
    unknown_job_id = derive_legacy_unknown_job_id("principal-1", "local", "idem-1", "f" * 64)

    assert revision_id == (
        "revision_308488fdca51d7156a31646fa5ed4f06f31977c935011b05544d119f6b22344f"
    )
    assert revision_id == derive_legacy_revision_id("portrait", "0" * 64)
    assert unknown_job_id == (
        "job_3c297b451031bf26f507992a1766e3d00a1b924f56edf4bdc2dec75ac4c3cb93"
    )
    assert unknown_job_id == derive_legacy_unknown_job_id(
        "principal-1", "local", "idem-1", "sha256:" + "f" * 64
    )


def test_legacy_object_derivation_helpers_reject_invalid_fields() -> None:
    job_id = "job_" + "a" * 64

    with pytest.raises(ValueError):
        derive_legacy_job_id("../server", "prompt")
    with pytest.raises(ValueError):
        derive_legacy_artifact_id("artifact_" + "a" * 64, "9", "images", 0, "x.png", "", "output")
    with pytest.raises(ValueError):
        derive_legacy_artifact_id(job_id, "9", "images", -1, "x.png", "", "output")
    with pytest.raises(ValueError):
        derive_legacy_artifact_id(job_id, "9", "images", True, "x.png", "", "output")
    with pytest.raises(ValueError):
        derive_legacy_artifact_id(job_id, "9", "images", 0, "x.png", "", "input")
    with pytest.raises(ValueError):
        derive_legacy_conflicting_workflow_id("../server", "portrait")
    with pytest.raises(ValueError):
        derive_legacy_revision_id("job_" + "a" * 64, "0" * 64)
    with pytest.raises(ValueError):
        derive_legacy_revision_id("portrait", "not-a-content-digest")
    with pytest.raises(ValueError):
        derive_legacy_unknown_job_id("owner", "../server", "key", "f" * 64)
    with pytest.raises(ValueError):
        derive_legacy_unknown_job_id("owner", "local", "", "f" * 64)


def test_derived_id_preserves_bool_null_integer_and_unicode_types() -> None:
    derived = derived_control_plane_id("plan", "contract-v1", [1, "1", True, False, None, "本地"])

    assert derived == "plan_124fbb0fb0cb5f68c1d90f0c38846be7a048632f00e35ade0e4861d30eb6c75f"


@pytest.mark.parametrize(
    "components",
    [
        ("value",),
        [1.5],
        [b"bytes"],
        [{}],
        [[]],
        [2**63],
        ["x" * 4097],
        ["x"] * 17,
        ["x" * 4096] * 5,
    ],
)
def test_derived_id_rejects_noncanonical_component_shapes(components: object) -> None:
    with pytest.raises(ValueError):
        derived_control_plane_id("job", "contract-v1", components)  # type: ignore[arg-type]


@pytest.mark.parametrize("namespace", ["legacy-job", "legacy-job-v0", "legacy-job-v01"])
def test_derived_id_requires_a_versioned_namespace(namespace: str) -> None:
    with pytest.raises(ValueError):
        derived_control_plane_id("job", namespace, ["local", "prompt-123"])


@pytest.mark.parametrize(
    ("kind", "identifier"),
    [
        ("job", "artifact_" + "a" * 64),
        ("artifact", "artifact_not-hex"),
        ("plan", "../plan_deadbeef"),
    ],
)
def test_validate_control_plane_id_rejects_wrong_or_unsafe_ids(
    kind: ControlPlaneKind, identifier: str
) -> None:
    with pytest.raises(ValueError):
        validate_control_plane_id(kind, identifier)


def test_validate_control_plane_id_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        validate_control_plane_id("unknown", "unknown_" + "a" * 32)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "identifier",
    [
        "revision_" + "a" * 32,
        "deployment_" + "a" * 32,
        "plan_" + "a" * 32,
        "job_" + "a" * 32,
        "attempt_" + "a" * 32,
        "asset_" + "a" * 32,
        "artifact_" + "a" * 32,
    ],
)
def test_workflow_ids_reject_other_control_plane_types(identifier: str) -> None:
    with pytest.raises(ValueError):
        validate_control_plane_id("workflow", identifier)


def test_workflow_ids_accept_existing_safe_slugs() -> None:
    assert validate_control_plane_id("workflow", "txt2img-v1") == "txt2img-v1"
    assert validate_control_plane_id("workflow", "job_daily") == "job_daily"
    assert validate_control_plane_id("workflow", "asset_pipeline") == "asset_pipeline"


@pytest.mark.parametrize(
    ("kind", "identifier", "expected"),
    [
        ("workflow", "portrait", "comfyui://workflows/portrait"),
        ("deployment", "deployment_" + "a" * 32, "comfyui://deployments/deployment_" + "a" * 32),
        ("plan", "plan_" + "a" * 32, "comfyui://plans/plan_" + "a" * 32),
        ("job", "job_" + "a" * 32, "comfyui://jobs/job_" + "a" * 32),
        ("asset", "asset_" + "a" * 32, "comfyui://assets/asset_" + "a" * 32),
        ("artifact", "artifact_" + "a" * 32, "comfyui://artifacts/artifact_" + "a" * 32),
    ],
)
def test_canonical_resource_uris_use_only_project_ids(
    kind: CanonicalResourceKind, identifier: str, expected: str
) -> None:
    assert canonical_resource_uri(kind, identifier) == expected


@pytest.mark.parametrize("kind", ["revision", "attempt", "unknown"])
def test_top_level_resource_uri_rejects_unsupported_kinds(kind: str) -> None:
    with pytest.raises(ValueError):
        canonical_resource_uri(kind, kind + "_" + "a" * 32)  # type: ignore[arg-type]


def test_workflow_revision_uri_binds_revision_to_workflow() -> None:
    revision_id = "revision_" + "a" * 64

    assert workflow_revision_uri("portrait", revision_id) == (
        f"comfyui://workflows/portrait/revisions/{revision_id}"
    )


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        (
            "comfyui://workflows/local/txt2img",
            LegacyResourceRef("workflow", "local", "txt2img"),
        ),
        (
            "comfyui://assets/local/asset_0123456789abcdef0123456789abcdef",
            LegacyResourceRef("asset", "local", "asset_0123456789abcdef0123456789abcdef"),
        ),
        (
            "comfyui://jobs/local/prompt-123",
            LegacyResourceRef("job", "local", "prompt-123"),
        ),
        (
            "comfyui://outputs/local/prompt-123/2",
            LegacyResourceRef("output", "local", "prompt-123", 2),
        ),
        (
            "comfyui://outputs/local/prompt-123/2147483647",
            LegacyResourceRef("output", "local", "prompt-123", 2_147_483_647),
        ),
    ],
)
def test_parse_legacy_resource_uri_returns_typed_alias(
    uri: str, expected: LegacyResourceRef
) -> None:
    assert parse_legacy_resource_uri(uri) == expected


def test_legacy_resource_ref_accepts_output_index_boundaries() -> None:
    assert LegacyResourceRef("output", "local", "prompt", 0).index == 0
    assert LegacyResourceRef("output", "local", "prompt", 2_147_483_647).index == 2_147_483_647


@pytest.mark.parametrize(
    "reference",
    [
        ("unknown", "local", "prompt", None),
        ("job", "../local", "prompt", None),
        ("job", "local", "prompt", 0),
        ("output", "local", "prompt", -1),
        ("output", "local", "prompt", None),
        ([], "local", "prompt", None),
        ("job", "", "prompt", None),
        ("output", "local", "prompt", True),
        ("output", "local", "prompt", 2_147_483_648),
    ],
)
def test_legacy_resource_ref_rejects_invalid_state(
    reference: tuple[object, object, object, object],
) -> None:
    with pytest.raises(ValueError):
        LegacyResourceRef(*reference)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "uri",
    [
        "comfyui://jobs/job_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "comfyui://jobs/local/../escaped",
        "comfyui://jobs/local/prompt%2Fescaped",
        "comfyui://outputs/local/prompt/not-an-index",
        "comfyui://outputs/local/prompt/-1",
        "https://example.com/jobs/local/prompt",
        "comfyui://jobs/local/prompt?secret=value",
        "comfyui://outputs/local/prompt/" + "9" * 10000,
        "comfyui://jobs/local/" + "a" * 2048,
        "comfyui://[invalid/jobs/local/prompt",
        " comfyui://jobs/local/prompt",
        "comfyui://jobs/local/prompt\r\n",
        "comfyui://outputs/local/prompt/²",
        "comfyui://outputs/local/prompt/00",
        "comfyui://jobs/%6cocal/prompt",
        "comfyui://jobs/local/prompt?",
        "comfyui://jobs/local/prompt#",
        "comfyui://jobs/local/prompt?#",
        "comfyui://jobs/" + "/".join(["a"] * 1100),
    ],
)
def test_parse_legacy_resource_uri_rejects_non_alias_or_unsafe_uri(uri: str) -> None:
    assert parse_legacy_resource_uri(uri) is None
