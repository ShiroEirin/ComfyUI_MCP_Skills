"""comfy-skills run / submit / status"""

from __future__ import annotations

import copy
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

import typer
import requests
from comfyui_mcp_skills.domain.errors import WorkflowArgumentsError
from comfyui_mcp_skills.domain.workflow_schema import normalize_parameters, validate_arguments

from comfyui_mcp_skills.infrastructure.comfyui.client import ComfyUIClient
from ..config import get_base_dir, get_default_server_id, get_server, load_config
from ..history_writer import (
    claim_job,
    find_existing_run,
    release_job_claim,
    renew_job_claim,
    save_run_record,
)
from ..output import OutputFormat, get_output_format, is_machine_mode, output_error, output_event, output_result
from ..error_hints import match_error_hint
from ..storage import get_schema, get_workflow_data

_POLL_INITIAL = 1.0
_POLL_MAX = 10.0
_POLL_FACTOR = 1.5


class _RunContext:
    """Execution metadata and fencing token for durable history updates."""

    __slots__ = (
        "base_dir",
        "server_id",
        "workflow_id",
        "prompt_id",
        "args",
        "job_id",
        "lease_token",
        "client_id",
        "_t0",
    )

    def __init__(
        self,
        base_dir: Path,
        server_id: str,
        workflow_id: str,
        prompt_id: str,
        args: dict[str, Any],
        job_id: str = "",
        lease_token: str = "",
        client_id: str = "",
    ) -> None:
        self.base_dir = base_dir
        self.server_id = server_id
        self.workflow_id = workflow_id
        self.prompt_id = prompt_id
        self.args = args
        self.job_id = job_id
        self.lease_token = lease_token
        self.client_id = client_id
        self._t0 = 0.0

    def start(self) -> None:
        self._t0 = time.time()

    def save(
        self, status: str, outputs: list | None = None, error: str = ""
    ) -> None:
        duration_ms = int((time.time() - self._t0) * 1000) if self._t0 else 0
        save_run_record(
            self.base_dir,
            self.server_id,
            self.workflow_id,
            self.prompt_id,
            self.args,
            status,
            job_id=self.job_id,
            lease_token=self.lease_token,
            client_id=self.client_id,
            duration_ms=duration_ms,
            outputs=outputs,
            error=error,
        )


def _ws_available() -> bool:
    try:
        import websocket  # noqa: F401
        return True
    except ImportError:
        return False


