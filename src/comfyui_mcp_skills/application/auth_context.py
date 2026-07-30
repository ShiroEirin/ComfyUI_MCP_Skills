"""Request-local principal and scope context shared by MCP adapters."""

from __future__ import annotations

from contextvars import ContextVar, Token

from comfyui_mcp_skills.application.authorization import AuthorizationContext

_CURRENT_AUTHORIZATION: ContextVar[AuthorizationContext | None] = ContextVar(
    "comfyui_mcp_authorization", default=None
)


def current_authorization() -> AuthorizationContext | None:
    return _CURRENT_AUTHORIZATION.get()


def set_authorization(context: AuthorizationContext) -> Token[AuthorizationContext | None]:
    return _CURRENT_AUTHORIZATION.set(context)


def reset_authorization(token: Token[AuthorizationContext | None]) -> None:
    _CURRENT_AUTHORIZATION.reset(token)
