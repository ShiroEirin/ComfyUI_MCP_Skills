"""Capability metadata must never drift from the authorization table.

``authorization._TOOL_SCOPES`` is the single source of truth for which scope a
Tool needs. The catalog (``capabilities.CAPABILITY_SPECS``) carries the same
fact for search/describe, so any edit that narrows a Tool scope without
narrowing its capability (or the other way round) would advertise a capability
to a principal that cannot actually call it.
"""

from __future__ import annotations

import pytest

from comfyui_mcp_skills.application.authorization import (
    _TOOL_SCOPES,
    AuthorizationContext,
    Scope,
    Toolset,
    admitted_scopes,
    tool_visible,
)
from comfyui_mcp_skills.application.capabilities import (
    CAPABILITY_BY_NAME,
    CAPABILITY_SPECS,
    CapabilityCatalog,
)


def test_every_capability_scope_matches_the_authorization_table() -> None:
    mismatched = {
        spec.name: {
            "capability": sorted(scope.value for scope in spec.required_scopes),
            "authorization": sorted(scope.value for scope in _TOOL_SCOPES[spec.name]),
        }
        for spec in CAPABILITY_SPECS
        if spec.name in _TOOL_SCOPES and _TOOL_SCOPES[spec.name] != spec.required_scopes
    }

    assert mismatched == {}


def test_every_capability_is_registered_for_authorization() -> None:
    """An unregistered capability would be advertised but never callable."""
    unregistered = sorted(spec.name for spec in CAPABILITY_SPECS if spec.name not in _TOOL_SCOPES)

    assert unregistered == []


def test_authorization_table_has_no_capability_orphans() -> None:
    orphaned = sorted(name for name in _TOOL_SCOPES if name not in CAPABILITY_BY_NAME)

    assert orphaned == []


def test_catalog_never_advertises_an_unauthorized_capability() -> None:
    """Catalog visibility implies ``tool_visible``; the reverse may be narrower."""
    catalog = CapabilityCatalog.default()
    granted_sets = [frozenset({scope}) for scope in Scope] + [
        admitted_scopes(toolset) for toolset in Toolset
    ]

    for toolset in Toolset:
        for granted in granted_sets:
            authorization = AuthorizationContext("principal", granted, toolset)
            for name in catalog.visible_names(authorization):
                assert tool_visible(name, toolset, granted), (
                    f"{name} is advertised to toolset={toolset.value} "
                    f"granted={sorted(scope.value for scope in granted)}"
                )


@pytest.mark.parametrize(
    "name",
    (
        "comfyui.workflow.visualize",
        "comfyui.revision.list",
        "comfyui.revision.diff",
        "comfyui.workflow.describe",
        "comfyui.workflow.dependencies.check",
    ),
)
def test_authoring_only_capabilities_stay_off_observe_surfaces(name: str) -> None:
    """Regression for the metadata drift: these are AUTHOR-only, not OBSERVE."""
    catalog = CapabilityCatalog.default()
    authoring_only = AuthorizationContext(
        "author", frozenset({Scope.AUTHOR}), Toolset.AUTHORING
    )
    observe_only = AuthorizationContext(
        "observer", frozenset({Scope.OBSERVE}), Toolset.AUTHORING
    )

    assert CAPABILITY_BY_NAME[name].required_scopes == frozenset({Scope.AUTHOR})
    assert name in catalog.visible_names(authoring_only)
    assert name not in catalog.visible_names(observe_only)
    assert not tool_visible(
        name, Toolset.OPERATIONS, admitted_scopes(Toolset.OPERATIONS)
    )