def run_cmd(
    ctx: typer.Context,
    skill_id: str = typer.Argument(help="Skill ID: server_id/workflow_id or workflow_id"),
    args: str = typer.Option("{}", "--args", "-a", help="JSON parameters"),
    only: str = typer.Option("", "--only", help="Comma-separated node IDs for partial execution (only run subgraph needed for these nodes)"),
    priority: float = typer.Option(0, "--priority", "-p", help="Queue priority (lower runs first, negative to jump queue)"),
    validate: bool = typer.Option(False, "--validate", help="Validate workflow without executing (checks node errors, then cancels)"),
    job_id: str = typer.Option("", "--job-id", help="Idempotency key — if this job was already executed, return cached result"),
    wait_timeout_seconds: float = typer.Option(3600.0, "--wait-timeout", min=0.0, help="Maximum seconds to wait; zero waits indefinitely"),
):
    """Execute a skill (blocking — waits for completion)."""
    base_dir, server_id, workflow_id = _resolve_skill(ctx, skill_id)

    client, schema_data, workflow_data, server_config = _prepare(ctx, base_dir, server_id, workflow_id)

    input_args = _parse_args(ctx, args)
    parameters = _get_parameters(schema_data)
    try:
        validate_arguments(parameters, input_args)
    except WorkflowArgumentsError as exc:
        output_error(ctx, exc.code, exc.message)
        return
    request_args = copy.deepcopy(input_args)
    client_id = str(uuid.uuid4())
    claim_token = ""
    if job_id:
        try:
            claimed = claim_job(
                base_dir,
                server_id,
                workflow_id,
                job_id,
                request_args,
                client_id,
            )
        except ValueError as exc:
            output_error(ctx, "IDEMPOTENCY_CONFLICT", str(exc))
            return
        if not claimed:
            existing = find_existing_run(base_dir, server_id, workflow_id, job_id)
            reconciled = _reconcile_cli_submission(
                client, base_dir, server_id, workflow_id, job_id, existing
            )
            output_result(ctx, reconciled or {"job_id": job_id, "status": "reserved"})
            return
        claim_token = str(claimed)
    try:
        _upload_media(ctx, client, parameters, input_args)
    except BaseException:
        if job_id:
            release_job_claim(
                base_dir, server_id, workflow_id, job_id, claim_token
            )
        raise
    if job_id:
        renew_job_claim(
            base_dir, server_id, workflow_id, job_id, claim_token
        )
    workflow = _inject_params(workflow_data, parameters, input_args)

    targets = [t.strip() for t in only.split(",") if t.strip()] if only else None

    try:
        result = client.queue_prompt(workflow, client_id=client_id, targets=targets, priority=priority if priority != 0 else None)
    except Exception as exc:
        outcome_unknown = _submission_outcome_unknown(exc)
        if job_id and outcome_unknown:
            save_run_record(
                base_dir,
                server_id,
                workflow_id,
                "",
                request_args,
                "submission_unknown",
                job_id=job_id,
                lease_token=claim_token,
                client_id=client_id,
            )
        if job_id and not outcome_unknown:
            release_job_claim(
                base_dir, server_id, workflow_id, job_id, claim_token
            )
        code = "SUBMISSION_UNKNOWN" if outcome_unknown else "SUBMIT_FAILED"
        output_error(ctx, code, f"Failed to submit workflow: {exc}")
        return

    prompt_id = result.get("prompt_id", "")
    if not prompt_id:
        if job_id:
            save_run_record(
                base_dir,
                server_id,
                workflow_id,
                "",
                request_args,
                "submission_unknown",
                job_id=job_id,
                lease_token=claim_token,
                client_id=client_id,
            )
        output_error(ctx, "SUBMISSION_UNKNOWN", "ComfyUI returned no prompt_id")
        return
    node_errors = result.get("node_errors", {})

    if validate:
        if prompt_id:
            try:
                client.interrupt(prompt_id)
                client.queue_delete([prompt_id])
            except Exception:
                pass
        if node_errors:
            output_result(ctx, {"status": "invalid", "prompt_id": prompt_id, "node_errors": node_errors})
        else:
            output_result(ctx, {"status": "valid", "prompt_id": prompt_id, "node_count": len(workflow)})
        if job_id:
            release_job_claim(
                base_dir, server_id, workflow_id, job_id, claim_token
            )
        return

    if node_errors:
        output_event(ctx, "warning", prompt_id=prompt_id, message="Workflow has node errors", node_errors=node_errors)

    fmt = get_output_format(ctx)
    output_event(ctx, "queued", prompt_id=prompt_id)

    if fmt == OutputFormat.TEXT:
        from rich.console import Console
        console = Console(stderr=True)
        console.print(f"[dim]Queued: {prompt_id}[/dim]")

    # Track execution for history
    run_ctx = _RunContext(
        base_dir,
        server_id,
        workflow_id,
        prompt_id,
        request_args,
        job_id,
        claim_token,
        client_id,
    )
    run_ctx.start()
    run_ctx.save("submitted")
    deadline = (
        time.monotonic() + wait_timeout_seconds
        if wait_timeout_seconds > 0
        else None
    )

    if _ws_available():
        try:
            _run_with_ws(
                ctx, client, workflow, prompt_id, client_id, base_dir,
                server_config, fmt, run_ctx, deadline=deadline,
            )
            return
        except typer.Exit:
            raise
        except Exception:
            logger.debug("WebSocket failed, falling back to polling", exc_info=True)

    _run_with_poll(
        ctx, client, prompt_id, base_dir, server_config, fmt,
        run_ctx, deadline=deadline,
    )


