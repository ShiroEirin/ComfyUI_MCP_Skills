"""Compact tool surface: one action-parameterized front door per resource family."""

from __future__ import annotations

from comfyui_mcp_skills.adapters.mcp.server import (
    legacy_asset_alias_allowed,
    resolve_canonical_tool_name,
)
from comfyui_mcp_skills.adapters.mcp.tooling import (
    ASSET_ACTIONS,
    LEGACY_ASSET_TOOL_NAMES,
    Tool,
    fixed_tools,
    phase_l_tools,
)
from comfyui_mcp_skills.application.authorization import Scope, Toolset

# `comfyui.asset.upload` lives on the fixed surface, not in phase_l_tools().
PHASE_L_ASSET_NAMES = LEGACY_ASSET_TOOL_NAMES - {"comfyui.asset.upload"}


def _asset_upload_tool() -> Tool:
    return next(tool for tool in fixed_tools() if tool.name == "comfyui.asset.upload")


def test_default_surface_keeps_the_legacy_asset_tools() -> None:
    tools = phase_l_tools()

    assert len(tools) == len(PHASE_L_ASSET_NAMES)
    assert {tool.name for tool in tools} == set(PHASE_L_ASSET_NAMES)


def test_compact_mode_exposes_exactly_one_asset_front_door() -> None:
    tools = phase_l_tools(compact=True, extra=[_asset_upload_tool()])

    assert [tool.name for tool in tools] == ["comfyui.asset"]
    schema = tools[0].input_schema
    assert schema["required"] == ["action"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["action"]["enum"] == sorted(ASSET_ACTIONS)


def test_compact_front_door_unions_member_parameters() -> None:
    properties = phase_l_tools(compact=True, extra=[_asset_upload_tool()])[0].input_schema[
        "properties"
    ]

    for expected in ("server_id", "asset_id", "local_path", "limit", "collection", "cursor"):
        assert expected in properties


def test_every_action_maps_to_a_distinct_legacy_tool() -> None:
    values = list(ASSET_ACTIONS.values())

    assert len(values) == len(set(values))
    assert set(values) == set(LEGACY_ASSET_TOOL_NAMES)


def test_unlisted_compact_alias_keeps_its_own_name() -> None:
    """Regression: a hidden family member has no portable-name entry.

    Indexing the mapping directly raised KeyError, which the dispatch error
    boundary reported as INVALID_ARGUMENTS instead of running the tool.
    """
    mapping = {"comfyui.asset": "comfyui.asset"}

    assert resolve_canonical_tool_name("comfyui.asset.list", mapping) == "comfyui.asset.list"


def test_portable_name_still_maps_to_its_canonical_name() -> None:
    mapping = {"comfyui_run_local_x": "comfyui.run.local.x"}

    assert resolve_canonical_tool_name("comfyui_run_local_x", mapping) == "comfyui.run.local.x"


def _alias_allowed(
    name: str,
    *,
    compact: bool = True,
    toolset: Toolset = Toolset.EXECUTION,
    scopes: frozenset[Scope] = frozenset({Scope.EXECUTE}),
    asset_library_available: bool = True,
) -> bool:
    return legacy_asset_alias_allowed(
        name,
        compact=compact,
        toolset=toolset,
        scopes=scopes,
        asset_library_available=asset_library_available,
    )


def test_legacy_alias_is_allowed_for_an_authorized_execution_principal() -> None:
    assert _alias_allowed("comfyui.asset.list") is True


def test_legacy_alias_is_rejected_without_the_execute_scope() -> None:
    assert _alias_allowed("comfyui.asset.list", scopes=frozenset({Scope.OBSERVE})) is False


def test_legacy_alias_is_rejected_on_other_surfaces() -> None:
    """Hiding a tool must not become a permission bypass for another face."""
    assert (
        _alias_allowed(
            "comfyui.asset.list",
            toolset=Toolset.AUTHORING,
            scopes=frozenset({Scope.OBSERVE, Scope.AUTHOR}),
        )
        is False
    )
    assert (
        _alias_allowed(
            "comfyui.asset.list",
            toolset=Toolset.OPERATIONS,
            scopes=frozenset({Scope.OBSERVE, Scope.OPERATE}),
        )
        is False
    )


def test_legacy_alias_is_rejected_outside_compact_mode() -> None:
    assert _alias_allowed("comfyui.asset.list", compact=False) is False


def test_legacy_alias_is_rejected_for_a_non_family_name() -> None:
    assert _alias_allowed("comfyui.job.get") is False


def test_legacy_alias_requires_the_asset_backend() -> None:
    assert _alias_allowed("comfyui.asset.list", asset_library_available=False) is False


def test_asset_upload_alias_does_not_require_the_asset_library() -> None:
    assert _alias_allowed("comfyui.asset.upload", asset_library_available=False) is True
