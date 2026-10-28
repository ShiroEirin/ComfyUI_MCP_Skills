"""Every advertised fixed tool must exist in the capability catalog.

The catalog drives search/describe while the fixed surface drives list/call.
A tool without a spec is invisible to discovery but still callable; a spec
without a tool is advertised but not callable. This test catches the first
direction, which is the one that silently grows.
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
from comfyui_mcp_skills.application.capabilities import CAPABILITY_BY_NAME


def test_every_fixed_tool_has_a_capability_spec() -> None:
    names = {
        tool.name
        for factory in (
            fixed_tools,
            phase_h_tools,
            phase_k_tools,
            phase_l_tools,
            phase_m_tools,
            phase_n_tools,
        )
        for tool in factory()
    }

    missing = sorted(name for name in names if name not in CAPABILITY_BY_NAME)
    assert missing == []