def _run_with_ws(
    ctx: typer.Context,
    client: ComfyUIClient,
    workflow: dict[str, Any],
    prompt_id: str,
    client_id: str,
    base_dir: Any,
    server_config: dict[str, Any],
    fmt: OutputFormat,
    run_ctx: _RunContext | None = None,
    deadline: float | None = None,
) -> None:
    if fmt == OutputFormat.TEXT:
        from rich.console import Console
        console = Console(stderr=True)

    node_classes: dict[str, str] = {}
    for node_id, node_data in workflow.items():
        if isinstance(node_data, dict) and "class_type" in node_data:
            node_classes[str(node_id)] = node_data["class_type"]

    remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
    for event in client.ws_events(client_id, prompt_id, remaining):
        if deadline is not None and time.monotonic() >= deadline:
            output_event(ctx, "submitted", prompt_id=prompt_id, reason="wait_timeout")
            if fmt != OutputFormat.STREAM_JSON:
                output_result(ctx, {
                    "status": "submitted",
                    "prompt_id": prompt_id,
                    "reason": "wait_timeout",
                })
            return
        etype = event["type"]
        data = event["data"]

        if etype == "execution_start":
            output_event(ctx, "started", prompt_id=prompt_id)
            if fmt == OutputFormat.TEXT:
                console.print("[yellow]Running...[/yellow]")

        elif etype == "execution_cached":
            nodes = data.get("nodes", [])
            output_event(ctx, "cached", prompt_id=prompt_id, nodes=nodes)

        elif etype == "executing":
            node = data.get("node")
            if node is None:
                break
            display = node_classes.get(node, node)
            output_event(ctx, "node_executing", prompt_id=prompt_id, node=node, node_display=display)
            if fmt == OutputFormat.TEXT:
                console.print(f"  [cyan]{display}[/cyan] ({node})")

        elif etype == "executed":
            node = data.get("node", "")
            display = node_classes.get(node, node)
            output_event(ctx, "node_completed", prompt_id=prompt_id, node=node, node_display=display, outputs=data.get("output", {}))

        elif etype == "progress":
            node = data.get("node", "")
            value = data.get("value", 0)
            max_val = data.get("max", 0)
            display = node_classes.get(node, node)
            output_event(ctx, "progress", prompt_id=prompt_id, node=node, node_display=display, value=value, max=max_val)
            if fmt == OutputFormat.TEXT and max_val > 0:
                pct = int(value / max_val * 100)
                console.print(f"    [dim]{display} {value}/{max_val} ({pct}%)[/dim]", highlight=False)

        elif etype == "execution_error":
            error_msg = data.get("exception_message", "Execution error")
            hint = match_error_hint(error_msg)
            output_event(ctx, "error", prompt_id=prompt_id, message=error_msg)
            if run_ctx:
                run_ctx.save("error", error=error_msg)
            output_error(ctx, "EXECUTION_FAILED", error_msg, hint=hint)
            return

        elif etype == "execution_interrupted":
            if run_ctx:
                run_ctx.save("interrupted")
            output_error(ctx, "EXECUTION_INTERRUPTED", "Execution was interrupted")
            return

    _run_with_poll(
        ctx,
        client,
        prompt_id,
        base_dir,
        server_config,
        fmt,
        run_ctx,
        deadline,
    )


