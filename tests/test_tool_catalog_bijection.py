"""The fixed tool surface and the capability catalog must stay in sync.

The catalog drives search/describe while the fixed surface drives list/call.
A tool without a spec is invisible to discovery but still callable; a spec
without a tool is advertised but not callable. This test checks both
directions and includes the Phase P tools, which are filtered out by default.
"""

from __future__ import annotations

from comfyui_mcp_skills.adapters.mcp.tooling import (
    fixed_tools,
    phase_h_tools,
    phase_k_tools,
    phase_l_tools,
    phase_m_tools,
    phase_n_tools,
)
from comfyui_mcp_skills.application.capabilities import (
    CAPABILITY_BY_NAME,
    CAPABILITY_SPECS,
)


def _fixed_names() -> set[str]:
    return {
        tool.name
        for tool in (
            *fixed_tools(),
            # The real surface enables Phase P; the default factory call
            # hides those seven tools, so this test must opt in explicitly.
            *phase_h_tools(include_phase_p=True),
            *phase_k_tools(),
            *phase_l_tools(),
            *phase_m_tools(),
            *phase_n_tools(),
        )
    }


def test_every_fixed_tool_has_a_capability_spec() -> None:
    missing = sorted(name for name in _fixed_names() if name not in CAPABILITY_BY_NAME)
    assert missing == []


def test_every_catalogued_fixed_capability_has_a_tool() -> None:
    names = _fixed_names()
    orphaned = sorted(
        spec.name
        for spec in CAPABILITY_SPECS
        if spec.name not in names and not spec.name.startswith("comfyui.run.")
    )
    # Admin-only capabilities live in the admin surface, not in the fixed
    # phase factories; exclude them from this fixed-surface check.
    assert [name for name in orphaned if not name.startswith("comfyui.admin.")] == []
