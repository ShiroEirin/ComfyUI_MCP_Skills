"""Separate MCP server for dangerous workflow administration."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import anyio
from mcp.server import Server, ServerRequestContext
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)
from mcp_types import INVALID_PARAMS

from comfyui_mcp_skills import __version__
from comfyui_mcp_skills.adapters.mcp.tooling import decorate_tool
from comfyui_mcp_skills.application.admin import MAX_ADMIN_REQUEST_ID_LENGTH, WorkflowAdmin
from comfyui_mcp_skills.domain.errors import ComfyUISkillsError
from comfyui_mcp_skills.infrastructure.persistence.workflows import (
    FileWorkflowRepository,
)

logger = logging.getLogger(__name__)


def create_admin_server(
    base_dir: Path,
    *,
    enabled: bool = False,
    actor: str = "stdio-admin",
) -> Server[dict[str, object]]:
    if not enabled:
        raise PermissionError("Admin MCP requires an explicit enabled=True configuration")
    base_dir = base_dir.resolve()
    admin = WorkflowAdmin(
        base_dir,
        FileWorkflowRepository(base_dir),
        actor=actor,
    )

    async def list_tools(
        _ctx: ServerRequestContext[dict[str, object]],
        _params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        identity = {
            "server_id": {"type": "string", "minLength": 1},
            "workflow_id": {"type": "string", "minLength": 1},
        }
        request_id = {
            "request_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_ADMIN_REQUEST_ID_LENGTH,
                "description": "Stable caller-supplied idempotency and audit request ID.",
            }
        }
        return ListToolsResult(
            tools=[
                decorate_tool(tool)
                for tool in [
                    Tool(
                        name="comfyui.admin.workflow.set_enabled",
                        description="Enable or disable one configured workflow.",
                        input_schema={
                            "type": "object",
                            "properties": {
                                **identity,
                                **request_id,
                                "enabled": {"type": "boolean"},
                            },
                            "required": ["server_id", "workflow_id", "enabled"],
                            "additionalProperties": False,
                        },
                        output_schema={"type": "object"},
                        annotations=ToolAnnotations(
                            read_only_hint=False,
                            destructive_hint=False,
                            idempotent_hint=True,
                            open_world_hint=False,
                        ),
                    ),
                    Tool(
                        name="comfyui.admin.workflow.delete",
                        description=(
                            "Permanently delete one workflow after an exact confirmation phrase. "
                            "Supply request_id to make retries idempotent."
                        ),
                        input_schema={
                            "type": "object",
                            "properties": {
                                **identity,
                                **request_id,
                                "confirmation": {"type": "string"},
                            },
                            "required": ["server_id", "workflow_id", "confirmation", "request_id"],
                            "additionalProperties": False,
                        },
                        output_schema={"type": "object"},
                        annotations=ToolAnnotations(
                            read_only_hint=False,
                            destructive_hint=True,
                            idempotent_hint=True,
                            open_world_hint=False,
                        ),
                    ),
                    Tool(
                        name="comfyui.admin.audit.get",
                        description="Read the durable commit and audit status of an admin request.",
                        input_schema={
                            "type": "object",
                            "properties": request_id,
                            "required": ["request_id"],
                            "additionalProperties": False,
                        },
                        output_schema={"type": "object"},
                        annotations=ToolAnnotations(
                            read_only_hint=True,
                            destructive_hint=False,
                            idempotent_hint=True,
                            open_world_hint=False,
                        ),
                    ),
                    Tool(
                        name="comfyui.admin.audit.retry",
                        description=(
                            "Retry only a pending audit outcome without repeating its operation."
                        ),
                        input_schema={
                            "type": "object",
                            "properties": request_id,
                            "required": ["request_id"],
                            "additionalProperties": False,
                        },
                        output_schema={"type": "object"},
                        annotations=ToolAnnotations(
                            read_only_hint=False,
                            destructive_hint=False,
                            idempotent_hint=True,
                            open_world_hint=False,
                        ),
                    ),
                ]
            ],
            cache_scope="private",
        )

    async def call_tool(
        ctx: ServerRequestContext[dict[str, object]], params: CallToolRequestParams
    ) -> CallToolResult:
        arguments = dict(params.arguments or {})
        context_request_id = "" if ctx.request_id is None else str(ctx.request_id)
        try:
            if params.name == "comfyui.admin.workflow.set_enabled":
                _validate_keys(arguments, {"server_id", "workflow_id", "enabled", "request_id"})
                server_id = _required_string(arguments, "server_id")
                workflow_id = _required_string(arguments, "workflow_id")
                enabled_value = arguments.get("enabled")
                if not isinstance(enabled_value, bool):
                    raise TypeError("enabled must be a boolean")
                request_id = _optional_request_id(arguments, context_request_id)
                result = await anyio.to_thread.run_sync(
                    lambda: admin.set_enabled(
                        server_id,
                        workflow_id,
                        enabled_value,
                        request_id=request_id,
                    )
                )
            elif params.name == "comfyui.admin.workflow.delete":
                _validate_keys(
                    arguments,
                    {"server_id", "workflow_id", "confirmation", "request_id"},
                )
                server_id = _required_string(arguments, "server_id")
                workflow_id = _required_string(arguments, "workflow_id")
                confirmation = _required_string(arguments, "confirmation")
                request_id = _required_string(arguments, "request_id")
                result = await anyio.to_thread.run_sync(
                    lambda: admin.delete(
                        server_id,
                        workflow_id,
                        confirmation,
                        request_id=request_id,
                    )
                )
            elif params.name == "comfyui.admin.audit.get":
                _validate_keys(arguments, {"request_id"})
                request_id = _required_string(arguments, "request_id")
                result = await anyio.to_thread.run_sync(lambda: admin.get_audit_status(request_id))
            elif params.name == "comfyui.admin.audit.retry":
                _validate_keys(arguments, {"request_id"})
                request_id = _required_string(arguments, "request_id")
                result = await anyio.to_thread.run_sync(lambda: admin.retry_audit(request_id))
            else:
                raise MCPError(
                    code=INVALID_PARAMS,
                    message=f"Unknown tool: {params.name}",
                )
            return _result(result)
        except MCPError:
            raise
        except (ComfyUISkillsError, KeyError, TypeError, ValueError) as exc:
            error = (
                exc.as_dict()
                if isinstance(exc, ComfyUISkillsError)
                else {"code": "INVALID_ARGUMENTS", "message": str(exc)}
            )
            return _result(error, error=True)
        except Exception:
            logger.exception("Unexpected admin MCP tool failure", extra={"tool": params.name})
            return _result(
                {
                    "code": "INTERNAL_ERROR",
                    "message": "Unexpected server error",
                    "retryable": False,
                    "details": {},
                },
                error=True,
            )

    return Server(
        "ComfyUI MCP Skills Admin",
        version=__version__,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def _required_string(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value:
        raise TypeError(f"{name} must be a non-empty string")
    if name == "request_id" and len(value) > MAX_ADMIN_REQUEST_ID_LENGTH:
        raise TypeError(f"request_id must be at most {MAX_ADMIN_REQUEST_ID_LENGTH} characters")
    return value


def _optional_request_id(arguments: dict[str, Any], fallback: str) -> str:
    if "request_id" not in arguments:
        return fallback
    return _required_string(arguments, "request_id")


def _validate_keys(arguments: dict[str, Any], allowed: set[str]) -> None:
    unexpected = set(arguments) - allowed
    if unexpected:
        raise ValueError(f"Unexpected arguments: {', '.join(sorted(unexpected))}")


def _result(value: dict[str, Any], *, error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
        structured_content=value,
        is_error=error,
    )