def _run_with_poll(
    ctx: typer.Context,
    client: ComfyUIClient,
    prompt_id: str,
    base_dir: Any,
    server_config: dict[str, Any],
    fmt: OutputFormat,
    run_ctx: _RunContext | None = None,
    deadline: float | None = None,
) -> None:
    if fmt == OutputFormat.TEXT:
        from rich.console import Console
        console = Console(stderr=True)

    poll_interval = _POLL_INITIAL
    prev_status = ""
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            output_event(ctx, "submitted", prompt_id=prompt_id, reason="wait_timeout")
            if fmt != OutputFormat.STREAM_JSON:
                output_result(ctx, {
                    "status": "submitted",
                    "prompt_id": prompt_id,
                    "reason": "wait_timeout",
                })
            return
        history = client.get_history(prompt_id)
        if history:
            state, outputs, error_msg = classify_history(history)
            if state in {"error", "interrupted", "cancelled"}:
                hint = match_error_hint(error_msg)
                output_event(ctx, "error", prompt_id=prompt_id, message=error_msg)
                if run_ctx:
                    run_ctx.save(state, error=error_msg)
                output_error(ctx, "EXECUTION_FAILED", error_msg, hint=hint)
                return

            if state == "completed":
                collected = _collect_outputs(outputs)
                collected = _download_outputs(client, collected, base_dir, server_config)
                output_event(ctx, "completed", prompt_id=prompt_id, outputs=collected)
                if run_ctx:
                    run_ctx.save("success", outputs=collected)
                if fmt == OutputFormat.STREAM_JSON:
                    return
                output_result(ctx, {
                    "status": "success",
                    "prompt_id": prompt_id,
                    "outputs": collected,
                })
                return

        queue = client.get_queue()
        current_status = ""
        for item in queue.get("queue_running", []):
            if len(item) > 1 and item[1] == prompt_id:
                current_status = "running"
                break
        if not current_status:
            for i, item in enumerate(queue.get("queue_pending", [])):
                if len(item) > 1 and item[1] == prompt_id:
                    current_status = f"queued:{i}"
                    break

        if current_status and current_status != prev_status:
            if current_status == "running":
                output_event(ctx, "running", prompt_id=prompt_id)
                if fmt == OutputFormat.TEXT:
                    console.print("[yellow]Running...[/yellow]")
            elif current_status.startswith("queued:"):
                pos = current_status.split(":")[1]
                output_event(ctx, "queued", prompt_id=prompt_id, position=int(pos))
            prev_status = current_status

        time.sleep(poll_interval)
        poll_interval = min(poll_interval * _POLL_FACTOR, _POLL_MAX)


def submit_cmd(
    ctx: typer.Context,
    skill_id: str = typer.Argument(help="Skill ID: server_id/workflow_id or workflow_id"),
    args: str = typer.Option("{}", "--args", "-a", help="JSON parameters"),
    only: str = typer.Option("", "--only", help="Comma-separated node IDs for partial execution"),
    priority: float = typer.Option(0, "--priority", "-p", help="Queue priority"),
    job_id: str = typer.Option("", "--job-id", help="Idempotency key"),
):
    """Submit a skill for execution and return immediately."""
    base_dir, server_id, workflow_id = _resolve_skill(ctx, skill_id)
    client, schema_data, workflow_data, _server_config = _prepare(
        ctx, base_dir, server_id, workflow_id
    )
    input_args = _parse_args(ctx, args)
    parameters = _get_parameters(schema_data)
    try:
        validate_arguments(parameters, input_args)
    except WorkflowArgumentsError as exc:
        output_error(ctx, exc.code, exc.message)
        return
    request_args = copy.deepcopy(input_args)
    client_id = str(uuid.uuid4())
    claim_token = ""
    if job_id:
        try:
            claimed = claim_job(
                base_dir,
                server_id,
                workflow_id,
                job_id,
                request_args,
                client_id,
            )
        except ValueError as exc:
            output_error(ctx, "IDEMPOTENCY_CONFLICT", str(exc))
            return
        if not claimed:
            existing = find_existing_run(base_dir, server_id, workflow_id, job_id)
            reconciled = _reconcile_cli_submission(
                client, base_dir, server_id, workflow_id, job_id, existing
            )
            output_result(
                ctx, reconciled or {"job_id": job_id, "status": "reserved"}
            )
            return
        claim_token = str(claimed)
    try:
        _upload_media(ctx, client, parameters, input_args)
    except BaseException:
        if job_id:
            release_job_claim(
                base_dir, server_id, workflow_id, job_id, claim_token
            )
        raise
    if job_id:
        renew_job_claim(
            base_dir, server_id, workflow_id, job_id, claim_token
        )
    workflow = _inject_params(workflow_data, parameters, input_args)
    targets = [item.strip() for item in only.split(",") if item.strip()] or None
    try:
        result = client.queue_prompt(
            workflow,
            client_id=client_id,
            targets=targets,
            priority=priority if priority != 0 else None,
        )
    except Exception as exc:
        outcome_unknown = _submission_outcome_unknown(exc)
        if job_id and outcome_unknown:
            save_run_record(
                base_dir,
                server_id,
                workflow_id,
                "",
                request_args,
                "submission_unknown",
                job_id=job_id,
                lease_token=claim_token,
                client_id=client_id,
            )
        if job_id and not outcome_unknown:
            release_job_claim(
                base_dir, server_id, workflow_id, job_id, claim_token
            )
        code = "SUBMISSION_UNKNOWN" if outcome_unknown else "SUBMIT_FAILED"
        output_error(ctx, code, f"Failed to submit workflow: {exc}")
        return
    prompt_id = str(result.get("prompt_id", ""))
    if not prompt_id:
        if job_id:
            save_run_record(
                base_dir,
                server_id,
                workflow_id,
                "",
                request_args,
                "submission_unknown",
                job_id=job_id,
                lease_token=claim_token,
                client_id=client_id,
            )
        output_error(ctx, "SUBMISSION_UNKNOWN", "ComfyUI returned no prompt_id")
        return
    save_run_record(
        base_dir,
        server_id,
        workflow_id,
        prompt_id,
        request_args,
        "submitted",
        job_id=job_id,
        lease_token=claim_token,
        client_id=client_id,
    )
    output_result(ctx, {"status": "submitted", "prompt_id": prompt_id})


