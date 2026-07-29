"""comfyui-skill cancel — cancel a running or queued job."""

from __future__ import annotations

import typer

from ..client import ComfyUIClient
from ..config import get_base_dir, get_default_server_id, get_server, load_config
from ..output import output_error, output_result


def cancel_cmd(
    ctx: typer.Context,
    prompt_id: str = typer.Argument(help="Prompt ID to cancel"),
):
    """Remove a queued job; refuse unsafe global interruption of running jobs."""
    base_dir = get_base_dir(ctx.obj.get("base_dir", ""))
    config = load_config(base_dir)
    server_id = ctx.obj.get("server") or get_default_server_id(config)
    server_config = get_server(config, server_id)

    if not server_config:
        output_error(ctx, "SERVER_NOT_FOUND", f'Server "{server_id}" not found.')
        return
    if server_config.get("enabled", True) is not True:
        output_error(ctx, "SERVER_DISABLED", f'Server "{server_id}" is disabled.')
        return

    client = ComfyUIClient(
        server_url=server_config.get("url", "http://127.0.0.1:8188"),
        auth=server_config.get("auth", ""),
    )

    try:
        queue = client.get_queue()

        # Standard ComfyUI /interrupt is global, not prompt-targeted.
        for item in queue.get("queue_running", []):
            if len(item) > 1 and item[1] == prompt_id:
                output_error(
                    ctx,
                    "UNSAFE_CANCEL",
                    "Running-job cancellation is unavailable because this ComfyUI server only exposes global interrupt.",
                )
                return

        # Check if pending
        for item in queue.get("queue_pending", []):
            if len(item) > 1 and item[1] == prompt_id:
                client.queue_delete([prompt_id])
                output_result(ctx, {"status": "removed", "prompt_id": prompt_id})
                return

        output_result(ctx, {"status": "not_found", "prompt_id": prompt_id})

    except typer.Exit:
        raise
    except Exception as exc:
        output_error(ctx, "CANCEL_FAILED", f"Failed to cancel job: {exc}")
        return
