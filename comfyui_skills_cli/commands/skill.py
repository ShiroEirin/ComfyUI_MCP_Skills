"""comfy-skills skill list / info"""

from __future__ import annotations

import typer
from comfyui_mcp_skills.application.catalog import WorkflowCatalog
from comfyui_mcp_skills.domain.errors import WorkflowNotFound
from comfyui_mcp_skills.infrastructure.persistence.workflows import FileWorkflowRepository

from ..config import get_base_dir, get_default_server_id, load_config
from ..output import output_error, output_result

app = typer.Typer()


@app.command("list")
def skill_list(ctx: typer.Context):
    """List all available skills."""
    base_dir = get_base_dir(ctx.obj.get("base_dir", ""))
    server_id = ctx.obj.get("server") or ""
    catalog = WorkflowCatalog(FileWorkflowRepository(base_dir))
    workflows = [
        workflow
        for workflow in catalog.list_enabled()
        if not server_id or workflow.server_id == server_id
    ]
    output_result(ctx, [
        {
            "workflow_id": workflow.workflow_id,
            "server_id": workflow.server_id,
            "description": workflow.description,
            "enabled": workflow.enabled,
            "param_count": len(workflow.parameters),
            "parameters": workflow.parameters,
        }
        for workflow in workflows
    ])


@app.command("info")
def skill_info(
    ctx: typer.Context,
    skill_id: str = typer.Argument(help="Skill ID in format: server_id/workflow_id"),
):
    """Show skill details including parameter schema."""
    base_dir = get_base_dir(ctx.obj.get("base_dir", ""))

    if "/" in skill_id:
        server_id, workflow_id = skill_id.split("/", 1)
    else:
        config = load_config(base_dir)
        server_id = ctx.obj.get("server") or get_default_server_id(config)
        workflow_id = skill_id

    catalog = WorkflowCatalog(FileWorkflowRepository(base_dir))
    try:
        workflow = catalog.get(server_id, workflow_id)
    except WorkflowNotFound:
        output_error(
            ctx,
            "SKILL_NOT_FOUND",
            f'Skill "{skill_id}" not found.',
            hint="Run `comfyui-skill list` to see available skills.",
        )
        return
    output_result(ctx, {
        "workflow_id": workflow.workflow_id,
        "server_id": workflow.server_id,
        "description": workflow.description,
        "enabled": workflow.enabled,
        "parameters": workflow.parameters,
    })