def status_cmd(
    ctx: typer.Context,
    prompt_id: str = typer.Argument(help="Prompt ID from submit"),
):
    """Check execution status of a submitted workflow."""
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
    client = _build_client(server_config)
    history = client.get_history(prompt_id)
    if history:
        state, outputs, error_msg = classify_history(history)
        if state in {"error", "interrupted", "cancelled"}:
            hint = match_error_hint(error_msg)
            result: dict[str, Any] = {
                "status": state,
                "prompt_id": prompt_id,
                "error": error_msg,
            }
            if hint:
                result["hint"] = hint
            output_result(ctx, result)
            return
        if state == "completed":
            collected = _collect_outputs(outputs)
            collected = _download_outputs(
                client, collected, base_dir, server_config
            )
            output_result(
                ctx,
                {"status": "success", "prompt_id": prompt_id, "outputs": collected},
            )
            return
    queue = client.get_queue()
    for item in queue.get("queue_running", []):
        if len(item) > 1 and item[1] == prompt_id:
            output_result(ctx, {"status": "running", "prompt_id": prompt_id})
            return
    for index, item in enumerate(queue.get("queue_pending", [])):
        if len(item) > 1 and item[1] == prompt_id:
            output_result(
                ctx,
                {"status": "queued", "prompt_id": prompt_id, "position": index},
            )
            return
    output_result(ctx, {"status": "not_found", "prompt_id": prompt_id})


def _submission_outcome_unknown(exc: Exception) -> bool:
    if not isinstance(exc, requests.RequestException):
        return False
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return not 400 <= exc.response.status_code < 500
    return True


