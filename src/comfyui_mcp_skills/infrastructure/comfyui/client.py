"""Stable public facade for the responsibility-focused ComfyUI clients."""

from __future__ import annotations

# Retain these module attributes for callers that patch the historical transport
# dependencies through ``comfyui.client``.
from comfyui_mcp_skills.infrastructure.comfyui.capabilities import CapabilitiesClient
from comfyui_mcp_skills.infrastructure.comfyui.core_client import CoreClient
from comfyui_mcp_skills.infrastructure.comfyui.jobs_client import JobsClient
from comfyui_mcp_skills.infrastructure.comfyui.manager_client import ManagerClient
from comfyui_mcp_skills.infrastructure.comfyui.userdata_client import UserdataClient


class ComfyUIClient(
    CoreClient,
    JobsClient,
    UserdataClient,
    ManagerClient,
    CapabilitiesClient,
):
    """Expose the established ComfyUI client API from a single facade."""


__all__ = ["ComfyUIClient"]