def _reconcile_cli_submission(
    client: ComfyUIClient,
    base_dir: Path,
    server_id: str,
    workflow_id: str,
    job_id: str,
    existing: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not existing or existing.get("prompt_id"):
        return existing
    client_id = str(existing.get("client_id", ""))
    if not client_id:
        return existing
    try:
        prompt_id = _prompt_for_client(client.get_queue(), client_id)
        if not prompt_id:
            for candidate, history in client.get_history_list(max_items=100).items():
                if _contains_client_id(history, client_id):
                    prompt_id = str(candidate)
                    break
    except requests.RequestException:
        return existing
    if not prompt_id:
        return existing
    save_run_record(
        base_dir,
        server_id,
        workflow_id,
        prompt_id,
        dict(existing.get("args", {})),
        "submitted",
        job_id=job_id,
        lease_token=str(existing.get("lease_token", "")),
        client_id=client_id,
    )
    return find_existing_run(base_dir, server_id, workflow_id, job_id)


def _prompt_for_client(queue: dict[str, Any], client_id: str) -> str:
    for key in ("queue_running", "queue_pending"):
        for item in queue.get(key, []):
            if (
                isinstance(item, list)
                and len(item) > 1
                and _contains_client_id(item, client_id)
            ):
                return str(item[1])
    return ""


def _contains_client_id(value: Any, client_id: str) -> bool:
    if isinstance(value, dict):
        if value.get("client_id") == client_id:
            return True
        return any(
            _contains_client_id(item, client_id) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_client_id(item, client_id) for item in value)
    return False


# -- Helpers --

def _resolve_skill(ctx: typer.Context, skill_id: str) -> tuple[Any, str, str]:
    base_dir = get_base_dir(ctx.obj.get("base_dir", ""))
    if "/" in skill_id:
        server_id, workflow_id = skill_id.split("/", 1)
    else:
        config = load_config(base_dir)
        server_id = ctx.obj.get("server") or get_default_server_id(config)
        workflow_id = skill_id
    return base_dir, server_id, workflow_id


def _prepare(ctx: typer.Context, base_dir: Any, server_id: str, workflow_id: str):
    config = load_config(base_dir)
    server_config = get_server(config, server_id)
    if not server_config:
        output_error(ctx, "SERVER_NOT_FOUND", f'Server "{server_id}" not found.')
        return
    if server_config.get("enabled", True) is not True:
        output_error(ctx, "SERVER_DISABLED", f'Server "{server_id}" is disabled.')
        return

    schema_data = get_schema(base_dir, server_id, workflow_id)
    if schema_data and schema_data.get("enabled", True) is not True:
        output_error(
            ctx,
            "WORKFLOW_DISABLED",
            f'Workflow "{server_id}/{workflow_id}" is disabled.',
        )
        return
    workflow_data = get_workflow_data(base_dir, server_id, workflow_id)
    if not workflow_data:
        output_error(ctx, "SKILL_NOT_FOUND", f'Skill "{server_id}/{workflow_id}" not found.')

    client = _build_client(server_config)
    return client, schema_data or {}, workflow_data, server_config


def _build_client(server_config: dict[str, Any]) -> ComfyUIClient:
    return ComfyUIClient(
        server_url=server_config.get("url", "http://127.0.0.1:8188"),
        auth=server_config.get("auth", ""),
        comfy_api_key=server_config.get("comfy_api_key", ""),
    )


def _parse_args(ctx: typer.Context, args_str: str) -> dict[str, Any]:
    try:
        return json.loads(args_str)
    except json.JSONDecodeError as exc:
        output_error(ctx, "INVALID_ARGS", f"Invalid JSON in --args: {exc}")
        return {}


def _get_parameters(schema_data: dict[str, Any]) -> dict[str, Any]:
    return normalize_parameters(schema_data)


def _looks_like_local_path(value: str) -> bool:
    """Only treat explicit filesystem paths as upload candidates.

    Bare filenames (``cat.png``) and server subfolder refs (``clipspace/foo.png``)
    are resolved on the ComfyUI server and must not be replaced by whatever
    happens to sit in the caller's cwd.
    """
    return os.path.isabs(value) or value.startswith(("./", "../", "~"))


def _upload_media(
    ctx: typer.Context,
    client: ComfyUIClient,
    parameters: dict[str, Any],
    args: dict[str, Any],
) -> None:
    """Upload local image files referenced by image-type params to ComfyUI.

    Rewrites ``args`` in place: an explicit local file path becomes the uploaded
    reference (``subfolder/name`` or ``name``) that ``LoadImage`` expects.
    URLs, bare filenames, server-side subfolder refs, and non-existent paths
    are left untouched.
    """
    for key, value in list(args.items()):
        if parameters.get(key, {}).get("type") != "image":
            continue
        if not isinstance(value, str) or not _looks_like_local_path(value):
            continue
        expanded = os.path.expanduser(value)
        if not os.path.isfile(expanded):
            continue
        try:
            result = client.upload_image(expanded)
        except Exception as exc:
            output_error(ctx, "UPLOAD_FAILED", f"Failed to upload {value}: {exc}")
            return
        name = result.get("name", "")
        subfolder = result.get("subfolder", "")
        args[key] = f"{subfolder}/{name}" if subfolder else name


def _inject_params(
    workflow_data: dict[str, Any],
    parameters: dict[str, Any],
    args: dict[str, Any],
) -> dict[str, Any]:
    workflow = copy.deepcopy(workflow_data)
    for key, value in args.items():
        if key not in parameters:
            continue
        node_id = str(parameters[key].get("node_id", ""))
        field = parameters[key].get("field", "")
        if node_id in workflow and isinstance(workflow[node_id], dict) and "inputs" in workflow[node_id]:
            workflow[node_id]["inputs"][field] = value
    return workflow


_MEDIA_KEYS = ("images", "gifs", "audio")

_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".avi", ".mov", ".gif"}
_AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg", ".aac", ".m4a"}


def _infer_media_type(filename: str, fallback: str) -> str:
    """Infer media type from file extension.

    ComfyUI's SaveVideo/PreviewVideo nodes report video output under
    the ``"images"`` history key, so relying on the key alone would
    misclassify video files as images.
    """
    ext = Path(filename).suffix.lower()
    if ext in _VIDEO_EXTENSIONS:
        return "video"
    if ext in _AUDIO_EXTENSIONS:
        return "audio"
    return fallback


def _collect_outputs(outputs: dict[str, Any]) -> list[dict[str, str]]:
    collected: list[dict[str, str]] = []
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        for key in _MEDIA_KEYS:
            fallback = "audio" if key == "audio" else ("video" if key == "gifs" else "image")
            for item in node_output.get(key, []):
                filename = item.get("filename", "")
                collected.append({
                    "filename": filename,
                    "subfolder": item.get("subfolder", ""),
                    "type": item.get("type", "output"),
                    "media_type": _infer_media_type(filename, fallback),
                })
    return collected


def _download_outputs(
    client: ComfyUIClient,
    outputs: list[dict[str, str]],
    base_dir: Path,
    server_config: dict[str, Any],
) -> list[dict[str, str]]:
    raw_dir = str(server_config.get("output_dir", "./outputs")).strip() or "./outputs"
    configured_dir = Path(raw_dir) if Path(raw_dir).is_absolute() else base_dir / raw_dir
    output_dir = configured_dir.resolve()
    try:
        max_file_bytes = int(
            server_config.get("max_output_file_bytes", 100 * 1024 * 1024)
        )
        max_total_bytes = int(
            server_config.get("max_output_total_bytes", 500 * 1024 * 1024)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Output download limits must be integers") from exc
    if max_file_bytes <= 0 or max_total_bytes <= 0:
        raise ValueError("Output download limits must be positive")
    downloaded_bytes = 0
    for item in outputs:
        try:
            filename = item.get("filename", "")
            subfolder = item.get("subfolder", "")
            if not filename:
                raise ValueError("Output filename is empty")
            local_path = (output_dir / subfolder / filename).resolve()
            try:
                local_path.relative_to(output_dir)
            except ValueError as exc:
                raise ValueError("Output path escapes configured directory") from exc
            remaining = max_total_bytes - downloaded_bytes
            if remaining <= 0:
                raise ValueError("Output download total limit exceeded")
            size = client.download_output_to(
                filename,
                local_path,
                subfolder,
                item.get("type", "output"),
                max_bytes=min(max_file_bytes, remaining),
            )
            downloaded_bytes += size
            item["local_path"] = str(local_path)
        except Exception as exc:
            logger.warning("Failed to download %s: %s", item["filename"], exc)
            item["local_path"] = ""
    return outputs


def _format_errors(history: dict[str, Any]) -> str:
    status_info = history.get("status", {})
    messages = status_info.get("messages", [])
    parts = []
    for msg in messages:
        if isinstance(msg, list) and len(msg) >= 2:
            parts.append(str(msg[1]))
    return "; ".join(parts) if parts else "Workflow execution failed"


def classify_history(
    history: dict[str, Any],
) -> tuple[str, dict[str, Any], str]:
    """Classify ComfyUI history with terminal failures taking precedence."""
    status_info = history.get("status", {})
    outputs = history.get("outputs", {})
    status = str(status_info.get("status_str", "")).lower()
    if status in {"error", "interrupted", "cancelled"}:
        return status, outputs, _format_errors(history)
    if status_info.get("completed", False) or outputs:
        return "completed", outputs, ""
    return "pending", outputs, ""
